# check_part-db

A monitoring plugin for [Part-DB](https://github.com/Part-DB/Part-DB-server),
for Nagios, Naemon, Icinga and anything else that speaks the
[monitoring plugins API](https://www.monitoring-plugins.org/doc/guidelines.html).

It queries Part-DB's REST API (and, for the frontend check, its web UI), so
the monitoring host needs nothing but network access and an API token — no
database credentials and no agent on the Part-DB server. Everything lives in
one file, `check_part-db`, with one mode per check:

| Mode | Answers |
|---|---|
| `health` | Is the API up, is our token still accepted, does the database answer? |
| `web` | Does the web frontend serve its page, and is the TLS certificate still valid? |
| `stats` | How many parts, categories, footprints, suppliers … are in the database? |
| `stock` | Which parts have fallen below their configured minimum amount? |
| `token` | How many days is the API token itself still valid? |

## Requirements

- Python 3.7 or newer — **standard library only**, no pip packages
- Part-DB 2.x with the REST API enabled (developed against 2.9.1)
- A Part-DB API token

## Installation

```sh
install -m 0755 check_part-db /usr/lib/nagios/plugins/
```

If your Python is not at `/usr/bin/python3`, adjust the shebang line.

### Upgrading from 0.1

Version 0.1 shipped four scripts and a shared `part_db.py`. They are now modes
of `check_part-db`, and every option stayed the same, so service definitions
only change the command name:

| 0.1 | now |
|---|---|
| `check_part-db-health …` | `check_part-db health …` |
| `check_part-db-web …` | `check_part-db web …` |
| `check_part-db-stats …` | `check_part-db stats …` |
| `check_part-db-stock …` | `check_part-db stock …` |

Remove the old `check_part-db-*` scripts and `part_db.py` from the plugin
directory once the service definitions are switched over.

## API token

Create the token in Part-DB under **User settings → API tokens**. Read-only
access is enough for every mode.

Never pass the token with `-T` in a service definition: command lines are
visible to every user via `ps`. Put it in a file that only the monitoring
user can read:

```sh
umask 077
printf '%s' 'tcp_yourtokenhere' > /etc/naemon/part-db.token
chown root:naemon /etc/naemon/part-db.token
chmod 0640 /etc/naemon/part-db.token
```

The plugin looks for the token in this order:

1. `-T` / `--token` (discouraged, see above)
2. `-f` / `--token-file`
3. `$PART_DB_TOKEN_FILE`
4. `$PART_DB_TOKEN`

`web` needs no token at all — it checks the public frontend.

### Token expiry

Part-DB API tokens expire: the form proposes one year from creation, and every
mode except `web` goes CRITICAL the day the token runs out. Unlike GitLab,
Nextcloud and Immich, Part-DB cannot renew a token through its API — the only
token endpoint is the read-only `/api/tokens/current`, and new tokens can only
be created in the web interface. So there is no `rotate` mode here; instead,
the `token` mode goes CRITICAL once fewer than 30 days are left, in time to
create a new token and replace the token file.

## Common options

All modes share the standard connection options:

| Option | Meaning |
|---|---|
| `-H`, `--hostname` | Host name or address of the Part-DB server (required) |
| `-p`, `--port` | Port (default: 443 with TLS, 80 with `--no-ssl`) |
| `-S`, `--ssl` | Use HTTPS — the default; state it for readability |
| `--no-ssl` | Use plain HTTP. The API token is then sent unencrypted |
| `-u`, `--uri` | Path prefix if Part-DB is not at the server root, e.g. `/partdb` |
| `-t`, `--timeout` | Request timeout in seconds (default: 10) |
| `-T`, `--token` / `-f`, `--token-file` | API token, see above |
| `--insecure` | Do not verify the TLS certificate |
| `--ca-cert` | CA bundle used to verify the certificate |

`check_part-db --version` shows the version. `check_part-db MODE --help`
lists the options of a mode, with worked examples.

### Thresholds

`-w` and `-c` take the standard
[range format](https://www.monitoring-plugins.org/doc/guidelines.html#THRESHOLDFORMAT):
`10` alerts outside 0–10, `10:` alerts below 10, `~:10` alerts above 10,
`10:20` alerts outside 10–20, and `@10:20` alerts *inside* 10–20.

### Exit codes

- **CRITICAL** — the service is broken: unreachable, HTTP 5xx, or the API
  rejected our token. A token that has quietly expired is a real outage for
  monitoring, so it alerts rather than going UNKNOWN.
- **WARNING** / **CRITICAL** — a threshold was crossed.
- **UNKNOWN** — an operator or protocol problem: bad command line, no token
  configured, an unparseable response. This includes command line errors,
  which argparse would otherwise report with exit code 2, i.e. CRITICAL.

## The modes

### health

Verifies three things in order: the API entrypoint answers (the application
runs), the token is accepted (authentication works), and a collection query
returns (the database behind it answers). `-w`/`-c` apply to the total API
response time in seconds (default 2 and 5).

```console
$ check_part-db health -H part-db.example.org -f /etc/naemon/part-db.token
PART-DB HEALTH OK - API responding in 0.143s, token valid, 19 collections, 952 objects in /api/parts | 'time'=0.1426s;2;5;0 'api_time'=0.0548s;;;0 'query_time'=0.0878s;;;0 'collections'=19;;;0
```

Use `--probe-path` to query a collection other than `/api/parts`.

### web

Checks the frontend a user actually sees, without a token: HTTP status, an
expected string on the page (`--expect-string`, default `Part-DB`), the
reported Part-DB version, and TLS certificate expiry. `-w`/`-c` apply to the
page load time in seconds (default 3 and 8). `--cert-warning` and
`--cert-critical` are in days remaining (default 30 and 14); `--no-cert-check`
disables that part.

```console
$ check_part-db web -H part-db.example.org
PART-DB WEB OK - Part-DB 2.9.1 (753ecee8) served in 0.051s, 39256 bytes, certificate valid for 87 days | 'time'=0.0509s;3;8;0 'size'=39256B;;;0 'cert_days'=87.4;30;14
```

With `--insecure` the expiry check is skipped and says so: an unvalidated
certificate cannot be read back, so reporting on it would be guesswork. The
certificate has to match the name given with `-H`, so check the public host
name rather than a tunnel on `localhost`.

### stats

Counts every collection the API offers and reports the counts as performance
data, which is what makes the database trendable in PNP4Nagios, Grafana and
the like. The collection list comes from the API entrypoint rather than being
hard coded, so a Part-DB upgrade that adds or renames entities needs no
change here.

```console
$ check_part-db stats -H part-db.example.org -f /etc/naemon/part-db.token
PART-DB STATS OK - 952 parts, 221 categories, 617 footprints, 105 manufacturers, 0 suppliers, 0 storage_locations, 0 part_lots, 0 projects (1 collection(s) skipped) | 'attachment_types'=2;;;0 ...
```

- `--list` shows the collections this instance offers.
- `-e`/`--entity` (repeatable) limits the check to certain collections.
- `--threshold ENTITY=WARNING[,CRITICAL]` alerts on a count, e.g.
  `--threshold parts=900:,500:` to catch unexpected data loss.
- Collections the token may not read are skipped with a note; `--strict`
  reports them UNKNOWN instead. A read-only token typically cannot read
  `/api/users`, so one skipped collection is normal.

Requests run in parallel (`--workers`, default 5), which keeps a full sweep
of ~19 collections well under a second.

### stock

Reports parts that have fallen below the minimum amount configured for them,
so monitoring tells you what needs reordering. `-w`/`-c` apply to the *number*
of parts below their minimum; the default `-w 0` alerts as soon as there is
one.

```console
$ check_part-db stock -H part-db.example.org -f /etc/naemon/part-db.token
PART-DB STOCK WARNING - 6 of 12 monitored part(s) below minimum | 'low_stock'=6;0;;0 'monitored_parts'=12;;;0
part-000: 0 in stock, minimum 10 (https://part-db.example.org/en/part/0/info)
...
```

Only parts with a minimum amount above zero are considered — a part without a
minimum cannot be under-stocked. **If no part in the database defines a
minimum amount, the check reports OK and says exactly that.** Set minimum
amounts in Part-DB for this check to have anything to do.

`--full-scan` additionally walks every part to report total stock and the
number of parts with no stock at all, with optional `--zero-warning` /
`--zero-critical` thresholds. It costs a request per page of parts (a few
seconds for ~1000 parts), so it is off by default.

### token

Reports when the API token expires, with its name and access level. A token
close to its end of life is an outage in waiting, so the default is CRITICAL
below 30 days (`-c 30:`) with no warning stage; add `-w 60:` for an earlier
heads-up. A token without an expiry date is always OK.

```console
$ check_part-db token -H part-db.example.org -f /etc/naemon/part-db.token
PART-DB TOKEN OK - API token 'naemon' (read-only) expires on 2027-09-23, in 365.0 days | 'days_left'=365;;30:
Last used 2026-09-23T11:01:50+00:00
```

## Naemon / Nagios integration

```
define command {
    command_name    check_part-db-health
    command_line    $USER1$/check_part-db health -H $HOSTADDRESS$ -f /etc/naemon/part-db.token -w $ARG1$ -c $ARG2$
}

define command {
    command_name    check_part-db-token
    command_line    $USER1$/check_part-db token -H $HOSTADDRESS$ -f /etc/naemon/part-db.token
}

define command {
    command_name    check_part-db-web
    command_line    $USER1$/check_part-db web -H $HOSTADDRESS$
}

define service {
    host_name               part-db
    service_description     Part-DB API
    check_command           check_part-db-health!2!5
    use                     generic-service
}

define service {
    host_name               part-db
    service_description     Part-DB API token
    check_command           check_part-db-token
    check_interval          1440
    use                     generic-service
}

define service {
    host_name               part-db
    service_description     Part-DB frontend
    check_command           check_part-db-web
    use                     generic-service
}
```

Statistics and stock change slowly; a `check_interval` of an hour is usually
plenty, and keeps the load on Part-DB negligible. Once a day is enough for
the token.

## Notes on the Part-DB API

A part's `total_instock` is computed from its lots. It cannot be filtered
server side and is not selectable through the `properties[]` field filter,
which is why `stock` narrows the query with `minamount[gt]=0` and compares
locally instead of asking the API for "low" parts.

## Versioning

The version is reported by `check_part-db --version` and defined at the top
of `check_part-db`. Releases are tagged `vMAJOR.MINOR`.

## License

GPL-2.0-only. See [LICENSE](LICENSE).
