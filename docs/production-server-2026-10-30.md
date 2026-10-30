# Production Server Deployment (2026-10-30)

## Endpoint and Isolation

- Client API base: `https://aohuang888.com/miaoshou-platform`
- Nginx strips `/miaoshou-platform/` and proxies to `127.0.0.1:8813`.
- Service: `aohuang-listing-v2.service`, running as `miaoshou-listing-v2`.
- Application directory: `/opt/miaoshou-listing-v2/server`.
- Data directory: `/var/lib/miaoshou-listing-v2`.
- Environment file: `/etc/aohuang-listing-v2.env` (root-owned, mode `0600`).
- PostgreSQL database and role: `miaoshou_listing_v2` (dedicated to this deployment).

The existing `aohuang-listing-app.service` on port `8812`, image proxy on `8790`,
other PostgreSQL databases, and other Nginx locations were left unchanged.

## Initial Data Import

The active desktop database was `C:\AohuangDesktop\userdata\app.db`. Its SQLite
integrity check passed before import. The controlled importer backed it up, re-encrypted
Miaoshou credentials with the server-only key, and transactionally imported 20 tables
and 3,124 rows. Verified totals were 4 accounts, 355 products, 3 existing operators,
and 1 size-chart bookmark; the first teacher account was then created separately.

The verified SQLite snapshot is kept outside the repository at:
`E:\codex工作台\p006\_aohuang-postgres-migration-backups-20261030\`.

## Backup Verification

An initial PostgreSQL custom-format backup is stored at
`/var/backups/miaoshou-listing-v2/initial-migration-20261030.dump` with mode `0600`.
It was restored into a temporary isolated database and verified at 4 accounts,
355 products, 4 operators, and 1 size-chart bookmark. The temporary restore database
was removed after verification. No recurring backup timer is configured yet.

## Client Setup

The desktop renderer accepts a secure path prefix in the server address. Enter the API
base above in the server-address field, then sign in with the teacher account. The
updated renderer is in `C:\AohuangDesktop\resources\web`; restart the desktop app to
load it if it was already open during deployment.
