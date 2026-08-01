# Security policy

## Supported versions

Security fixes are applied to the latest released version and the `main`
branch. Older releases may not receive backports.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting flow from the repository's
**Security** tab.

Do not publish exploit details, credentials, certificate material, private
keys, internal or public addresses, monitoring exports, discovery caches, or
database contents in a public issue.

If private reporting is temporarily unavailable, open a public issue asking
the maintainer to establish private contact. Include no sensitive technical
details in that issue.

## Security model

Uplink Ledger is a trusted-network operational application:

- TLS protects dashboard transport.
- `systemd` runs the process as unprivileged user `uplinkledger` with two bounded
  Linux capabilities.
- PostgreSQL uses local Unix-socket peer authentication by default.
- Response headers restrict framing, content sources, referrers, and content
  type interpretation.
- The application has **no login or authorization layer**.
- Deployers must restrict TCP 80 and 443 with a host firewall, upstream
  firewall, VPN, or trusted management network.

The full trust-boundary diagram, capability explanation, TLS/file guidance,
headers, threat model, and hardening checklist are in [Deployment
security](docs/security.md).

## Operational responsibility

The dashboard and exports reveal network topology and quality history. Treat
them as sensitive operational data. Keep the host patched, protect backups,
renew certificates, restrict administrative access, and verify firewall policy
from outside the trusted network.
