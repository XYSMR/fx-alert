# Security Policy

## Supported versions

Only the current `main` branch and the live site (https://xysmr.github.io/fx-alert/) receive security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub: **Security → Report a vulnerability**
(https://github.com/XYSMR/fx-alert/security/advisories/new).

Please include:

- what is affected (site, Supabase table / function, Edge Function, GitHub workflow)
- steps to reproduce
- the impact you expect (for example, reading another user's alerts)

Do not include real API keys, tokens or other people's personal data in the report.

## What to expect

- Acknowledgement within **3 days**
- A fix or a plan within **14 days** for confirmed issues
- Credit in the advisory if you want it

## Scope

In scope:

- Access control: Row Level Security, roles, feature-flag checks
- Authentication and sessions (Google sign-in, JWT)
- Leaking secrets through workflows, logs or the public site
- Edge Functions (`unsubscribe`, `delete-account`)

Out of scope:

- Exchange-rate data from the bank's public board
- Denial of service against Supabase, GitHub or Gmail
- Issues that need access to the maintainer's accounts

## How the project protects itself

See [docs/security-design.md](docs/security-design.md): Google OIDC + JWT, RBAC with RLS,
Dependabot, secret scanning with push protection, protected environments and a ruleset on `main`.
