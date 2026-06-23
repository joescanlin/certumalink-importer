# Certuma Reach - Ops & Dev Runbook (Phase 0)

Operational floor for the Certuma Reach app. Pairs with `docs/certuma-architecture.md` and
`docs/certuma-phase0-plan.md`.

## Local development

```sh
make venv        # create .venv and install the app deps (SQLAlchemy, Alembic, psycopg, FastAPI)
make db-up       # start the local Postgres container (isolated host port 55433)
make migrate     # alembic upgrade head  (15 tables + campaign/template seed)
make test        # existing + golden + unit + db tests (db tests skip if no DB)
make test-db     # schema/ledger/gate/seed/dashboard/e2e against the live DB
make all-tests   # db-up + migrate + test + test-db
```

The repo's own Postgres runs on host port **55433** so it never collides with the existing
`certumalocal` stack on 55432.

## Test layout

- `tests/golden/` - byte-exact parity of `certuma_core` against the monolith. No DB.
- `tests/unit/` - pure unit tests (publish client, observability, config). No DB.
- `tests/db/` - schema, ledger-writer, gate, seed migration, dashboard, end-to-end. Need a
  migrated Postgres; they **skip cleanly** when no DB or SQLAlchemy is present.

## Migration policy (C4)

Alembic owns all DDL (`certuma/db/alembic/versions/`). **Forward-fix only in production**: never
run `alembic downgrade` against prod - a downgrade can drop `audit_log`, which is the CAN-SPAM /
legal record. `downgrade` is a dev/test convenience only (exercised on throwaway DBs by the schema
tests). Every schema change is a new revision.

## Backups & PITR (C4)

Because Postgres is the **sole source of truth**, durability is existential. On the managed
Postgres (provider-specific - decided with the DB-target choice), enable: automated daily backups,
WAL retention for point-in-time recovery, and a documented restore-to-timestamp procedure. Treat
`audit_log` retention as a compliance requirement, not a convenience.

## Secrets (C1)

Two separate stores, never in the repo, read only via `certuma.config.Settings.from_env`:

- **App / corporate**: `CERTUMA_DATABASE_URL`, `CERTUMALINK_API_URL`, `CERTUMALINK_API_TOKEN`.
- **Cold-ESP (firewalled)**: `CERTUMA_ESP_API_KEY` and mailbox/DMARC creds, in an isolated store
  with separate access (decision #1: the cold sending account is firewalled from corporate).

CI uses ephemeral test credentials only.

## Observability (C3)

- **Structured logs**: `certuma.observability.configure_logging()` at the app entrypoint emits
  one JSON line per event. Library/test code uses the `METRICS` sink as the assertable signal.
- **Metrics** (`certuma.observability.METRICS`, an in-process counter sink, bridge to Prometheus
  later): `ledger_transition` (by new status), `ledger_rejected` (by reason: concurrency /
  illegal_transition / illegal_actor), `gate_decision` (by decision + reason_code), `seed_run`,
  `seed_abort`. The two failure modes that matter most - silently clobbered live state and a
  silently corrupted activation metric - surface here, not just as rows nobody reads.

## Cold sending domain (A1-A4)

See `docs/certuma-phase0-plan.md` section 6. Blocked on stakeholder inputs: the cold domain name,
the cold-tolerant ESP account, and the named accountable employee. No sends happen in Phase 0.
