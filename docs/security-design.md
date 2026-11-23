# Security Design – Feature Experiment (DevOps Assignment 10, Task 2)

Owner: Developer (XYSMR). Scope: security for the two features that will be put behind toggles:

| Flag | Feature |
|---|---|
| `trip_plan` | Trip Exchange Plan (`trips`, `trip_exchanges`, `trip_notifications`) |
| `quick_target` | One-click target buttons (−0.5% / −1% / −2%) on each currency card |

## 1. Architecture and trust boundaries

```mermaid
flowchart LR
  subgraph B["Untrusted: user's browser"]
    U["GitHub Pages site<br/>publishable key only"]
  end
  G["Google<br/>OAuth 2.0 / OIDC"]
  subgraph SB["Trust boundary: Supabase"]
    AU["Auth<br/>issues JWT"]
    DB[("Postgres + RLS<br/>profiles.role, feature flags")]
    EF["Edge Functions<br/>unsubscribe, delete-account"]
    V[("Vault<br/>GitHub dispatch token")]
  end
  subgraph GH["Trust boundary: GitHub"]
    RS[("Repository secrets")]
    ENV[("Environments<br/>staging / production")]
    A["Actions<br/>check-rates, deploy-migrations"]
  end
  M["Gmail SMTP"]

  U -- "sign in" --> AU
  AU -- "OIDC" --> G
  U -- "JWT on every request" --> DB
  U -- "JWT" --> EF
  V -. "pg_cron dispatch" .-> A
  RS -. "secret key, SMTP password" .-> A
  ENV -. "DB URL (production: approval required)" .-> A
  A -- "secret key (bypasses RLS)" --> DB
  A -- "send alerts" --> M
```

* The browser is untrusted: it only holds the publishable key, so every table relies on RLS.
* Keys that bypass RLS (`sb_secret_…`, database URLs) only exist in GitHub secrets, Supabase Edge Function secrets and Vault.

## 2. Authentication and authorization

| Decision | Choice | Reason |
|---|---|---|
| Login | Supabase Auth + **Google OAuth 2.0 / OIDC**; email login disabled | No passwords to store; users already have Google accounts |
| Session | **JWT** (short-lived access token + refresh token) | Postgres reads `auth.uid()` from the JWT, so RLS works without a backend server |
| Roles | **RBAC**: `profiles.role` = `user` / `beta` / `admin` (migration `20261122000000_user_roles.sql`) | Three roles are enough for a feature experiment; simpler than ABAC |
| Role changes | Only `service_role` | `authenticated` only has column-level `UPDATE` on `email_enabled`, `notify_start`, `notify_end` |
| Feature access | Checked in Postgres (RLS / functions), never only in the UI | A hidden button is not access control |

### Who may use each toggled feature

| | Anonymous | `user` inside rollout % | `user` outside rollout % | `beta` | `admin` |
|---|---|---|---|---|---|
| `trip_plan` | ✗ | ✓ | ✗ | ✓ | ✓ |
| `quick_target` | ✗ | ✓ | ✗ | ✓ | ✓ |
| Change roles / flags | ✗ | ✗ | ✗ | ✗ | `service_role` only |

Server-side check (migration `20261123000200_trips_flag_policy.sql`, uses the Operator's `is_feature_on()`):

```sql
create policy "trips only when trip_plan is on" on public.trips
  as restrictive for insert to authenticated
  with check ((select public.is_feature_on('trip_plan')));
```

It must be **restrictive**. `trips` already has the permissive policy "insert own trips", and PostgreSQL combines permissive policies with **OR**, so a second permissive policy would not block anything (`tests/db/test_feature_flags.py::test_permissive_policy_does_not_block`). A restrictive policy is combined with **AND**: the row must belong to the user **and** `trip_plan` must be on for that user. When the flag is off the feature becomes **read-only**, matching the Designer's read-only UI (migration `20261123000300_trip_plan_readonly.sql`): five more restrictive policies block update and delete on `trips` and insert, update and delete on `trip_exchanges`. Reads still work, and the scheduler (`service_role`) and account deletion (foreign-key cascade) are not affected.

| Write when `trip_plan` is off | Database result | HTTP (PostgREST) |
|---|---|---|
| Insert trip / exchange | error 42501 | 403 |
| Update trip / exchange | error 42501 (check is in `WITH CHECK`) | 403 |
| Delete trip / exchange | 0 rows deleted, data unchanged (`USING` hides the row) | 200/204, empty result |

## 3. Software Composition Analysis (Dependabot)

* `.github/dependabot.yml`: weekly updates for `pip` and `github-actions`.
* Not covered by Dependabot (reviewed manually): `site/vendor/` (supabase-js, Chart.js copied into the repo) and Deno imports in `supabase/functions/`.
* Each alert is recorded as **Upgrade / Replace / Accept** with a reason in the assignment report.

## 4. Secret management

| Secret | Where it lives | Used by |
|---|---|---|
| `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `UNSUBSCRIBE_SECRET`, `SMTP_USER`, `SMTP_PASSWORD`, `HEALTHCHECK_PING_URL` | Environment `production-runtime` (`main` only, no reviewer) | `check-rates` (scheduled) |
| `STAGING_SUPABASE_*`, `STAGING_UNSUBSCRIBE_SECRET`, `SMTP_*` | Environment `staging-runtime` (`main` only) | `staging-check` |
| `STAGING_DB_URL` | Environment `staging` | `deploy-migrations` (staging job) |
| `PRODUCTION_DB_URL` | Environment `production` (required reviewer, `main` only) | `deploy-migrations` (production job) |
| `UNSUBSCRIBE_SECRET`, `FX_SECRET_KEY` | Supabase Edge Function secrets | Edge Functions |
| `github_dispatch_token` | Supabase Vault | `pg_cron` → workflow dispatch |
| Publishable key, project URL | GitHub Variables (public by design) | Website |

Repository-level secrets: none.

Controls: secret scanning and push protection are enabled; CI fails if public files contain an email address, `SMTP_` or `sb_secret_`; secrets are passed only to the steps that need them and never printed; a test checks that every job using secrets is bound to an environment (`tests/test_workflows.py`).

### Risk found: teammates and repository secrets

Collaborators on a **personal** repository always get **Write** access (the Triage role exists only in organizations). With Write, anyone can push a branch with a modified workflow, and that workflow can read **repository** secrets. Decision for the 4-person team:

* Teammates stay collaborators so they can push branches (for example `feat/feature-flags`) and open pull requests.
* All secrets moved from repository secrets into environments that only accept `main`; a workflow on any other branch (or a pull request) cannot read them. Repository secrets are now empty.
* Ruleset on `main`: pull request required, 1 approval required (the repository admin can bypass only through a pull request), all CI checks must pass, no force push or deletion. A teammate therefore cannot merge a modified workflow into `main` without the maintainer's review.
* Because teammates had Write access before the move, the production and staging secret keys, the Gmail app password and both unsubscribe secrets were rotated, and the old keys revoked.
* Accepted risk: `STAGING_DB_URL` (environment `staging`) is not branch-limited because migrations are tested on PR branches; it only reaches the staging project.

## 5. Built now vs planned

| Item | Status |
|---|---|
| Google OIDC login, JWT sessions | Built |
| RLS on every public table | Built |
| Environments `staging` / `production` with approval | Built |
| Secret scanning, push protection, CI secret check | Built |
| `profiles.role` + `current_user_role()` + tests | Built |
| Dependabot config | Built |
| Dependabot alerts / security updates, CodeQL, private vulnerability reporting, `SECURITY.md` | Built |
| Ruleset on `main` (PR + 1 approval + 6 checks) | Built |
| Restrictive RLS: `trip_plan` off = read-only for trips and exchanges | Built (`tests/db/test_trips_flag_policy.py`) |
| All secrets in branch-limited environments | Built |
