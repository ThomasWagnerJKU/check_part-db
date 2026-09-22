# -*- coding: utf-8 -*-
#
# Part-DB monitoring plugins for Nagios/Naemon
# Copyright (C) 2026 Thomas Wagner
#
# SPDX-License-Identifier: GPL-2.0-only
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 as published
# by the Free Software Foundation. It is distributed in the hope that it
# will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# LICENSE file for details.

"""Shared helpers for the check_part-db-* monitoring plugins.

Python 3 standard library only - no third party modules, so the plugins run
on a stock Naemon host.  Install this file next to the check_part-db-*
scripts (e.g. /usr/lib/nagios/plugins/); Python puts the script's own
directory on sys.path, so the plugins import it without any PYTHONPATH
fiddling.
"""

import argparse
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1"

OK = 0
WARNING = 1
CRITICAL = 2
UNKNOWN = 3

STATUS_TEXT = {OK: "OK", WARNING: "WARNING", CRITICAL: "CRITICAL", UNKNOWN: "UNKNOWN"}

# Order in which states escalate: a plugin's overall state is the "worst" one.
_SEVERITY = {OK: 0, WARNING: 1, UNKNOWN: 2, CRITICAL: 3}


class PluginError(Exception):
    """Usage or protocol problem; the plugin exits UNKNOWN with this message."""


class AuthFailure(PluginError):
    """The API rejected our credentials.

    Monitoring has lost its access to Part-DB and somebody has to act, so
    this is reported CRITICAL rather than UNKNOWN, consistently across all
    of the plugins.
    """


class ApiUnreachable(PluginError):
    """The instance could not be contacted at all, or TLS failed.

    That is a genuine service failure rather than an operator mistake, so
    plugins exit CRITICAL on it.
    """


# --------------------------------------------------------------------------
# Threshold ranges (monitoring-plugins guideline format)
#   10      alert if value < 0 or value > 10
#   10:     alert if value < 10
#   ~:10    alert if value > 10
#   10:20   alert if value < 10 or value > 20
#   @10:20  alert if 10 <= value <= 20 (inverted)
# --------------------------------------------------------------------------
class Range(object):
    def __init__(self, spec):
        self.spec = spec
        self.inverted = False
        self.start = 0.0
        self.end = float("inf")

        text = str(spec).strip()
        if not text:
            raise PluginError("empty threshold range")
        if text.startswith("@"):
            self.inverted = True
            text = text[1:]

        if ":" in text:
            low, high = text.split(":", 1)
            self.start = float("-inf") if low in ("~", "") else self._num(low)
            self.end = float("inf") if high == "" else self._num(high)
        else:
            self.end = self._num(text)

        if self.start > self.end:
            raise PluginError(
                "invalid threshold range '%s': start is greater than end" % spec
            )

    @staticmethod
    def _num(text):
        try:
            return float(text)
        except ValueError:
            raise PluginError("invalid number '%s' in threshold range" % text)

    def breached(self, value):
        """True when *value* should raise an alert for this range."""
        inside = self.start <= value <= self.end
        return inside if self.inverted else not inside

    def __str__(self):
        return self.spec


def parse_range(spec):
    return None if spec is None else Range(spec)


# --------------------------------------------------------------------------
# Performance data
# --------------------------------------------------------------------------
class Perfdata(object):
    def __init__(self, label, value, uom="", warn=None, crit=None,
                 minimum=None, maximum=None):
        self.label = label
        self.value = value
        self.uom = uom
        self.warn = warn
        self.crit = crit
        self.minimum = minimum
        self.maximum = maximum

    @staticmethod
    def _fmt(value):
        if value is None:
            return ""
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return ""
            text = "%.6f" % value
            text = text.rstrip("0").rstrip(".")
            return text if text else "0"
        return str(value)

    def __str__(self):
        # Labels are always single quoted: that is valid everywhere and keeps
        # labels containing spaces or '=' safe.
        label = str(self.label).replace("'", "")
        parts = [
            "'%s'=%s%s" % (label, self._fmt(self.value), self.uom),
            self._fmt(self.warn),
            self._fmt(self.crit),
            self._fmt(self.minimum),
            self._fmt(self.maximum),
        ]
        while len(parts) > 1 and parts[-1] == "":
            parts.pop()
        return ";".join(parts)


# --------------------------------------------------------------------------
# Plugin result accumulator
# --------------------------------------------------------------------------
class Plugin(object):
    def __init__(self, shortname):
        self.shortname = shortname
        self.status = OK
        self.messages = {OK: [], WARNING: [], CRITICAL: [], UNKNOWN: []}
        self.perfdata = []
        self.extra_lines = []

    def add_status(self, code, message=None):
        if _SEVERITY[code] > _SEVERITY[self.status]:
            self.status = code
        if message:
            self.messages[code].append(message)

    def add_perfdata(self, *args, **kwargs):
        self.perfdata.append(Perfdata(*args, **kwargs))

    def add_line(self, text):
        """Extra long-output line, shown below the summary."""
        self.extra_lines.append(text)

    def check_value(self, value, warn=None, crit=None):
        """Return the state *value* falls into for the given ranges."""
        if crit is not None and crit.breached(value):
            return CRITICAL
        if warn is not None and warn.breached(value):
            return WARNING
        return OK

    def check_and_report(self, value, warn, crit, template):
        """Evaluate thresholds and record a message built from *template*.

        *template* is a format string receiving the value as ``{value}``.
        """
        code = self.check_value(value, warn, crit)
        self.add_status(code, template.format(value=value))
        return code

    def exit(self, summary=None):
        pieces = []
        for code in (CRITICAL, UNKNOWN, WARNING, OK):
            pieces.extend(self.messages[code])
        text = ", ".join(pieces) if pieces else (summary or "")

        line = "%s %s - %s" % (self.shortname, STATUS_TEXT[self.status], text)
        if self.perfdata:
            line += " | " + " ".join(str(p) for p in self.perfdata)
        print(line)
        for extra in self.extra_lines:
            print(extra)
        sys.exit(self.status)


def unknown_exit(shortname, message):
    print("%s UNKNOWN - %s" % (shortname, message))
    sys.exit(UNKNOWN)


# --------------------------------------------------------------------------
# HTTP / API access
# --------------------------------------------------------------------------
class Response(object):
    def __init__(self, status, headers, body, elapsed, url):
        self.status = status
        self.headers = headers
        self.body = body
        self.elapsed = elapsed
        self.url = url

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise PluginError("response from %s is not valid JSON: %s"
                              % (self.url, exc))

    def text(self):
        return self.body.decode("utf-8", "replace")


def ssl_context(insecure=False, ca_cert=None):
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=ca_cert)


def http_get(url, token=None, timeout=10, insecure=False, ca_cert=None,
             accept="application/ld+json", follow_redirects=True):
    """GET *url* and return a Response.  HTTP error codes are returned, not
    raised; only transport level failures raise PluginError."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", accept)
    request.add_header("User-Agent", "check_part-db (monitoring plugin)")
    if token:
        request.add_header("Authorization", "Bearer " + token)

    handlers = [urllib.request.HTTPSHandler(
        context=ssl_context(insecure, ca_cert))]
    if not follow_redirects:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)

    started = time.time()
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
            return Response(response.status, dict(response.headers), body,
                            time.time() - started, response.url)
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return Response(exc.code, dict(exc.headers or {}), body,
                        time.time() - started, url)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            raise ApiUnreachable("TLS error for %s: %s (use --insecure or "
                                 "--ca-cert to adjust)" % (url, reason))
        if isinstance(reason, socket.timeout):
            raise ApiUnreachable("timeout after %ss connecting to %s"
                                 % (timeout, url))
        raise ApiUnreachable("cannot reach %s: %s" % (url, reason))
    except socket.timeout:
        raise ApiUnreachable("timeout after %ss reading from %s"
                             % (timeout, url))
    except OSError as exc:
        raise ApiUnreachable("cannot reach %s: %s" % (url, exc))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# --------------------------------------------------------------------------
# Part-DB specifics
# --------------------------------------------------------------------------
def total_items(payload, url=""):
    """Read the collection size out of an API Platform / Hydra response."""
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("hydra:totalItems", "totalItems"):
            if key in payload:
                return int(payload[key])
        for key in ("hydra:member", "member"):
            if key in payload:
                return len(payload[key])
    raise PluginError("cannot determine item count from response%s"
                      % (" of " + url if url else ""))


def collection_members(payload):
    """Return the item list out of an API Platform / Hydra response."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("hydra:member", "member"):
            if key in payload:
                return payload[key]
    return []


class PartDbApi(object):
    def __init__(self, base_url, token, timeout=10, insecure=False,
                 ca_cert=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.insecure = insecure
        self.ca_cert = ca_cert
        self.request_count = 0
        self.total_time = 0.0

    def url_for(self, path, params=None):
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        else:
            url = self.base_url + "/" + path.lstrip("/")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        return url

    def get(self, path, params=None, accept="application/ld+json"):
        url = self.url_for(path, params)
        response = http_get(url, token=self.token, timeout=self.timeout,
                            insecure=self.insecure, ca_cert=self.ca_cert,
                            accept=accept)
        self.request_count += 1
        self.total_time += response.elapsed
        return response

    def get_json(self, path, params=None, accept="application/ld+json"):
        """GET and decode, turning API level failures into PluginError."""
        response = self.get(path, params, accept)
        if response.status == 401:
            raise AuthFailure("API token rejected (HTTP 401) - check the token "
                              "and that it has not expired")
        if response.status == 403:
            raise AuthFailure("API token lacks permission for %s (HTTP 403)"
                              % response.url)
        if response.status >= 400:
            raise PluginError("HTTP %s from %s: %s"
                              % (response.status, response.url,
                                 _short(response.text())))
        return response.json()

    def count(self, path):
        """Number of objects in a collection, fetching a single item only."""
        payload = self.get_json(path, {"itemsPerPage": 1, "page": 1})
        return total_items(payload, path)


def _short(text, limit=160):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------------------------
# Argument handling shared by all plugins
# --------------------------------------------------------------------------
class ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which Nagios reads as CRITICAL.

    A wrong command line is an operator mistake, not a service failure, so
    the plugins exit UNKNOWN instead.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        sys.exit(UNKNOWN)


def add_common_args(parser, needs_token=True):
    parser.add_argument("-V", "--version", action="version",
                        version="%(prog)s " + VERSION,
                        help="Show the plugin version and exit")
    parser.add_argument("-H", "--hostname", required=True, metavar="ADDRESS",
                        help="Host name or address of the Part-DB server, "
                             "e.g. part-db.example.org")
    parser.add_argument("-p", "--port", type=int, metavar="PORT",
                        help="Port to connect to (default: 443 with TLS, "
                             "80 with --no-ssl)")
    parser.add_argument("-S", "--ssl", dest="ssl", action="store_true",
                        default=True,
                        help="Use HTTPS (default). Stated explicitly for "
                             "readability in service definitions")
    parser.add_argument("--no-ssl", dest="ssl", action="store_false",
                        help="Use plain HTTP. Note that the API token is then "
                             "sent unencrypted")
    parser.add_argument("-u", "--uri", metavar="PATH", default="",
                        help="Path prefix when Part-DB is not served from the "
                             "server root, e.g. /partdb")
    parser.add_argument("-t", "--timeout", type=float, default=10.0,
                        metavar="SEC",
                        help="Request timeout in seconds (default: 10)")
    parser.add_argument("--insecure", action="store_true",
                        help="Do not verify the TLS certificate")
    parser.add_argument("--ca-cert", metavar="FILE",
                        help="CA bundle used to verify the TLS certificate")
    if needs_token:
        parser.add_argument("-T", "--token", metavar="TOKEN",
                            help="Part-DB API token. Prefer --token-file or "
                                 "the PART_DB_TOKEN environment variable: "
                                 "arguments are visible in the process list")
        parser.add_argument("-f", "--token-file", metavar="FILE",
                            help="File containing the API token "
                                 "(default: $PART_DB_TOKEN_FILE)")
    return parser


def resolve_token(args):
    """Token from --token, --token-file, $PART_DB_TOKEN or $PART_DB_TOKEN_FILE."""
    if getattr(args, "token", None):
        return args.token.strip()

    path = getattr(args, "token_file", None) or os.environ.get("PART_DB_TOKEN_FILE")
    if path:
        try:
            with open(path, "r") as handle:
                token = handle.read().strip()
        except IOError as exc:
            raise PluginError("cannot read token file %s: %s" % (path, exc))
        if not token:
            raise PluginError("token file %s is empty" % path)
        return token

    token = os.environ.get("PART_DB_TOKEN", "").strip()
    if token:
        return token

    raise PluginError("no API token given - use --token, --token-file, "
                      "$PART_DB_TOKEN or $PART_DB_TOKEN_FILE")


def base_url_from_args(args):
    """Assemble the base URL from -H/-p/-S/-u."""
    host = args.hostname.strip()
    if "://" in host or "/" in host:
        raise PluginError("-H takes a host name or address, not a URL; use "
                          "-H %s and, if needed, --no-ssl, -p and -u"
                          % host.split("://")[-1].split("/")[0])
    if not host:
        raise PluginError("-H requires a host name or address")

    scheme = "https" if args.ssl else "http"
    port = args.port if args.port else (443 if args.ssl else 80)
    if port < 1 or port > 65535:
        raise PluginError("invalid port %s" % port)

    # Bracket IPv6 literals so the URL stays parseable.
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    # Leave the default port out: it keeps the Host header conventional.
    authority = host if port == (443 if args.ssl else 80) else "%s:%d" % (host,
                                                                          port)

    uri = args.uri.strip()
    if uri and not uri.startswith("/"):
        uri = "/" + uri
    return "%s://%s%s" % (scheme, authority, uri.rstrip("/"))


def api_from_args(args):
    return PartDbApi(base_url_from_args(args), resolve_token(args),
                     timeout=args.timeout, insecure=args.insecure,
                     ca_cert=args.ca_cert)


def run(main_func, shortname):
    """Wrap a plugin main() so no traceback ever reaches the monitoring core.

    An unreachable instance or a rejected token is a service failure
    (CRITICAL); anything else that goes wrong is an operator or protocol
    problem (UNKNOWN).
    """
    try:
        main_func()
    except (ApiUnreachable, AuthFailure) as exc:
        print("%s CRITICAL - %s" % (shortname, exc))
        sys.exit(CRITICAL)
    except PluginError as exc:
        unknown_exit(shortname, str(exc))
    except SystemExit:
        raise
    except KeyboardInterrupt:
        unknown_exit(shortname, "interrupted")
    except Exception as exc:  # pragma: no cover - safety net
        unknown_exit(shortname, "unhandled %s: %s" % (type(exc).__name__, exc))
