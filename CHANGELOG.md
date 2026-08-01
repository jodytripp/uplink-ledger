# Changelog

All notable changes to Uplink Ledger are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses semantic versioning.

## [Unreleased]

## [1.3.0] - 2026-07-31

### Added

- Public Uplink Ledger branding and repository documentation.
- MIT license, contribution guidance, security policy, architecture notes,
  troubleshooting guide, issue templates, and CI workflow.

### Changed

- Product, dashboard, terminal, installer, and service descriptions now use
  the Uplink Ledger name.
- Existing `isp-loss-monitor` paths, service names, database names, and role
  names remain unchanged for upgrade compatibility.

### Included from pre-public development

- Synchronized Router, First Hop, Cloudflare, Google, and Quad9 probes.
- PostgreSQL history with idempotent CSV import and export mirror.
- Public-IP-aware persistent First-Hop discovery.
- Exact five-minute wall-clock intervals and restart-safe runtime tracking.
- TLS on 443 with permanent redirects from port 80.
- Independent chart ranges with scrolling, dragging, hover details, and
  rolling live windows.

[Unreleased]: https://github.com/jodytripp/uplink-ledger/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/jodytripp/uplink-ledger/releases/tag/v1.3.0
