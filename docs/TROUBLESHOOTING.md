# Troubleshooting

## Start with service status

```sh
sudo systemctl status isp-loss-monitor --no-pager -l
sudo journalctl -u isp-loss-monitor -n 100 --no-pager
```

The service logs its version, targets, database URI, and discovery result at
startup. Do not post unredacted logs publicly if they contain addresses you
consider sensitive.

## The service fails immediately

Validate the installed arguments and required files:

```sh
sudo cat /etc/sysconfig/isp-loss-monitor
sudo -u ispmon test -r /etc/isp-loss-monitor/server.crt
sudo -u ispmon test -r /etc/isp-loss-monitor/server.key
sudo -u ispmon psql -d isp_loss_monitor -c 'SELECT 1;'
```

Check for port conflicts:

```sh
sudo ss -ltnp '( sport = :80 or sport = :443 )'
```

## PostgreSQL peer authentication fails

The Linux service user and PostgreSQL role must both be `ispmon`. Verify:

```sh
id ispmon
sudo -u postgres psql -Atc "SELECT rolname FROM pg_roles WHERE rolname='ispmon';"
sudo -u ispmon psql -d isp_loss_monitor -c 'SELECT current_user;'
```

Confirm this rule appears before broader rules in
`/var/lib/pgsql/data/pg_hba.conf`:

```text
local   isp_loss_monitor   ispmon   peer
```

Then reload PostgreSQL:

```sh
sudo systemctl reload postgresql
```

## TLS certificate or key errors

The key must be PEM encoded and unencrypted. Both files must be readable by the
service account:

```sh
sudo chown root:ispmon \
  /etc/isp-loss-monitor/server.crt \
  /etc/isp-loss-monitor/server.key
sudo chmod 0640 \
  /etc/isp-loss-monitor/server.crt \
  /etc/isp-loss-monitor/server.key
sudo -u ispmon test -r /etc/isp-loss-monitor/server.key
```

Verify the certificate hostname:

```sh
openssl x509 -in /etc/isp-loss-monitor/server.crt -noout -subject -issuer -dates -ext subjectAltName
```

## The dashboard is unreachable

Verify listeners and local health first:

```sh
sudo ss -ltn '( sport = :80 or sport = :443 )'
curl --fail --resolve monitor.example.com:443:127.0.0.1 \
  https://monitor.example.com/api/health
```

If the local request succeeds, inspect `firewalld`, upstream firewall rules,
DNS, and routing. The installer does not automatically open firewall ports.

## First Hop is unavailable

Many ISP routers filter or rate-limit traceroute responses. Test manually:

```sh
ip route show default
traceroute -n -m 12 -q 1 -w 2 1.1.1.1
sudo -u ispmon cat /var/lib/isp-loss-monitor/discovery-cache.json
```

If a previous hop is cached and the public IP is unchanged, Uplink Ledger will
continue using it. If no hop has ever responded, public-destination monitoring
continues without one.

## The public-IP check fails

```sh
sudo -u ispmon curl --fail --silent --show-error \
  https://ipv4.icanhazip.com/
```

DNS, TLS trust, outbound HTTPS policy, or endpoint availability may be the
cause. A failure does not discard an existing cached First Hop.

## Charts show no history

The service waits for the next exact five-minute boundary and records only
complete intervals. Confirm rows exist:

```sh
sudo -u ispmon psql -d isp_loss_monitor -c \
  'SELECT count(*), max(interval_end) FROM isp_loss_intervals;'
```

The browser's initial history request can be larger than later refreshes.
Reload once after confirming the API responds:

```sh
curl --fail 'https://YOUR_MONITOR_HOST/api/status?limit=12'
```

## Continuous runtime reset unexpectedly

Runtime remains continuous only when the unmeasured gap between completed
intervals is ten minutes or less. Inspect gaps:

```sh
sudo -u ispmon psql -d isp_loss_monitor -c \
  "SELECT interval_start, interval_end, interval_start - lag(interval_end) OVER (ORDER BY interval_start) AS gap FROM isp_loss_intervals ORDER BY interval_start DESC LIMIT 20;"
```

## First Hop loss is high but public targets are clean

That pattern usually indicates ICMP reply limiting at the hop rather than
forwarding loss. Uplink Ledger intentionally classifies it conservatively.
Forwarded public traffic is stronger evidence than the intermediate router's
willingness to answer pings directed at itself.

## Collecting a diagnostic bundle

Capture only what is needed and redact addresses before sharing:

```sh
sudo systemctl status isp-loss-monitor --no-pager -l
sudo journalctl -u isp-loss-monitor -n 200 --no-pager
sudo -u ispmon psql -d isp_loss_monitor -c \
  'SELECT count(*), min(interval_start), max(interval_end) FROM isp_loss_intervals;'
python3 /opt/isp-loss-monitor/isp_loss_monitor.py --version
```
