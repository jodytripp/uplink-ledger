# Contributing to Uplink Ledger

Thanks for helping improve Uplink Ledger. Changes should preserve its central
goal: collect defensible Internet-path evidence without overstating what ICMP
measurements prove.

## Development setup

The runtime uses only Python's standard library. A local PostgreSQL server is
not required for the unit tests because database calls are isolated behind the
`PostgresStore` interface.

```sh
git clone https://github.com/jodytripp/uplink-ledger.git
cd uplink-ledger
python3 -m unittest discover -s tests -v
```

Before submitting a change, run:

```sh
python3 -m py_compile isp_loss_monitor.py import_csv_to_postgres.py
sh -n install.sh
python3 -m unittest discover -s tests -v
```

## Design expectations

- Keep the installed runtime free of third-party Python dependencies unless a
  strong operational reason justifies adding one.
- Preserve AlmaLinux 10 and `systemd` compatibility.
- Treat PostgreSQL as mandatory and authoritative.
- Keep completed intervals aligned to exact wall-clock boundaries.
- Discard incomplete intervals rather than recording misleading aggregates.
- Prefer conservative diagnosis language when router behavior, ICMP rate
  limiting, or path asymmetry makes attribution uncertain.
- Preserve existing configuration and data paths during upgrades.
- Keep the dashboard usable without a JavaScript build toolchain.
- Add or update tests for behavioral changes.

## Pull requests

Keep pull requests focused. Include:

- the problem being solved;
- the behavior before and after the change;
- validation performed;
- installation or compatibility impact; and
- screenshots for material dashboard changes, with real addresses redacted.

Do not commit production certificates, private keys, public IP addresses,
private network diagrams, database dumps, or unredacted monitoring exports.

## Bug reports

Use the bug-report template and include the Uplink Ledger version, AlmaLinux
version, PostgreSQL version, relevant service logs, and steps to reproduce.
Redact public IP addresses and any internal details you do not want published.
