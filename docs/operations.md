# Operations

Uplink Ledger is intended to run continuously under `systemd`. PostgreSQL
retains completed history, the CSV mirrors exports, and a short restart does
not necessarily reset the displayed continuous runtime.

## Startup lifecycle

```mermaid
sequenceDiagram
    participant SD as systemd
    participant UL as Uplink Ledger
    participant CSV
    participant PG as PostgreSQL
    participant NET as Network discovery
    participant WEB as HTTPS / redirect servers

    SD->>UL: Start as ispmon
    UL->>CSV: Ensure file and load recent records
    UL->>PG: Ensure schema
    UL->>PG: Upsert recent CSV records
    UL->>PG: Load history and continuity bounds
    UL->>NET: Discover Router, public IP, First Hop
    UL->>WEB: Listen on 443 and 80
    UL->>UL: Wait for next exact interval boundary
    UL->>UL: Begin synchronized measurements
```

Startup is deliberately fail-fast around PostgreSQL and TLS. A process that
cannot persist authoritative history or provide its configured secure listener
should not continue in a degraded, misleading state.

## Everyday service commands

```sh
sudo systemctl status isp-loss-monitor --no-pager -l
sudo journalctl -u isp-loss-monitor -f
sudo systemctl restart isp-loss-monitor
sudo systemctl stop isp-loss-monitor
sudo systemctl start isp-loss-monitor
```

Check the version:

```sh
python3 /opt/isp-loss-monitor/isp_loss_monitor.py --version
```

Check health without loading the dashboard:

```sh
curl --fail https://monitor.example.com/api/health
```

## What happens during an interval

```mermaid
flowchart TD
    WAIT["Wait for exact boundary"] --> BEGIN["Create live interval state"]
    BEGIN --> BURST["Run one parallel probe burst"]
    BURST --> LIVE["Update live API and terminal aggregates"]
    LIVE --> DEAD{"Deadline reached?"}
    DEAD -- No --> SLOT["Wait for next 10-second slot"]
    SLOT --> BURST
    DEAD -- Yes --> FULL{"At least one burst and full window elapsed?"}
    FULL -- No --> CANCEL["Cancel incomplete interval"]
    FULL -- Yes --> WRITE["Append CSV, commit PostgreSQL, publish history"]
    WRITE --> WAIT
    CANCEL --> WAIT
```

Missed burst slots are skipped. They are never replayed in a catch-up storm.
If the service stops before the deadline, that partial interval is absent from
durable history.

## Continuous runtime across restarts

The runtime card reconstructs continuity from PostgreSQL, not merely the
current process start time.

```mermaid
flowchart LR
    A["10:00–10:05<br/>completed"] --> B["10:05–10:15<br/>restart / no complete record"]
    B --> C["10:15–10:20<br/>completed"]
    C --> KEEP["Gap from prior end = 10 minutes<br/>continuity retained"]

    A2["10:00–10:05<br/>completed"] --> B2["10:05–10:30<br/>no complete record"]
    B2 --> C2["10:30–10:35<br/>completed"]
    C2 --> RESET["Gap from prior end = 25 minutes<br/>continuity resets"]
```

The exact rule compares the next interval start with the previous completed
interval end:

- gap `≤ 600` seconds: keep the earlier continuous start;
- gap `> 600` seconds: begin a new continuous run.

Process uptime and continuous measurement runtime are separate API values.

## Time synchronization

Wall-clock alignment depends on a healthy system clock. Verify it periodically:

```sh
timedatectl status
```

The scheduler uses UTC-aware wall-clock boundaries and a monotonic timer inside
each interval. Avoid manually changing the clock during an evidence run.

## Upgrade procedure

The installer preserves sysconfig, TLS material, PostgreSQL data, the CSV
mirror, and the discovery cache.

```sh
cd uplink-ledger
git pull --ff-only

python3 -m unittest discover -s tests -v

sudo systemctl stop isp-loss-monitor
sudo ./install.sh
sudo systemctl start isp-loss-monitor
sudo systemctl status isp-loss-monitor --no-pager -l
```

Upgrade near a five-minute boundary when practical. Stopping during an interval
discards only that incomplete interval. Confirm the installed version and the
next completed database row afterward.

## Back up history

PostgreSQL is authoritative. Back it up with `pg_dump`:

```sh
sudo -u ispmon pg_dump \
  --format=custom \
  --file=/var/lib/isp-loss-monitor/isp-loss-monitor.backup \
  isp_loss_monitor
```

Copy the backup to storage outside the monitoring host. Also preserve these
small operational files when rebuilding the same installation:

```text
/etc/sysconfig/isp-loss-monitor
/etc/isp-loss-monitor/server.crt
/etc/isp-loss-monitor/server.key
/var/lib/isp-loss-monitor/discovery-cache.json
/var/lib/isp-loss-monitor/isp-packet-loss.csv
```

Treat the private key as sensitive. A new host may use a newly issued
certificate rather than restoring the old key.

Test restores into a separate empty database before relying on a backup. See
[Data and API](data-and-api.md) for schema and CSV recovery behavior.

## CSV export and migration

Download the mirror from the dashboard or:

```sh
curl --fail --output isp-packet-loss.csv \
  https://monitor.example.com/export.csv
```

To import a historical CSV archive, stop the writer and run the idempotent
importer:

```sh
sudo systemctl stop isp-loss-monitor
sudo -u ispmon /usr/bin/python3 \
  /opt/isp-loss-monitor/import_csv_to_postgres.py \
  --csv /var/lib/isp-loss-monitor/isp-packet-loss.csv
sudo systemctl start isp-loss-monitor
```

Repeated imports are safe because interval and target keys are upserted.

## Capacity and retention

At five-minute intervals, one uninterrupted year produces approximately:

- `105,120` interval rows; and
- up to `525,600` measurement rows for five targets.

Actual database size depends on PostgreSQL version, indexes, address
availability, and maintenance. Monitor it instead of guessing:

```sh
sudo -u ispmon psql -d isp_loss_monitor -c \
  "SELECT pg_size_pretty(pg_database_size(current_database()));"
```

There is no automatic retention policy. Back up the database before deleting
history, and retain the time span required for seasonal or recurring ISP
analysis.

## Certificate renewal

Replace the certificate and key atomically or during a short service stop,
preserve `root:ispmon` ownership and `0640` modes, then restart:

```sh
sudo systemctl restart isp-loss-monitor
sudo journalctl -u isp-loss-monitor -n 30 --no-pager
```

The Python TLS context loads certificate material only at process startup.

## Graceful shutdown

`systemctl stop` and `Ctrl+C` set the same stop event. The service cancels an
incomplete interval, shuts down both web listeners, joins its helper threads,
and exits. `systemd` allows 15 seconds before escalating termination.
