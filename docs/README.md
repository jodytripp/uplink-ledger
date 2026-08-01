# Documentation

The guides are grouped by task. There is no required reading order beyond
installing the application before operating it.

## Setup and administration

- [Installation](installation.md)—packages, PostgreSQL peer authentication,
  TLS, firewalling, service startup, and verification.
- [Configuration](configuration.md)—listeners, discovery, sampling, storage,
  terminal modes, defaults, and safe tuning.
- [Operations](operations.md)—service commands, interval behavior, runtime
  continuity, upgrades, backups, imports, retention, and certificate renewal.
- [Security](security.md)—network boundaries, service capabilities, TLS,
  PostgreSQL access, response headers, and deployment hardening.
- [Troubleshooting](troubleshooting.md)—symptom-driven diagnostics for startup,
  database, TLS, discovery, measurements, history, and browser problems.

## Measurements and results

- [Evidence model](evidence-model.md)—what each destination establishes,
  metric calculations, classification rules, limitations, and defensible
  conclusions.
- [Interpreting results](interpreting-results.md)—dashboard sections, rolling
  statistics, chart controls, statuses, common patterns, and ISP evidence
  collection.

## Technical reference

- [Architecture](architecture.md)—components, threads, discovery state,
  scheduling, persistence order, and failure handling.
- [Data and API](data-and-api.md)—PostgreSQL schema, startup reconciliation,
  HTTP endpoints, JSON response shape, CSV format, imports, and SQL examples.
- [Development and releases](development.md)—source layout, validation, local
  development, tests, compatibility contracts, and release procedure.

## Find the answer by task

| I need to… | Guide |
| --- | --- |
| Install on a new AlmaLinux host | [Installation](installation.md) |
| Change ping frequency, gateway, ports, or history | [Configuration](configuration.md) |
| Restart, upgrade, back up, or restore the service | [Operations](operations.md) |
| Decide whether the evidence implicates the ISP | [Evidence model](evidence-model.md) and [Interpreting results](interpreting-results.md) |
| Understand a card, chart, status, or threshold | [Interpreting results](interpreting-results.md) |
| Query PostgreSQL or consume the API | [Data and API](data-and-api.md) |
| Harden access or renew a certificate | [Security](security.md) |
| Diagnose a startup, discovery, database, or UI problem | [Troubleshooting](troubleshooting.md) |
| Understand an internal component or failure path | [Architecture](architecture.md) |
| Build, test, or release a change | [Development](development.md) |

Return to the [project README](../README.md) for the product overview and
installation summary.
