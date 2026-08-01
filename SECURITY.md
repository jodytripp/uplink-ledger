# Security policy

## Supported versions

Security fixes are applied to the latest released version and the `main`
branch. Older releases may not receive backports.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting flow from the repository's
**Security** tab. Do not publish certificate material, credentials, private
keys, internal addresses, database contents, or exploit details in a public
issue.

If private reporting is temporarily unavailable, open a public issue asking
the maintainer to establish private contact, but do not include sensitive
technical details in that issue.

## Deployment security model

Uplink Ledger provides TLS transport but intentionally has no application
login system. The dashboard and API reveal network topology and quality
history. Deployers must restrict TCP 80 and 443 with a host firewall, upstream
firewall, VPN, or trusted management network.

The installed service runs as the unprivileged `ispmon` account with a narrow
Linux capability set. TLS private keys must be unencrypted for unattended
startup and should remain owned by `root:ispmon` with mode `0640`.
