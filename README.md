# Uplink Ledger

[![Tests](https://github.com/jodytripp/uplink-ledger/actions/workflows/test.yml/badge.svg)](https://github.com/jodytripp/uplink-ledger/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Uplink Ledger is a self-contained, client-side Internet-path quality monitor.
It continuously records packet loss, latency, and jitter from a real LAN client
and turns the results into durable evidence in PostgreSQL, CSV, the terminal,
and a live TLS dashboard.

It is designed for the question that matters during an ISP dispute:

> Is the problem inside the LAN, at the router, or beyond the router on the
> Internet path?

Uplink Ledger monitors five points simultaneously:

1. the client's default router;
2. the first responding hop beyond that router;
3. Cloudflare (`1.1.1.1`);
4. Google (`8.8.8.8`); and
5. Quad9 (`9.9.9.9`).

The supported deployment target is an AlmaLinux 10 server or VM connected to
the LAN being measured. The application uses Python's standard library and the
operating system's `ping`, `traceroute`, `curl`, and `psql` clients. It does not
require Docker, Node.js, or a Python web framework.

## Features

- Synchronized probes to every destination every ten seconds.
- Exact five-minute intervals aligned to `:00`, `:05`, `:10`, and so on.
- Router and first-ISP-hop discovery with a persistent last-known-hop cache.
- Public-IP change detection through `https://ipv4.icanhazip.com/`.
- Packet loss, average/maximum RTT, jitter, sent, and received counts.
- Conservative path-fault classification intended to avoid overstating ISP
  responsibility.
- PostgreSQL as the authoritative, restart-safe history store.
- CSV mirror and one-click browser export.
- Independent 24-, 12-, 6-, 4-, and 1-hour chart windows.
- Scroll and drag navigation through up to seven days of dashboard history.
- Continuous-runtime reconstruction across short service restarts.
- Built-in HTTPS on TCP 443 and permanent HTTP-to-HTTPS redirects on TCP 80.
- Hardened `systemd` service running as an unprivileged account.
- No dashboard login system; access is intended to be restricted by firewall.

## How the evidence works

Every ten seconds, Uplink Ledger sends five pings to every available target.
Thirty bursts produce up to 150 observations per target in each complete
five-minute interval. All targets are measured in parallel, so the values in a
row describe the same time window.

| Observation | Most defensible interpretation |
| --- | --- |
| Router clean; First Hop and multiple public targets lose packets | The monitored client-to-router path is clean. Loss begins at or beyond the router's forwarding/WAN path. |
| Router clean; public targets show correlated loss; First Hop unavailable | Downstream loss is present, but traceroute filtering prevents locating the first responding ISP router. |
| Router and public targets lose packets | Investigate the client, switch/VLAN, cabling, router LAN interface, or firewall load before blaming the ISP. |
| First Hop loses packets but public targets are clean | The hop is probably limiting ICMP replies while forwarding traffic normally. |
| Only one public target loses packets | Target-specific routing or ICMP behavior; weak evidence of an access-link problem. |

A clean router ping proves that the monitored LAN path reaches the router
reliably. It does not independently prove that the router's NAT, forwarding,
or WAN path is healthy. Correlation across the First Hop and independent public
destinations is what makes the record useful.

For the cleanest evidence, use a wired monitoring host, document its switch
port and VLAN, and leave it connected in one place.

## Requirements

- AlmaLinux 10 with `systemd`.
- Root access for installation and service management.
- PostgreSQL on the same host using Unix-socket peer authentication.
- A certificate/full-chain PEM file and an unencrypted PEM private key.
- TCP 80 and 443 available on the monitoring host.
- Outbound ICMP, HTTPS, and traceroute traffic.

PostgreSQL and TLS are required for the installed service. Plain HTTP is
available only through the explicit `--insecure-http` development option.

## Installation on AlmaLinux 10

### 1. Clone the repository

```sh
git clone https://github.com/jodytripp/uplink-ledger.git
cd uplink-ledger
```

### 2. Install operating-system dependencies

```sh
sudo dnf install -y \
  python3 iputils traceroute curl postgresql postgresql-server
```

### 3. Initialize PostgreSQL

Skip initialization if PostgreSQL is already configured on this host.

```sh
sudo postgresql-setup --initdb
sudo systemctl enable --now postgresql
```

### 4. Install Uplink Ledger

The installer creates the unprivileged `ispmon` service account, installs the
application, and registers the service without starting it.

```sh
sudo ./install.sh
```

### 5. Create the database role and database

```sh
sudo -u postgres createuser \
  --no-superuser --no-createdb --no-createrole ispmon
sudo -u postgres createdb --owner=ispmon isp_loss_monitor
```

If the role or database already exists, do not recreate it.

Ensure the PostgreSQL `pg_hba.conf` contains this rule before broader local
rules:

```text
local   isp_loss_monitor   ispmon   peer
```

On a standard AlmaLinux installation the file is
`/var/lib/pgsql/data/pg_hba.conf`. Reload PostgreSQL after editing it:

```sh
sudo systemctl reload postgresql
sudo -u ispmon psql -d isp_loss_monitor -c 'SELECT current_user;'
```

The final command should report `ispmon` without prompting for a password.

### 6. Install the TLS certificate

Install the certificate/full chain and **unencrypted** private key:

```sh
sudo install -o root -g ispmon -m 0640 fullchain.pem \
  /etc/isp-loss-monitor/server.crt
sudo install -o root -g ispmon -m 0640 private-key.pem \
  /etc/isp-loss-monitor/server.key
```

The certificate's subject alternative name must match the hostname used to
open the dashboard.

### 7. Review the service configuration

```sh
sudo vi /etc/sysconfig/isp-loss-monitor
```

The supplied configuration listens on all IPv4 interfaces, serves HTTPS on
443, redirects port 80 to HTTPS, uses the local PostgreSQL socket, and stores
runtime state under `/var/lib/isp-loss-monitor`.

Automatic discovery normally uses the Linux default gateway. To monitor a
specific router address, append this inside `ISPMON_ARGS`:

```text
--gateway-address 192.168.1.1
```

### 8. Restrict network access

The dashboard intentionally has no application login. Restrict ports 80 and
443 to trusted management networks with the host firewall or an upstream
firewall.

Example for `192.168.1.0/24` with `firewalld`:

```sh
sudo firewall-cmd --permanent \
  --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port port="80" protocol="tcp" accept'
sudo firewall-cmd --permanent \
  --add-rich-rule='rule family="ipv4" source address="192.168.1.0/24" port port="443" protocol="tcp" accept'
sudo firewall-cmd --reload
```

### 9. Start the monitor

```sh
sudo systemctl enable --now isp-loss-monitor
sudo systemctl status isp-loss-monitor
```

Open `https://YOUR_MONITOR_HOST/` and verify the health endpoint:

```sh
curl --fail https://YOUR_MONITOR_HOST/api/health
```

The service waits for the next exact five-minute boundary before starting its
first interval. This is intentional.

## Upgrading

The installer preserves an existing `/etc/sysconfig/isp-loss-monitor`, TLS
material, PostgreSQL database, CSV mirror, and discovery cache.

```sh
cd uplink-ledger
git pull --ff-only
sudo systemctl stop isp-loss-monitor
sudo ./install.sh
sudo systemctl start isp-loss-monitor
sudo systemctl status isp-loss-monitor
```

An unfinished measurement interval is discarded during the restart. A gap of
ten minutes or less does not reset the continuous-runtime counter.

## Importing existing CSV history

The standalone importer is useful when migrating pre-PostgreSQL history or a
CSV archive from another installation. Stop the monitor before importing a
file that it is actively writing.

```sh
sudo systemctl stop isp-loss-monitor
sudo -u ispmon /usr/bin/python3 \
  /opt/isp-loss-monitor/import_csv_to_postgres.py \
  --csv /var/lib/isp-loss-monitor/isp-packet-loss.csv
sudo systemctl start isp-loss-monitor
```

Imports use PostgreSQL upserts and are safe to repeat.

## Service operation

```sh
sudo systemctl status isp-loss-monitor
sudo journalctl -u isp-loss-monitor -f
sudo systemctl restart isp-loss-monitor
sudo systemctl stop isp-loss-monitor
```

Installed locations:

| Purpose | Location |
| --- | --- |
| Application | `/opt/isp-loss-monitor` |
| Service configuration | `/etc/sysconfig/isp-loss-monitor` |
| TLS material | `/etc/isp-loss-monitor` |
| CSV mirror and discovery cache | `/var/lib/isp-loss-monitor` |
| PostgreSQL database | `isp_loss_monitor` |
| PostgreSQL and OS role | `ispmon` |
| systemd unit | `isp-loss-monitor.service` |

The `isp-loss-monitor` identifiers are retained for compatibility with
existing installations. Uplink Ledger is the product and repository name.

## Dashboard and API

| Endpoint | Purpose |
| --- | --- |
| `/` | Live dashboard |
| `/api/health` | Minimal health and version response |
| `/api/status?limit=288` | Live state and bounded completed history |
| `/export.csv` | CSV mirror download |

The API history limit is clamped to `1–2016`. The browser loads up to seven
days once, then returns to smaller five-second refreshes.

Each chart has an independent time range. Selecting or panning one chart does
not alter the other. Scroll or drag to browse older data; select **Latest** or
double-click that chart to resume its rolling current window.

## Configuration reference

```text
--gateway-address ADDRESS     Explicit router LAN address
--discovery-cache PATH        Persistent public-IP/First-Hop identity
--public-ip-url URL           Public IPv4 endpoint
--postgres-url URI            PostgreSQL connection URI
--listen ADDRESS              Dashboard listen address
--port PORT                   HTTPS dashboard port
--http-redirect-port PORT     Plain HTTP port permanently redirected to HTTPS
--tls-cert PATH               PEM certificate/full-chain file
--tls-key PATH                Unencrypted PEM private-key file
--csv PATH                    Compatibility/export CSV mirror
--interval-seconds 300        Aggregation interval
--burst-period 10             Time between synchronized probe bursts
--ping-count 5                Pings per destination in each burst
--ping-interval 0.2           Time between pings inside a burst
--reply-timeout 1.5           Per-reply wait
--discovery-period 3600       Public-IP/gateway discovery interval
--history-limit 2016          Maximum intervals held in dashboard memory
--terminal-mode MODE          auto, dashboard, lines, or quiet
```

Run `python3 isp_loss_monitor.py --help` for the complete command-line help.

## Storage

PostgreSQL is authoritative and has no automatic retention limit:

- `isp_loss_intervals` stores interval boundaries and assessments.
- `isp_loss_measurements` stores one destination measurement per interval.
- Primary keys make writes and imports idempotent.

The CSV file is a compatibility/export mirror and migration source. Recent CSV
records are synchronized into PostgreSQL during startup to recover from an
interrupted database write.

Useful database checks:

```sh
sudo -u ispmon psql -d isp_loss_monitor -c \
  'SELECT count(*), min(interval_start), max(interval_end) FROM isp_loss_intervals;'

sudo -u ispmon psql -d isp_loss_monitor -c \
  'SELECT target_key, avg(loss_pct), max(loss_pct) FROM isp_loss_measurements GROUP BY target_key ORDER BY target_key;'
```

## Stable First-Hop discovery

Uplink Ledger compares the default gateway and public IPv4 address with its
persistent cache. It reuses the last known First Hop while those identifiers
remain unchanged. If either changes, traceroute runs again. A filtered or
failed retrace does not erase a previously working hop; the monitor retains the
stale value and retries later.

See [Architecture](docs/ARCHITECTURE.md) for the full data flow and
[Troubleshooting](docs/TROUBLESHOOTING.md) for operational diagnostics.

## Development and tests

The application has no third-party Python runtime dependencies.

```sh
python3 -m py_compile isp_loss_monitor.py import_csv_to_postgres.py
sh -n install.sh
python3 -m unittest discover -s tests -v
```

The tests cover Linux and FreeBSD ping parsing, synchronized interval
alignment, discovery-cache behavior, retained First Hops, fault
classification, PostgreSQL upserts, CSV migration, runtime reconstruction,
the web API, and HTTP-to-HTTPS redirects.

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change.

## Security

The dashboard exposes network addresses and connection-quality history. It has
TLS but no user authentication. Do not expose it directly to the public
Internet. Report security concerns according to [SECURITY.md](SECURITY.md).

## License

Uplink Ledger is released under the [MIT License](LICENSE).
