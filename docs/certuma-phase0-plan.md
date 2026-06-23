# Certuma Phase 0 — Detailed Executable Build Plan

> Foundation only: spine + schema + library extraction + CSV→Postgres migration + cold-domain standup + dashboard skeleton. **NO sends. NO LLM nodes.** All symbol/line refs verified against `/Users/joescanlin/Documents/Certumalink-platform/portable/certumalink-doctor-import.py` (1827 lines) and `docs/certuma-architecture.md`.

## 0. Phase 0 goal & definition of done

Phase 0 builds the **deterministic spine** Certuma will later hang sending logic on, without sending anything or invoking any LLM. We (a) lift the load-bearing pure logic out of the 1827-line monolith into a tested `certuma_core` library with behavior parity pinned by golden tests, (b) stand up Postgres + Alembic with the full schema (state machine, suppression, events, idempotency, audit, time-series scoring), (c) migrate `activation_status.csv` into Postgres as source of truth via a dry-run + reconciliation seed migration, (d) replace the flat `VALID_ACTIVATION_STATUSES` set with an enforced `ALLOWED_TRANSITIONS` graph behind a single ledger-writer, (e) provision the isolated cold-sending domain (separate registrar+ESP account, SPF/DKIM/DMARC, mailboxes, Postmaster Tools) and **start** reputation warmup, and (f) ship a thin dashboard skeleton with an approval-queue view and a Gate-wired kill switch. We also establish the non-negotiable operational floor for a system whose Postgres is the **sole** source of truth: secrets isolation, automated backups/PITR, structured observability, and reproducible local-dev DB. The existing importer keeps working throughout.

**Canonical naming decision (resolved up front, applied everywhere below):**
- The lead state-machine column is **`activation_status`** on table `lead`.
- The next-touch scheduling column is **`next_action_at`** on table `lead` (this is the canonical name; the architecture doc's alternate spelling `next_touch_at` is an alias and is NOT used in the schema).
- `email_status` lives **only** on table `contact` (not `lead`, not `prospect`).
- `claim_url` lives on table **`lead`** (per-`(npi,campaign)`), NOT on `prospect`. Rationale recorded in §3 note C4: claim_url is the sole conversion event (decision #6) and a physician may run in multiple campaigns; per-campaign attribution requires per-lead issuance. This is a deliberate, documented deviation from the architecture doc's `prospect.claim_url`.

**Definition of Done (exit checklist):**
- [ ] `certuma_core` package exists; scoring, grouping, status, campaigns, ledger, urls, queue, specialty modules extracted; **zero CSV/print/network/argparse coupling** in the core.
- [ ] Golden tests prove `certuma_core` reproduces the monolith's `activation_score`, `activation_priority`, `priority_reason`, `profile_completeness_score`, `practice_group_id`/`practice_group_size`, and `rank_queue` ordering **byte-for-byte** against `tests/fixtures/nppes_mixed_page.json` and `output/live-78701.csv`.
- [ ] Scoring magic numbers (lines 1332–1374) live in an injected `ScoringConfig`; defaults reproduce today exactly (pinned by a frozen snapshot test).
- [ ] Postgres reachable; **all 15 tables** created via Alembic `upgrade head`; `downgrade base` clean; `citext` extension created as the literal first migration op.
- [ ] `ALLOWED_TRANSITIONS` graph enforced; single ledger-writer is the **only** path that writes `lead.activation_status`; illegal/backward/terminal-exit transitions rejected with a typed error; optimistic-concurrency (`lead.version`) and idempotency `(npi, campaign, cadence_step)` enforced; HOLD is a no-op (no transition).
- [ ] `activation_status.csv` migrated: dry-run produces a reconciliation report; live run upserts; immutable CSV backup retained; legacy values mapped; unknown statuses **hard-fail** the migration; every legacy lead assigned the seeded `legacy` campaign so the `lead.campaign` FK is satisfied.
- [ ] Nightly importer re-run path upserts **seed columns only** — provably cannot touch `lead.activation_status`/`lead.next_action_at`/`lead.cadence_step`/`lead.claim_url`/`lead.version`/`contact.email_status` (test-enforced, per actual table each column lives on).
- [ ] `practice_group` rows populated by the importer/migration before any `prospect.practice_group_id` FK is set (no dangling FK).
- [ ] Cold domain registered on a separate account; SPF/DKIM/DMARC (p=none) live and validating; first DMARC `rua` aggregate report confirmed received; ≥N mailboxes provisioned; Google Postmaster Tools verified; warmup started; accountable employee named.
- [ ] Dashboard skeleton runs: approval-queue view (reads `approval`), read-only pipeline/health view, global + per-campaign kill switch persisted and **read by a Gate stub** (Gate returns `HOLD` when killed/paused; `BLOCK` on suppression).
- [ ] `suppression`, `event`, `audit_log`, `app_user` tables exist and are writable; suppression keyed by **both email AND npi**.
- [ ] Secrets management in place: no credential in the repo; cold-ESP creds in a **separate** store from corporate; app reads DSN/tokens from injected config.
- [ ] Automated Postgres backups + PITR enabled; forward-fix migration policy documented (no `downgrade` in prod).
- [ ] Structured logging + a metrics sink emit for importer, migration, ledger-writer, and Gate; local-dev DB is reproducible via `docker-compose` + `make`.

---

## 1. Repository & project layout

Two installable packages in one repo. `certuma_core` is **pure/stdlib-friendly** (the importer pipeline stays stdlib — it has zero third-party deps today per `pyproject.toml`). The `certuma` app deliberately adopts SQLAlchemy/Alembic/FastAPI — the "stdlib-only" constraint is intentionally left behind at the app boundary, not in the lifted library.

```
Certumalink-platform/
├── portable/certumalink-doctor-import.py      # UNCHANGED — keeps working all of Phase 0
├── src/certumalink_importer/                  # UNCHANGED fetch/normalize/export pkg
├── output/, tests/fixtures/                    # golden-master inputs (regression lock)
│
├── certuma_core/                              # NEW — pure, testable, stdlib-only library
│   ├── __init__.py
│   ├── models.py          # DoctorRecord, PracticeGroup, WorkflowFields, CampaignPreset
│   ├── config.py          # ScoringConfig (weights/thresholds), PriorityThresholds
│   ├── util.py            # _clean, _digits_only, _dedupe
│   ├── status.py          # STATES, ALLOWED_TRANSITIONS, TERMINAL_STATES, LEGACY map,
│   │                      #   normalize_status(), is_legal_transition(), assert_transition()
│   ├── scoring.py         # compute_workflow_fields(), profile_completeness(),
│   │                      #   priority_counts(), average_profile_completeness()
│   ├── grouping.py        # build_practice_groups, group_by_npi, practice_group_key/_id,
│   │                      #   practice_group_rows (upsert builder)
│   ├── campaigns.py       # CampaignPreset model, CAMPAIGN_PRESETS seed, get_campaign/list_campaigns
│   ├── specialty.py       # combined_specialty_filters, matches_specialty (shared predicate)
│   ├── queue.py           # rank_queue() (retyped _rox_today_rows), QUEUE_EXCLUDED_STATES
│   ├── urls.py            # profile_url, profile_slug, slugify, claim_urls_by_npi
│   ├── ledger.py          # normalize, seed-row build, in-memory merge (pure model)
│   └── reporting.py       # status_counts (pure, in-memory test version)
│
├── certuma/                                   # NEW — the app (NOT stdlib-only)
│   ├── config.py          # Settings: reads DSN/tokens from env/secret store (no os.environ literals)
│   ├── observability.py   # structured logging + metrics sink
│   ├── db/
│   │   ├── models.py      # SQLAlchemy ORM (the 15 tables in §3)
│   │   ├── session.py
│   │   └── alembic/       # migrations/ + env.py
│   ├── ledger_writer.py   # the SINGLE writer of lead.activation_status (§4)
│   ├── gate.py            # Phase-0 Gate STUB: kill-switch + suppression + transition only
│   ├── seed_importer.py   # CSV->Postgres one-time migration (§5), reuses certuma_core
│   ├── publish/
│   │   ├── payload.py     # _publish_payload + _profile_draft_row request builder
│   │   └── client.py      # _publish_to_certumalink (injected base_url/token) + claim_urls + summary
│   ├── repo/              # thin repositories (prospect, lead, suppression, approval, practice_group...)
│   ├── api/               # FastAPI app (dashboard backend, read-mostly)
│   └── dashboard/         # thin UI (approval queue, kill switch, health) — §7
│
├── tests/
│   ├── golden/            # parity tests certuma_core == monolith
│   ├── db/                # schema/migration/transition/idempotency tests
│   └── fixtures/          # existing nppes_mixed_page.json + new golden CSVs
├── docker-compose.yml     # local Postgres for dev + CI
├── Makefile               # db-up / db-seed / test targets
├── pyproject.toml         # add [project.optional-dependencies] app = [...]
└── docs/certuma-architecture.md
```

**Packaging / deps.** Keep the existing `certumalink-nppes-importer` build. Add `certuma_core` as a second package (pure, no runtime deps). Add an optional `app` extra:

```toml
[project.optional-dependencies]
app = ["SQLAlchemy>=2.0", "alembic>=1.13", "psycopg[binary]>=3.1",
       "fastapi>=0.110", "uvicorn>=0.29", "pydantic>=2.6",
       "pydantic-settings>=2.2", "dnspython>=2.6",
       "structlog>=24.1", "prometheus-client>=0.20"]
dev = ["pytest>=8", "pytest-cov"]
```
Pin Postgres ≥ 15. `certuma_core` imports nothing from `certuma`; the dependency arrow points one way (`certuma` → `certuma_core`).

---

## 2. Monolith → library extraction map

Group A = pure, lift verbatim. Group B = pure but **must be retyped/parameterized**. Group C = pure logic **kept only as reference / replaced** (copy generators). Group D = IO/network — moves to app/importer, not core. **Every pure symbol with a downstream Phase-0 consumer has an explicit home below; the NPPES normalize-selector cluster is explicitly retained in the importer package as Group D so nothing is silently dropped.**

| Monolith symbol | file:line | Target | Purity | Required changes |
|---|---|---|---|---|
| **— Scoring —** | | | | |
| `_workflow_fields` | `:1321-1391` | `certuma_core/scoring.py::compute_workflow_fields` | pure | **Refactor.** Add `config: ScoringConfig` param. Move every inline weight (`+25` phone L1333, `+10` name L1338, `+15` specialty L1340, `+15` addr L1342, `+boost` L1344, `+5` shared L1347, `+5` fresh L1350, `+10`/`-10` completeness L1353/1355) and threshold (90/70 L1352/1354, 75/50 L1366/1369) into config defaults that reproduce today. Preserve **override order** (do_not_contact → physician_activated → needs_review/completeness<70 → numeric) EXACTLY (L1357-1374). Return `missing_profile_fields` as `list[str]`, not comma-join (drop L1387 join). Add an unused `features: ScoringFeatures \| None = None` hook for future email-engagement. |
| `_profile_completeness` | `:1394-1408` | `certuma_core/scoring.py::profile_completeness` | pure | **Lift, minor.** Move the 9-field checklist into config as ordered `[(label, accessor)]`. Keep `round(present/9*100)`. Present iff `_clean(v)` non-empty. |
| `_rox_today_rows` | `:1411-1430` | `certuma_core/queue.py::rank_queue` | pure | **Refactor.** Retype to operate on `WorkflowFields`/typed records, not `dict[str,str]` — kills the `int(row['activation_score'] or '0')` smell (L1422). Promote `priority_rank={high:0,medium:1,low:2}` (L1417) to config. Preserve tie-break `(rank, -score, name, npi)`. Decouple eligibility (state-based) from ranking (score-based). |
| `_priority_counts` | `:1433-1437` | `certuma_core/scoring.py` | pure | Lift verbatim. |
| `_average_profile_completeness` | `:1440-1444` | `certuma_core/scoring.py` | pure | Lift verbatim (returns 0 on empty). |
| **— Grouping —** | | | | |
| `_build_practice_groups` | `:1262-1271` | `certuma_core/grouping.py` | pure | Lift verbatim (OrderedDict insertion order preserved). |
| `_group_by_npi` | `:1274-1279` | `certuma_core/grouping.py` | pure | Lift verbatim. |
| `_practice_group_key` | `:1282-1292` | `certuma_core/grouping.py` | pure | Lift verbatim; preserve `address_2` in key for parity (flag over-fragmentation, do not change). |
| `_practice_group_id` | `:1295-1297` | `certuma_core/grouping.py` | pure | Lift verbatim (`'practice-'+sha1(key)[:10]`). Flag: widen digest if it becomes a real PK at scale. |
| `_practice_group_rows` | `:1300-1318` | `certuma_core/grouping.py::practice_group_rows` | pure | **Retype.** Becomes the `practice_group` UPSERT-row builder (sorted by -size then group_id). Drop the `doctors`/`npi_list` joined-string columns — derive membership from `prospect` FK. This is the producer for the practice_group population step in §5 (closes the "no task populates practice_group" gap). |
| `_digits_only` | `:1447-1448` | `certuma_core/util.py` | pure | Lift verbatim. |
| `_clean` | `:1808-1809` | `certuma_core/util.py` | pure | Lift verbatim; shared by scoring + grouping. |
| `_dedupe` | `(helper of `_combined_specialty_filters`)` | `certuma_core/util.py::_dedupe` | pure | **Lift explicitly.** Order-preserving dedupe; pure dependency of `combined_specialty_filters`. Has a real home now (was an orphan). |
| `PracticeGroup` | `:265-272` | `certuma_core/models.py` | pure | Lift; keep mutable internally, expose frozen view to scorer. |
| **— State / Ledger —** | | | | |
| `VALID_ACTIVATION_STATUSES` (set) | `:37-47` | `certuma_core/status.py::STATES` | pure | **Replace.** Becomes node-set of `ALLOWED_TRANSITIONS` graph (§4); add 5 agentic states. Promote magic strings to a `StrEnum` shared by scorer + state machine. |
| `LEGACY_ACTIVATION_STATUS_MAP` | `:32-36` | `certuma_core/status.py::LEGACY_STATUS_MAP` | pure | Lift **verbatim** (3 entries). Used by migration + ingest only. |
| `DEFAULT_ACTIVATION_STATUS` | `:31` | `certuma_core/status.py::DEFAULT_STATE` | pure | Lift; becomes initial state + Postgres column default. |
| `QUEUE_EXCLUDED_STATUSES` | `:147` | `certuma_core/queue.py::QUEUE_EXCLUDED_STATES` | pure | Lift as migration-parity constant; add `exhausted`. Real eligibility derives from graph + suppression + Gate. |
| `_normalize_activation_status` | `:1511-1515` | `certuma_core/status.py::normalize_status` | pure | Lift verbatim. Split: `normalize()` = trim+legacy+default; `validate()` = membership/transition (separate fn). |
| `_read_status_ledger` | `:1486-1508` | `certuma/seed_importer.py` (one-time) | reads-file | **Replace.** Becomes Alembic data-migration source. Preserve **hard-fail** ValueError on unknown status. Make dedup deterministic (newest by `last_seen_at`, NOT CSV row order). |
| `_build_activation_status_rows` | `:1451-1473` | `certuma_core/ledger.py` + `certuma/repo` | pure | **Refactor.** Becomes explicit **seed-only UPSERT** (writes profile_url/display_name/specialty/practice_zip/last_seen_at ONLY). Inject clock (today `datetime.now(timezone.utc)` inline). Must be structurally unable to touch `activation_status`/`next_action_at`. |
| `_merge_status_rows` | `:1476-1483` | replaced by `ON CONFLICT DO UPDATE` | pure | **Replace** with row-level upsert; **exclude** state columns from SET clause. Drop NPI sort (cosmetic). |
| `_status_counts` | `:1518-1523` | `certuma_core/reporting.py` + SQL `GROUP BY` | pure | Keep pure version for tests; live = SQL. |
| **— Campaigns / specialty —** | | | | |
| `CampaignPreset` | `:256-262` | `certuma_core/campaigns.py` | pure | Lift; add governance fields when hydrated from DB (active, version, autonomy_level). |
| `CAMPAIGN_PRESETS` | `:288-326` | `certuma_core/campaigns.py` (seed) + `campaign` table | pure | Move 4 presets to seed migration; exact boosts: primary-care=18, dermatology=22, cardiology=22, urgent-care=18. |
| `_campaign_for_name` | `:903-906` | `certuma_core/campaigns.py::get_campaign` | pure | Raise typed `CampaignNotFound` (today: unhandled KeyError). |
| `_combined_specialty_filters` | `:909-916` | `certuma_core/specialty.py::combined_specialty_filters` | pure | Lift verbatim (order-preserving dedupe via `_dedupe`). |
| `_matches_specialty` | `:919-924` | `certuma_core/specialty.py::matches_specialty` | pure | Lift verbatim. **Preserve asymmetry**: substring-in for specialty text, exact `==` for taxonomy code; empty filter ⇒ True. Shared by import gate + scoring boost — single shared predicate, do not duplicate. |
| **— URLs / publish links —** | | | | |
| `_profile_url`/`_profile_slug`/`_slugify` | `:1526-1539` | `certuma_core/urls.py` | pure | Lift verbatim. Keep `CERTUMALINK_BASE_URL` injectable. Keep `-{npi}` suffix for slug uniqueness/idempotency. |
| `_claim_urls_by_npi` | `:1242-1259` | `certuma_core/urls.py` | pure | Lift; defines response contract `results[].{npi,claim_url}`. claim_url persisted per `(npi,campaign)` on `lead`; POLLED interim (decision #7). |
| **— Publish (Group D, app) —** | | | | |
| `_profile_draft_row` | `:1069-1103` | `certuma/publish/payload.py` | pure | **Assign home.** Canonical prospect+score+publish row builder; reused verbatim inside `_publish_payload`. Drop `str()` casting for typed DB columns. (Was an orphan — needed even in Phase 0 because the publish client targets the unbuilt endpoint.) |
| `_publish_payload` | `:1142-1171` | `certuma/publish/payload.py` | pure | Inject clock/source; request envelope `{dry_run, generated_at, source, campaign, profiles[]}`. Feed Postgres-sourced records, not CSV. |
| `_publish_to_certumalink` | `:1174-1220` | `certuma/publish/client.py` | network | Inject base_url/token (no `os.environ`). Targets unbuilt endpoint; must not block Phase 0. |
| `_publish_summary` | `:1223-1239` | `certuma/publish/client.py` | pure | Reconciliation record builder for the run-report row. |
| `_rox_outreach_row` | `:1106-1139` | reference only (see Group C) | pure | Copy columns become message-draft artifacts in Phase 1; row spine documented for §3 mapping. Not built as a send path in Phase 0. |
| **— Importer core (Group D, stays IO) —** | | | | |
| `DoctorRecord` | `:150-193` | `certuma_core/models.py` | pure | Lift dataclass; **drop `to_export_row()`** (CSV coupling). |
| `NppesClient` | `:329-379` | importer / `certuma/prospector` | network | Keep ~as-is, inject for tests. |
| `import_zip_codes` | `:382-467` | importer | network | Decouple `progress` print + `stats` mutation → return result object when lifted. |
| `normalize_result_with_reason` / `normalize_result` | `:470-530` | importer (`prospector/normalize.py`) | pure | Keep. **No email field** extracted (NPPES has none) → enrichment is downstream. |
| **NPPES normalize-selector cluster** (`_mapping`, `_is_active`, `_select_physician_taxonomy`, `_select_practice_address`, `_display_name`, `_normalize_address_zip`, `normalize_zip_code`, `_page_signature`, `_limit_pages`, `_load_fixture`) | various in `:382-530` | **stays in importer package (Group D)** | pure/IO | **Explicitly retained in importer**, not lifted to core. Listed here so reviewers confirm they were considered, not missed. None are Phase-0 library dependencies. |
| `ImportStats` | `:196-221` | importer (`prospector/stats.py`) → run-report row | pure | Per-run prospector metrics persisted to a run-report/event row; `skip_reasons` is the rejection histogram. |
| `_publish_payload`/`client`/`summary` env coupling | `:1174-1220` | injected config | mixed | **DROP `_update_self` remote-exec** (L~838, pulls+executes from `CERTUMALINK_IMPORTER_URL`) entirely (supply-chain risk). |
| `_write_bundle_outputs` | `:927-1066` | decompose | writes-file | Split pure derivation / CSV writers / network publish. App orchestrator sequences pure core calls; writes go to Postgres. |
| `OutputBundle` / `_resolve_output_bundle` / `_write_csv` / `_write_json` | `:241-253` etc. | **eliminated** | IO | Postgres replaces file outputs; per-run CSV is debug-only. |
| `summary.json` builder | `:1037-1065` | run-report row (event/audit) | pure | Counts + status_counts + priority_counts + publish summary become reconciliation columns; `paths{}` drops out. |
| **— Copy (Group C, REPLACED) —** | | | | |
| `_rox_editable_drafts` | `:1552-1582` | reference only → `template` table + COPYWRITER (Phase 1) | pure | **Do not reuse for sends.** CAN-SPAM non-compliant (no unsubscribe L1571-1577, no address, constant subject L1570). Tokens `{last_name, pitch_angle, city, claim_url}` seed the first **compliant** template. |
| `_suggested_pitch` | `:1542-1549` | reference only | pure | Reconcile token inconsistency (uses `primary_specialty`, not `pitch_angle`) when authoring the governed template. |

---

## 3. Postgres schema (DDL)

NPI is the universal key. **DDL is presented and executed in dependency order**: extension first, then tables with no FKs, then tables that reference them. The `prospect ⇄ practice_group` cycle (prospect references practice_group; practice_group rows are derived from prospect) is broken by creating both tables FK-free and adding the `prospect.practice_group_id` FK via a later `ALTER TABLE`. CSV columns from §2 map explicitly into these tables; canonical column names resolve the output-alias renames (`primary_specialty`/`primary_taxonomy_code` are canonical; the `profile_drafts` `specialty`/`taxonomy_code`/`city`/`state` are *output aliases only*).

```sql
-- ============ MIGRATION OP #1 (LITERAL FIRST STATEMENT) ============
-- Requires a role with CREATE EXTENSION privilege (superuser / rds_superuser).
-- CI asserts the extension is present before any table is created.
CREATE EXTENSION IF NOT EXISTS citext;

-- ============ PRACTICE_GROUP (created FIRST; no FK out) ============
CREATE TABLE practice_group (
    practice_group_id   TEXT PRIMARY KEY,                    -- 'practice-'+sha1(key)[:10]
    practice_group_size INTEGER NOT NULL DEFAULT 0,          -- denormalized cache
    practice_phone      TEXT NOT NULL DEFAULT '',
    practice_address_1  TEXT NOT NULL DEFAULT '',
    practice_address_2  TEXT NOT NULL DEFAULT '',
    practice_city       TEXT NOT NULL DEFAULT '',
    practice_state      VARCHAR(2) NOT NULL DEFAULT '',
    practice_zip        TEXT NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    -- 'doctors' / 'npi_list' joined strings NOT stored: derive from prospect FK
);
CREATE INDEX ix_pgroup_size ON practice_group(practice_group_size);

-- ============ APP_USER (named owner + backup; FK target for actors) ============
CREATE TABLE app_user (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    email       CITEXT UNIQUE,
    role        TEXT NOT NULL CHECK (role IN ('owner','backup','system')),
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ CAMPAIGN (CAMPAIGN_PRESETS L288-326 → governed table) ============
CREATE TABLE campaign (
    name            TEXT PRIMARY KEY,                        -- 'primary-care', ..., 'legacy'
    label           TEXT NOT NULL,
    specialty_terms TEXT[] NOT NULL DEFAULT '{}',
    priority_boost  INTEGER NOT NULL DEFAULT 0,
    pitch_angle     TEXT NOT NULL DEFAULT '',
    autonomy_level  TEXT NOT NULL DEFAULT 'assisted'
                    CHECK (autonomy_level IN ('assisted','supervised','autonomous')),
    is_active       BOOLEAN NOT NULL DEFAULT false,
    is_paused       BOOLEAN NOT NULL DEFAULT false,          -- per-campaign kill switch
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ PROSPECT (doctors.csv / EXPORT_FIELDS L48-66) — FK added later ============
CREATE TABLE prospect (
    npi                   VARCHAR(10) PRIMARY KEY,
    first_name            TEXT NOT NULL DEFAULT '',
    middle_name           TEXT NOT NULL DEFAULT '',
    last_name             TEXT NOT NULL DEFAULT '',
    credential            TEXT NOT NULL DEFAULT '',
    display_name          TEXT NOT NULL DEFAULT '',
    primary_taxonomy_code TEXT NOT NULL DEFAULT '',
    primary_specialty     TEXT NOT NULL DEFAULT '',
    practice_address_1    TEXT NOT NULL DEFAULT '',
    practice_address_2    TEXT NOT NULL DEFAULT '',
    practice_city         TEXT NOT NULL DEFAULT '',
    practice_state        VARCHAR(2) NOT NULL DEFAULT '',
    practice_zip          TEXT NOT NULL DEFAULT '',
    practice_phone        TEXT NOT NULL DEFAULT '',
    matched_zips          TEXT[] NOT NULL DEFAULT '{}',     -- was comma-joined string
    source                TEXT NOT NULL DEFAULT 'cms_nppes_registry_api',
    source_fetched_at     TIMESTAMPTZ,
    practice_group_id     TEXT,                              -- FK added via ALTER below
    profile_url           TEXT,                              -- deterministic (urls.py)
    profile_slug          TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Break the prospect⇄practice_group cycle: add the FK after both tables exist.
ALTER TABLE prospect
    ADD CONSTRAINT fk_prospect_group
    FOREIGN KEY (practice_group_id) REFERENCES practice_group(practice_group_id);
CREATE INDEX ix_prospect_group ON prospect(practice_group_id);
CREATE INDEX ix_prospect_state ON prospect(practice_state);
CREATE INDEX ix_prospect_zip   ON prospect(practice_zip);

-- ============ CONTACT (verify-first enrichment target; empty in Phase 0) ============
CREATE TABLE contact (
    id              BIGSERIAL PRIMARY KEY,
    npi             VARCHAR(10) NOT NULL REFERENCES prospect(npi),
    email           CITEXT,                                  -- nullable: no email yet
    email_status    TEXT NOT NULL DEFAULT 'unknown'          -- valid|risky|catch_all|unknown|invalid
                    CHECK (email_status IN ('valid','risky','catch_all','unknown','invalid')),
    verifier        TEXT,
    verified_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (npi, email)
);
CREATE INDEX ix_contact_npi    ON contact(npi);
CREATE INDEX ix_contact_status ON contact(email_status);

-- ============ WORKFLOW_SCORE (TIME-SERIES, versioned) — _workflow_fields output ============
CREATE TABLE workflow_score (
    id                        BIGSERIAL PRIMARY KEY,
    npi                       VARCHAR(10) NOT NULL REFERENCES prospect(npi),
    campaign                  TEXT NOT NULL DEFAULT '' REFERENCES campaign(name),  -- FK; '' allowed via seed
    activation_priority       TEXT NOT NULL CHECK (activation_priority IN ('high','medium','low')),
    activation_score          INTEGER NOT NULL,
    priority_reason           TEXT NOT NULL DEFAULT '',
    full_priority_reasons     TEXT[] NOT NULL DEFAULT '{}',  -- full list, not truncated[:3]
    profile_completeness_score INTEGER NOT NULL,
    missing_profile_fields    TEXT[] NOT NULL DEFAULT '{}',  -- was comma-joined
    practice_group_id         TEXT,
    practice_group_size       INTEGER NOT NULL DEFAULT 0,
    model_version             TEXT NOT NULL,                 -- scoring config/version hash
    scored_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_wscore_npi_time ON workflow_score(npi, scored_at DESC);
CREATE INDEX ix_wscore_campaign ON workflow_score(campaign);
-- NOTE: workflow_score.campaign FK requires a '' campaign row OR an empty-sentinel campaign.
--   The monolith scores un-campaigned records with campaign='' (L1382). We seed a row
--   name='' (label='(none)') in B10 so the FK holds; document this in the seed migration.

-- ============ LEAD (live state machine; activation_status.csv L125-133) ============
CREATE TABLE lead (
    id                    BIGSERIAL PRIMARY KEY,
    npi                   VARCHAR(10) NOT NULL REFERENCES prospect(npi),
    campaign              TEXT NOT NULL REFERENCES campaign(name),
    activation_status     TEXT NOT NULL DEFAULT 'not_contacted',  -- written ONLY by ledger_writer
    cadence_step          INTEGER NOT NULL DEFAULT 0,
    next_action_at        TIMESTAMPTZ,                             -- canonical name; never touched by importer
    stop_reason           TEXT,
    owner                 TEXT,
    claim_url             TEXT,                                    -- sole conversion CTA, per (npi,campaign)
    last_polled_at        TIMESTAMPTZ,                             -- claim_url poll bookkeeping (decision #7)
    activation_detected_at TIMESTAMPTZ,                            -- set when poll/webhook sees claim click
    version               INTEGER NOT NULL DEFAULT 0,              -- optimistic concurrency
    last_seen_at          TIMESTAMPTZ,                             -- seed col (importer refreshes)
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT lead_status_valid CHECK (activation_status IN (
        'not_contacted','queued_today','enriching','sendable','email_sent',
        'awaiting_reply','replied','interested','called_no_answer','voicemail_left',
        'physician_activated','do_not_contact','needs_review','exhausted')),
    UNIQUE (npi, campaign)                                         -- one lead per (npi,campaign)
);
CREATE INDEX ix_lead_status      ON lead(activation_status);
CREATE INDEX ix_lead_next_action ON lead(next_action_at) WHERE next_action_at IS NOT NULL;
CREATE INDEX ix_lead_poll        ON lead(last_polled_at)
    WHERE activation_status NOT IN ('physician_activated','do_not_contact','exhausted');

-- ============ THREAD ============
CREATE TABLE thread (
    id              BIGSERIAL PRIMARY KEY,
    lead_id         BIGINT NOT NULL REFERENCES lead(id),
    reply_token     TEXT UNIQUE,                              -- plus-addressed Reply-To token
    is_locked       BOOLEAN NOT NULL DEFAULT false,           -- conversation lock (reply race)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_thread_lead ON thread(lead_id);

-- ============ MESSAGE (idempotency key written BEFORE ESP call) ============
CREATE TABLE message (
    id              BIGSERIAL PRIMARY KEY,
    lead_id         BIGINT NOT NULL REFERENCES lead(id),
    thread_id       BIGINT REFERENCES thread(id),
    npi             VARCHAR(10) NOT NULL,
    campaign        TEXT NOT NULL REFERENCES campaign(name),  -- FK so a typo can't fragment idem space
    cadence_step    INTEGER NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('outbound','inbound')),
    variant_id      TEXT,
    subject         TEXT,
    body_rendered   TEXT,                                     -- retained for CAN-SPAM defense
    esp_message_id  TEXT,
    sent_at         TIMESTAMPTZ,
    delivered       BOOLEAN NOT NULL DEFAULT false,
    bounced         BOOLEAN NOT NULL DEFAULT false,
    complained      BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- THE idempotency key: exactly one OUTBOUND per (npi,campaign,cadence_step), matching arch
-- (npi,campaign,cadence_step). Scoped to outbound via a PARTIAL unique index so that
-- inbound replies (which have no natural cadence_step) never collide.
CREATE UNIQUE INDEX uq_msg_idem_outbound
    ON message (npi, campaign, cadence_step)
    WHERE direction = 'outbound';
-- Inbound dedup keyed on the ESP's own id (each inbound reply is a distinct ESP message).
CREATE UNIQUE INDEX uq_msg_inbound_esp
    ON message (esp_message_id)
    WHERE direction = 'inbound' AND esp_message_id IS NOT NULL;
CREATE INDEX ix_msg_lead   ON message(lead_id);
CREATE INDEX ix_msg_thread ON message(thread_id);

-- ============ EVENT (deduped incl. internal/polled events; at-least-once / out-of-order safe) ============
CREATE TABLE event (
    id              BIGSERIAL PRIMARY KEY,
    dedup_key       TEXT NOT NULL,                            -- esp_event_id OR synthetic internal key
    lead_id         BIGINT REFERENCES lead(id),
    message_id      BIGINT REFERENCES message(id),
    npi             VARCHAR(10),
    event_type      TEXT NOT NULL CHECK (event_type IN
                    ('delivered','opened','replied','bounced','complained',
                     'activated','opt_out','unsubscribe_click','sent')),
    payload         JSONB NOT NULL DEFAULT '{}',
    occurred_at     TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Single NON-NULL dedup key for BOTH ESP and internal/polled events. Polled activation events
-- use a deterministic key (e.g. 'poll:activated:{npi}:{campaign}'), so re-polling the same
-- claim click can NEVER insert a duplicate and re-fire the terminal transition.
CREATE UNIQUE INDEX uq_event_dedup ON event(dedup_key);
CREATE INDEX ix_event_lead ON event(lead_id);
CREATE INDEX ix_event_type ON event(event_type);

-- ============ SUPPRESSION (keyed by BOTH email AND npi; never deleted) ============
CREATE TABLE suppression (
    id              BIGSERIAL PRIMARY KEY,
    npi             VARCHAR(10),
    email           CITEXT,
    reason          TEXT NOT NULL CHECK (reason IN
                    ('opt_out','hard_bounce','complaint','do_not_contact','manual','legal')),
    source          TEXT NOT NULL DEFAULT 'system',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT suppression_has_key CHECK (npi IS NOT NULL OR email IS NOT NULL)
);
CREATE UNIQUE INDEX uq_suppress_npi   ON suppression(npi)   WHERE npi   IS NOT NULL;
CREATE UNIQUE INDEX uq_suppress_email ON suppression(email) WHERE email IS NOT NULL;

-- ============ TEMPLATE (governed copy asset; seeded COMPLIANT, not authored in Phase 0) ============
CREATE TABLE template (
    id              BIGSERIAL PRIMARY KEY,
    campaign        TEXT REFERENCES campaign(name),
    version         INTEGER NOT NULL DEFAULT 1,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,                            -- MUST carry unsubscribe + postal address
    merge_tokens    TEXT[] NOT NULL DEFAULT '{}',             -- last_name,pitch_angle,city,claim_url
    is_approved     BOOLEAN NOT NULL DEFAULT false,
    approved_by     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (campaign, version)
);

-- ============ APPROVAL (feeds dashboard queue) ============
CREATE TABLE approval (
    id               BIGSERIAL PRIMARY KEY,
    lead_id          BIGINT NOT NULL REFERENCES lead(id),
    proposed_action  TEXT NOT NULL,        -- first_touch|reply|follow_up
    value_tier       TEXT,                 -- high|medium|low
    model_confidence NUMERIC(4,3),
    gate_reason_code TEXT,
    proposed_subject TEXT,
    proposed_body    TEXT,
    state            TEXT NOT NULL DEFAULT 'pending'
                     CHECK (state IN ('pending','approved','rejected','edited','expired')),
    sla_expires_at   TIMESTAMPTZ,                             -- expiry → HOLD (never auto-send)
    decided_by       BIGINT REFERENCES app_user(id),
    decided_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_approval_state ON approval(state);

-- ============ AUDIT_LOG (append-only; retains rendered bodies; CAN-SPAM legal defense) ============
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    entity          TEXT NOT NULL,         -- 'lead','message','suppression','migration',...
    entity_id       TEXT NOT NULL,
    npi             VARCHAR(10),
    action          TEXT NOT NULL,         -- 'transition','send','suppress','approve',...
    old_value       JSONB,
    new_value       JSONB,
    actor           TEXT NOT NULL,         -- 'ledger_writer','importer','poller','dashboard:<user>'
    reason_code     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_entity ON audit_log(entity, entity_id);
CREATE INDEX ix_audit_npi    ON audit_log(npi);

-- ============ KILL_SWITCH (global; per-campaign lives on campaign.is_paused) ============
CREATE TABLE kill_switch (
    id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- singleton
    is_active   BOOLEAN NOT NULL DEFAULT false,
    set_by      BIGINT REFERENCES app_user(id),
    set_at      TIMESTAMPTZ
);
INSERT INTO kill_switch (id, is_active) VALUES (1, false);
```

**Schema notes (load-bearing decisions):**
- **C1 — table count is 15:** practice_group, app_user, campaign, prospect, contact, workflow_score, lead, thread, message, event, suppression, template, approval, audit_log, kill_switch.
- **C2 — `next_action_at`** is the single canonical next-touch column (on `lead`). The architecture doc's `next_touch_at` is an alias only.
- **C3 — `email_status`** lives only on `contact`. The seed-only-upsert clobber-guard test (§5) therefore targets `lead.activation_status`, `lead.next_action_at`, `lead.cadence_step`, `lead.claim_url`, `lead.version` for the `lead` upsert, and `contact.email_status` separately for any `contact` upsert — never asserting `email_status` against `prospect`/`lead` (which would false-green).
- **C4 — `claim_url` on `lead`, not `prospect`** (deliberate deviation from arch). A physician in two campaigns gets a distinct claim_url per lead, so claim-click attribution is per-campaign, consistent with the `(npi,campaign,cadence_step)` idempotency space and decision #6.
- **C5 — campaign is a FK everywhere it appears** (`lead`, `workflow_score`, `message`), so a typo cannot silently fragment the idempotency space. `workflow_score.campaign` and the `''` sentinel campaign row support un-campaigned scoring (monolith L1382).
- **C6 — idempotency is a partial unique index scoped to `direction='outbound'`** on `(npi,campaign,cadence_step)` — exactly the arch key — so a second inbound reply on a thread does not throw `IntegrityError`. Inbound is deduped on `esp_message_id`.
- **C7 — `event.dedup_key` is NOT NULL and uniquely indexed**, covering polled/internal events with a deterministic synthetic key, so the polled `activated` event cannot be inserted repeatedly and re-fire the terminal transition.

**CSV→table mapping summary:** `doctors.csv`→`prospect`; `practice_groups.csv`→`practice_group` (+ FK on `prospect`; joined `doctors`/`npi_list` derived); `profile_drafts.csv`→`prospect`+`workflow_score`+`lead.claim_url`; `rox_outreach.csv`/`rox_today.csv`→`workflow_score` spine + `template`/`message` drafts (NOT prospect cols), `queue_rank` is a derived view; `activation_status.csv`→`lead`(status,next_action_at,last_seen_at) + seed cols on `prospect`; `summary.json`→a run-report row (event/audit).

---

## 4. State machine: statuses + ALLOWED_TRANSITIONS

**Extended node-set (14):** the existing 9 (`not_contacted`, `queued_today`, `called_no_answer`, `voicemail_left`, `email_sent`, `interested`, `physician_activated`, `do_not_contact`, `needs_review`) + 5 agentic (`enriching`, `sendable`, `awaiting_reply`, `replied`, `exhausted`). Modeled as `StrEnum` in `certuma_core/status.py`.

```python
# certuma_core/status.py
DEFAULT_STATE = "not_contacted"
TERMINAL_STATES = frozenset({"physician_activated", "do_not_contact", "exhausted"})
QUEUE_EXCLUDED_STATES = TERMINAL_STATES | {"needs_review"}

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "not_contacted":      frozenset({"queued_today","enriching","do_not_contact","needs_review"}),
    "queued_today":       frozenset({"enriching","sendable","do_not_contact","needs_review"}),
    "enriching":          frozenset({"sendable","needs_review","do_not_contact","exhausted"}),
    "sendable":           frozenset({"email_sent","enriching","needs_review","do_not_contact"}),
    "email_sent":         frozenset({"awaiting_reply","replied","enriching","exhausted","do_not_contact","needs_review"}),
    "awaiting_reply":     frozenset({"replied","email_sent","enriching","exhausted","needs_review","do_not_contact"}),
    "replied":            frozenset({"interested","needs_review","do_not_contact","awaiting_reply","exhausted"}),
    "interested":         frozenset({"physician_activated","awaiting_reply","email_sent","do_not_contact","needs_review","exhausted"}),
    "needs_review":       frozenset({"sendable","queued_today","enriching","do_not_contact","exhausted"}),  # HOLD; human-resumed
    "called_no_answer":   frozenset({"voicemail_left","email_sent","awaiting_reply","do_not_contact","needs_review"}),  # legacy
    "voicemail_left":     frozenset({"email_sent","awaiting_reply","do_not_contact","needs_review"}),                   # legacy
    "physician_activated": frozenset(),   # TERMINAL success (claim_url click)
    "do_not_contact":      frozenset(),   # TERMINAL suppression
    "exhausted":           frozenset(),   # TERMINAL non-success
}

def is_legal_transition(old: str, new: str) -> bool:
    return new in ALLOWED_TRANSITIONS.get(old, frozenset())

def assert_transition(old: str, new: str) -> None:
    if old in TERMINAL_STATES:
        raise IllegalTransition(f"{old} is terminal; cannot move to {new}")
    if not is_legal_transition(old, new):
        raise IllegalTransition(f"{old} -> {new} not allowed")
```

**Properties enforced:**
- No backward edges into earlier funnel stages (no `physician_activated → email_sent`); terminal nodes have empty out-sets and reject all writes.
- **Re-enrich edges exist** (closes the hard-bounce → re-enrich requirement, arch line 41/135): `email_sent → enriching`, `awaiting_reply → enriching`, and `sendable → enriching` make re-enrichment representable when verification goes stale or a soft signal warrants re-checking before any further send. Hard-bounce itself routes to suppression (`→ do_not_contact`) and writes a `suppression` row; the explicit `→ enriching` edges let a re-enrich flow exist without resurrecting a suppressed lead.
- **HOLD is a no-op, not a state.** The Gate returning `HOLD` (kill switch on, campaign paused, over warmup cap, quiet hours, missing claim_url, CAN-SPAM-incomplete) performs **no transition** — the lead stays where it is and is re-queued later. `needs_review` is reserved for genuine human escalation (low-confidence, SLA-expiry, manual flag), reached only by an explicit transition, never by a routine Gate HOLD. A Phase-0 test asserts a Gate `HOLD` leaves `lead.activation_status` and `lead.version` unchanged.
- `do_not_contact`/`physician_activated` are enforced as **hard non-sendable** by the Gate, not merely scored `low` (closes the monolith gap where they were only `priority=low`).
- **Activation is poller-only.** A test/invariant asserts that the `interested → physician_activated` transition is permitted **only** when `actor in {'poller','activation_webhook'}` — never `reply_handler`, never a human approver — so the sole success metric (claim_url click, decision #6) cannot be corrupted by a reply classification or a manual mark. The ledger-writer enforces this guard for that specific edge.

**The single ledger-writer contract** (`certuma/ledger_writer.py`) — the **only** code allowed to write `lead.activation_status`:

```python
ACTIVATION_ONLY_ACTORS = {"poller", "activation_webhook"}

def transition(session, lead_id, new_status, *, actor, reason_code,
               expected_version, idempotency=None):
    # 1. SELECT ... FOR UPDATE  (row lock)
    lead = session.execute(select(Lead).where(Lead.id == lead_id)
                           .with_for_update()).scalar_one()
    # 2. optimistic concurrency
    if lead.version != expected_version:
        raise ConcurrencyConflict(lead.id, expected_version, lead.version)
    # 3. legality (raises IllegalTransition; terminal-safe)
    assert_transition(lead.activation_status, new_status)
    # 3b. sole-success-metric guard: only the poller/webhook may activate
    if new_status == "physician_activated" and actor not in ACTIVATION_ONLY_ACTORS:
        raise IllegalActor(f"{actor} may not set physician_activated")
    # 4. idempotency (insert (npi,campaign,cadence_step) BEFORE any ESP call upstream)
    if idempotency:
        session.execute(insert(Message).values(**idempotency))  # partial UNIQUE → IntegrityError on dup
    # 5. write + bump version + append audit_log — SAME txn
    old = lead.activation_status
    lead.activation_status = new_status
    lead.version += 1
    session.add(AuditLog(entity="lead", entity_id=str(lead.id), npi=lead.npi,
                         action="transition", old_value={"status": old},
                         new_value={"status": new_status}, actor=actor, reason_code=reason_code))
    session.commit()
```

The importer's seed-upsert path is **structurally separate** and never imports `ledger_writer` — it cannot change status.

---

## 5. CSV → Postgres migration

One-time Alembic **data migration** (`certuma/seed_importer.py`), reusing `certuma_core` primitives. Idempotent and re-runnable. **It runs after the campaign seed (B10) so the `lead.campaign` FK target exists, and it populates `practice_group` before setting any `prospect.practice_group_id`.**

**Legacy campaign rule (resolved, not deferred):** `activation_status.csv` has no campaign column, but `lead.campaign` is `NOT NULL REFERENCES campaign(name)` with `UNIQUE(npi,campaign)`. Every legacy NPI is assigned to a seeded sentinel campaign **`legacy`** (`label='Legacy (pre-campaign)'`, `is_active=false`, `priority_boost=0`). This row is created in B10. Legacy leads therefore satisfy the FK and the uniqueness constraint deterministically, and can be re-assigned to real campaigns later by an explicit operation.

**Procedure:**
1. **Immutable backup.** Copy `output/activation_status.csv` → `migrations/backup/activation_status.<utc>.csv`, `chmod 0444`. Record sha256 in `audit_log`.
2. **Read + canonicalize.** Reuse `normalize_status()` (= trim + `LEGACY_STATUS_MAP` + `DEFAULT_STATE`). Apply the exact legacy map: `draft_profile_created→not_contacted`, `rox_contacted→email_sent`, `activated→physician_activated`. Blank → `not_contacted`. Empty-NPI rows skipped.
3. **Deterministic dedup.** Within-file duplicate NPI: keep newest by `last_seen_at` (do NOT rely on CSV row order — a deliberate, documented divergence from the monolith's last-row-wins; the parity suite in §8 must NOT assert the old row-order behavior for the ledger reader).
4. **Hard-fail validation.** Any normalized status not in the 14-node `STATES` set **aborts the migration** with `ValueError` (preserves `_read_status_ledger` L1500 / `_build_activation_status_rows` L1460 behavior). Silent coercion is forbidden — it would mask corruption.
5. **Populate practice_group, then prospect.** Build groups via `build_practice_groups` → upsert `practice_group` rows (using `practice_group_rows`), THEN upsert `prospect` (so `prospect.practice_group_id` FK never dangles). New NPIs from CSV that lack a fetched prospect record are inserted as minimal prospect stubs (npi + seed cols) to satisfy the `lead.npi` FK.
6. **Dry-run mode (`--dry-run`, default).** Produce a reconciliation report; write nothing.
7. **Live run.** Upsert `lead` (one per `(npi, 'legacy')` for legacy rows; campaign FK satisfied) and seed `prospect` (profile_url/display_name/specialty/practice_zip). New NPIs → `not_contacted`.

**Reconciliation report (printed + stored as audit row):**
- `csv_row_count` vs `npi_unique_count` vs `rows_to_insert` vs `rows_to_update`.
- Status histogram before/after legacy mapping (delta proves mapping applied).
- Count of rows that triggered legacy rewrite (must equal CSV occurrences of the 3 legacy values).
- List of any unknown statuses (if non-empty → **abort**, do not proceed to live).
- Skipped empty-NPI rows count.
- `practice_group` rows created/updated count; assertion that no `prospect.practice_group_id` references a missing group.
- Post-load assertions: `SELECT count(*) FROM lead` == `npi_unique_count` (+ pre-existing); `SELECT count(*) FROM lead WHERE activation_status NOT IN (STATES)` == 0; `SELECT count(*) FROM lead WHERE campaign NOT IN (SELECT name FROM campaign)` == 0.

**Nightly importer re-run rule (test-enforced):** the importer upserts **seed columns ONLY** —
```sql
INSERT INTO prospect (...) VALUES (...)
ON CONFLICT (npi) DO UPDATE SET
  display_name=EXCLUDED.display_name, primary_specialty=EXCLUDED.primary_specialty,
  practice_zip=EXCLUDED.practice_zip, profile_url=EXCLUDED.profile_url,
  matched_zips=EXCLUDED.matched_zips, practice_group_id=EXCLUDED.practice_group_id,
  updated_at=now();
-- lead: ON CONFLICT (npi,campaign) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at, updated_at=now()
--   *** activation_status, next_action_at, cadence_step, claim_url, version NOT in SET ***
-- contact: never written by the importer in Phase 0 (no email source); email_status untouched.
```
A regression test asserts the generated `lead` UPDATE SET clause never contains `activation_status`/`next_action_at`/`cadence_step`/`claim_url`/`version`, and (if a `contact` upsert path is exercised) that its SET clause never contains `email_status` — each assertion targeting the actual table the column lives on (closes the `_merge_status_rows` clobber risk, L1476-1483, and the false-green guard-drift risk).

---

## 6. Cold sending domain standup (ops runbook)

**No sends in Phase 0 — provisioning + warmup-start only.** Fully firewalled from `certumalink.com` (decision #1). Runs **parallel** to library extraction. **Credentials for the cold domain/ESP are stored in a store SEPARATE from corporate secrets (see §11); no creds in the repo.**

- [ ] **Register a separate sending domain** (e.g. `getcertuma.com`) at a **registrar account distinct** from certumalink.com — separate billing, separate login. NOT a subdomain of certumalink.com.
- [ ] **Separate ESP account** on a cold-tolerant managed provider (Instantly / Smartlead class) — own account, own API key, never the corporate SES/Postmark account (cascade-kill risk per architecture §2).
- [ ] **DNS — SPF:** `v=spf1 include:<provider-spf> -all` on the cold domain only.
- [ ] **DNS — DKIM:** publish provider-issued DKIM CNAME/TXT selectors; verify signing.
- [ ] **DNS — DMARC:** start `v=DMARC1; p=none; rua=mailto:dmarc@getcertuma.com; ruf=mailto:dmarc@getcertuma.com; fo=1` → after ≥2 weeks clean → `p=quarantine` → later `p=reject`. Document the promotion path; do not jump to reject. (`ruf` included because `fo=1` requests forensic reports; decide retention policy for forensic data.)
- [ ] **MX + inbound parse:** point MX at the provider; enable inbound-parse webhook (for Phase 1 reply handling).
- [ ] **Plus-addressed Reply-To plan:** `reply+<thread_id>@getcertuma.com` routes to the parse webhook → thread match (`thread.reply_token`). Verify the catch-all/plus routing now even though unused.
- [ ] **Mailbox provisioning:** create N accountable mailboxes (start 2–4) under the named employee's identity; set display name + title; configure each in the ESP for multi-mailbox reputation distribution.
- [ ] **Google Postmaster Tools:** verify the domain; baseline reputation/spam-rate/DKIM/DMARC dashboards.
- [ ] **Reverse DNS / PTR:** confirm provider-managed (managed-ESP usually handles).
- [ ] **Custom tracking domain** (link/open tracking) on `getcertuma.com`, never certumalink.com — keeps the cold domain firewalled and the `claim_url` success metric clean.
- [ ] **START warmup:** enable the provider's reputation-based warmup pool; low daily volume, gradual ramp. Record start date + cap schedule. Warmup caps later feed the Gate's `HOLD`-over-cap check.
- [ ] **Verify feedback loop is live:** confirm the **first DMARC `rua` aggregate report is actually received** before relying on warmup feedback (acceptance gate on A2, not just "records published").

**Still-needed inputs (blockers to flag now):** (1) the final **domain name**; (2) the **provider account** (who pays, who owns creds); (3) the **named accountable employee** (real name + title) under whom mailboxes are created and to whom escalations route (decision #5).

---

## 7. Dashboard skeleton (Phase 0 scope only)

Thin slice. **Tech:** FastAPI backend reading Postgres via `certuma/repo`; minimal server-rendered HTML (Jinja) or a small read-only page — keep it light, no build pipeline pressure. One named owner + one backup approver (decision #9), both rows in `app_user`.

**In scope:**
1. **Approval-queue view** — `GET /approvals?state=pending` reads `approval` joined to `lead`/`prospect`; renders physician context + `gate_reason_code` + `sla_expires_at`. Renders correctly **when empty** (no rows yet in Phase 0). Buttons stubbed (Approve/Edit/Reject) writing `approval.state` + `approval.decided_by` (FK to `app_user`) — no send wired.
2. **Kill switch (global + per-campaign), wired to the Gate** — `POST /kill-switch` toggles `kill_switch.is_active` (records `set_by` → `app_user`); `POST /campaign/{name}/pause` toggles `campaign.is_paused`. The **Gate stub** (`certuma/gate.py`) reads both before any (future) send and returns `HOLD reason_code=kill_switch` / `HOLD reason_code=campaign_paused`. A Phase-0 test proves: kill on ⇒ Gate returns `HOLD` and performs no transition. This is the load-bearing wiring — the switch must actually gate, not just display.
3. **Read-only pipeline/health view** — counts by `activation_status` (SQL `GROUP BY`), suppression count, lead count, last importer run, warmup start date, last migration reconciliation summary. Static deliverability placeholders (bounce/complaint = 0 until Phase 1).

**Deferred:** funnel analytics, deliverability circuit-breaker panel, compliance suppression-management UI, edit-and-send, template console, A/B, real SSO/auth — all Phase 1+. (Phase 0 `app_user` reserves identity so `decided_by`/`set_by`/audit `actor` reference real rows without building full auth.)

---

## 8. Testing strategy

**A. Golden-master parity (the regression lock — highest priority).** Drive `certuma_core` and the monolith with identical inputs; assert identical outputs.
- Input 1: `tests/fixtures/nppes_mixed_page.json` (5 results) → normalize → score → group → queue.
- Input 2: `output/live-78701.csv` (17-col real export) → load as `DoctorRecord`s → score/group.
- Assert exactly: `activation_score`, `activation_priority`, `priority_reason` (incl. the `[:3]` truncation and `'; '`/`,` joins), `profile_completeness_score`, `missing_profile_fields`, `practice_group_id` (`practice-`+sha1[:10]), `practice_group_size`, `other_doctors_at_location` (` | ` join), and `rank_queue` ordering with `queue_rank`.
- Pin the **`ScoringConfig` defaults** test: a frozen snapshot proves phone=25, name=10, specialty=15, address=15, shared=5, fresh=5, completeness +10/-10, thresholds 90/70, tiers 75/50, rank {high:0,medium:1,low:2}, boosts 18/22 — so behavior parity is provable before any re-weighting.
- Override-order test: a record with raw score ≥75 but `activation_status='needs_review'` must yield `low` (proves L1357-1374 order preserved).
- `matches_specialty` asymmetry test: substring match on specialty text, exact match on taxonomy, empty filter ⇒ True.
- **Divergence carve-out:** the parity suite deliberately does NOT assert the monolith's "last-row-wins by CSV order" for the status-ledger reader; that single intentional divergence (newest-by-`last_seen_at`) is covered by the migration suite (E), not the parity suite, and is documented as the one place we diverge.

**B. State machine.** `assert_transition` accepts every edge in `ALLOWED_TRANSITIONS`, rejects every non-edge; terminal states reject all out-transitions; `physician_activated → email_sent` rejected; legacy `called_no_answer → email_sent` accepted; re-enrich edges (`email_sent → enriching`, `awaiting_reply → enriching`, `sendable → enriching`) accepted.

**C. Ledger-writer.** Optimistic-concurrency conflict raises `ConcurrencyConflict`; `version` increments; an `audit_log` row is written in the same txn; illegal transition leaves status unchanged; `interested → physician_activated` raises `IllegalActor` for actor `reply_handler`/`dashboard:<user>` and succeeds for `poller`/`activation_webhook`.

**D. Idempotency.** Two **outbound** inserts of the same `(npi, campaign, cadence_step)` → second raises `IntegrityError` (partial UNIQUE `uq_msg_idem_outbound`). Two **inbound** replies on the same thread with distinct `esp_message_id` both succeed (no false collision); duplicate inbound with same `esp_message_id` → `IntegrityError`. Re-inserting a polled `activated` event with the same deterministic `dedup_key` → `IntegrityError` (no duplicate terminal fire).

**E. Migration.** Dry-run reconciliation counts match a hand-checked fixture CSV; legacy map applied (histogram delta); **unknown status aborts** (assert raises); within-file duplicate NPI resolves to newest `last_seen_at` (NOT CSV order); live run idempotent (re-run = no-op deltas); legacy leads land on the `legacy` campaign and satisfy the FK; `practice_group` populated before `prospect` FK set; seed-only upsert **cannot** mutate `lead.activation_status`/`next_action_at`/`cadence_step`/`claim_url`/`version` (assert UPDATE SET excludes them) nor `contact.email_status`.

**F. Schema.** `alembic upgrade head` then `downgrade base` clean on a throwaway DB; `citext` present before first table; `upgrade head` succeeds with `prospect`/`practice_group` cyclic FK resolved by `ALTER TABLE`; suppression UNIQUE on email and on npi both enforced; `lead.activation_status` CHECK rejects an off-list value; campaign FK on `lead`/`workflow_score`/`message` rejects an unknown campaign.

**G. Gate stub.** kill switch on ⇒ `HOLD reason_code=kill_switch` and no transition; campaign paused ⇒ `HOLD reason_code=campaign_paused`; suppressed npi/email ⇒ `BLOCK reason_code=suppression`; HOLD provably leaves `lead.activation_status`/`version` unchanged.

**H. Observability/config smoke.** App boots reading DSN/tokens from injected config (no `os.environ` literal in app code path); structured log lines emitted for a migration run and a ledger transition; metrics sink receives at least the importer-run and transition counters.

CI: `pytest` over `tests/golden`, `tests/db` (against the `docker-compose` ephemeral Postgres service), with `citext`-capable role.

---

## 9. Task breakdown

Two parallel tracks: **Track A (cold-domain ops)** runs independently start-to-finish; **Track B (engineering)** is the critical path. **Track C (platform floor)** unblocks app tasks. IDs, deps, acceptance, size.

| ID | Title | Depends on | Acceptance | Size |
|----|-------|-----------|------------|------|
| **A1** | Acquire domain + separate registrar/ESP account | — (needs stakeholder inputs) | Domain + ESP account owned, separate from certumalink.com | M |
| **A2** | SPF/DKIM/DMARC(p=none,ruf,fo=1) + MX + tracking domain DNS | A1 | All records validate; **first DMARC `rua` report received** | M |
| **A3** | Provision N mailboxes under named employee; Postmaster Tools | A1, A2, C1 | Mailboxes live; Postmaster verified; creds in cold-ESP secret store | S |
| **A4** | Start reputation warmup; record cap schedule | A3 | Warmup running; start date logged | S |
| **C1** | Secrets/config: `certuma/config.py` Settings; secret store; cold-ESP store separate from corporate | — | No creds in repo; app reads DSN/tokens via injected Settings; §8-H boots | S |
| **C2** | `docker-compose.yml` + `Makefile` (db-up/seed/test) for local + CI Postgres (citext role) | — | `make db-up` gives a working DB; CI uses same service | S |
| **C3** | Observability: `certuma/observability.py` structured logs + metrics sink | C1 | Importer/migration/ledger/Gate emit; §8-H passes | S |
| **C4** | Postgres backups + PITR + forward-fix migration policy doc | C2 | Automated backup + PITR enabled; rollback policy documented | M |
| **B0** | Repo scaffold: `certuma_core` + `certuma` pkgs, pyproject extras, CI wired to C2 DB | C2 | `pip install -e .[app,dev]`; empty test suite green | S |
| **B1** | Lift Group-A pure symbols (`util` incl. `_dedupe`, `grouping` incl. `practice_group_rows`, `urls`, `campaigns`, `specialty`, `models`) | B0 | Modules import; no CSV/print/network | M |
| **B2** | `status.py`: `STATES`, `ALLOWED_TRANSITIONS` (incl. re-enrich + activation-actor guard), legacy map, `assert_transition` | B1 | §8-B tests pass | M |
| **B3** | `config.py` + refactor `scoring.py` (parameterized weights) | B1 | Defaults snapshot test passes | M |
| **B4** | `queue.py` `rank_queue` retyped to objects | B3, B2 | Ordering golden test passes | S |
| **B5** | **Golden parity harness** vs monolith (fixtures + live CSV; scoring/group/queue scope) | B3, B4 | Byte-exact parity on both inputs | L |
| **B6** | `ledger.py` in-memory model: normalize, seed-row, merge semantics | B2 | Pure merge tests pass | S |
| **B7** | SQLAlchemy ORM + Alembic for all 15 tables (§3): citext-first, cyclic-FK via ALTER, partial idem index, event dedup_key | B0, C2 | `upgrade head`/`downgrade base` clean (§8-F) | L |
| **B8** | `ledger_writer.py` single writer (lock+version+transition+activation-actor guard+audit+idem) | B7, B2 | §8-C, §8-D pass | M |
| **B9** | `gate.py` Phase-0 stub (kill switch + campaign pause + suppression + transition; HOLD=no-op) | B7, B8 | §8-G pass | S |
| **B10** | Campaign + template seed migration (CAMPAIGN_PRESETS → DB) incl. `legacy` + `''` sentinel rows; **compliant placeholder template** (unsubscribe+address tokens) | B7, B1 | 4 campaigns + legacy + '' seeded with exact boosts; template carries unsubscribe+address | S |
| **B11** | CSV→Postgres seed importer (dry-run + reconciliation): populate practice_group→prospect→lead(legacy campaign) | B7, B6, B2, **B10** | §8-E pass; reconciliation report produced; FKs satisfied | L |
| **B12** | Seed-only importer upsert path (+ clobber-guard test per actual table) | B11 | UPDATE SET excludes state cols; §8-E guard asserts | M |
| **B13** | Publish payload/client extraction (`_profile_draft_row`, `_publish_payload`, injected client; drop `_update_self`) | B1, C1 | Payload builds from Postgres rows; client targets endpoint, non-blocking | M |
| **B14** | Dashboard backend (approval queue, health, kill-switch endpoints; app_user-attributed) | B7, B9, C1 | Endpoints serve; kill toggles Gate | M |
| **B15** | Dashboard UI skeleton (queue/health/kill views) | B14 | Renders empty queue + health; owner+backup access | M |
| **B16** | Wire DoD checklist; end-to-end dry run on a clean DB | B5,B11,B12,B13,B15,C3,C4 | All §0 DoD boxes checked | M |

**Critical path:** C2→B0→B1→{B2,B3}→B5 (parity) and B7→B8→B10→B11→B12. Dashboard (B14/B15), publish (B13), ops (A1–A4), and platform floor (C1/C3/C4) parallelize off the critical path. **B11 depends on B10** (campaign FK target) — the previously-missing edge is now explicit.

---

## 10. Risks & still-needed inputs for Phase 0

**Risks (engineering):**
- **Parity drift on truncated/joined fields.** `priority_reason` surfaces only `reasons[:3]` (L1368/1371/1374) and joins differ (`'; '` vs `,` vs ` | `). Golden tests must assert the *exact* strings, and `workflow_score.full_priority_reasons[]` captures the untruncated list for audit so the lossy CSV form isn't the system of record.
- **Seed-upsert clobber.** The monolith preserves `activation_status` only by convention (`_merge_status_rows` L1476 is last-write-wins-by-NPI). A naive ORM upsert that includes state columns in SET **will** stomp live state. Mitigated by B12's explicit-exclusion test (targeting the actual table each column lives on) — the single highest-consequence Phase-0 bug.
- **Practice-group fragility.** `_practice_group_key` (L1282) includes `address_2` (suite line) and does no address standardization → over-fragments groups → undercounts `practice_group_size` → mis-routes the **high-value gate** (`high AND size≥3`, decision #3). Preserve behavior for parity; flag for a precision pass before autonomy widens. `practice_group_id` is sha1[:10] (10 hex) — fine at current volume, widen before it becomes a hard FK at scale.
- **Deliverability-blind scoring carried forward intentionally.** `+25` for merely *having* a phone (L1332) is the biggest weight and there is **no email field anywhere** in `DoctorRecord`/NPPES. Phase 0 ships this rubric *as-is* (parity first); the `ScoringFeatures` hook and `contact.email_status` column exist so Phase 1 enrichment can re-weight without a rewrite.
- **CAN-SPAM copy must never leak in.** `_rox_editable_drafts` (L1552-1582) has no unsubscribe, no address, constant subject (L1570), and falls back to `profile_url` on `certumalink.com` (firewall leak). Keep it as **reference only**; the seeded `template` (B10) must carry unsubscribe + postal address + a real `claim_url`. No send path exists in Phase 0; the Gate stub conceptually `BLOCK`s on CAN-SPAM incompleteness.
- **Unbuilt platform endpoints (decision #7).** `_claim_urls_by_npi` (L1242) expects `results[].claim_url` from an endpoint that doesn't exist. Postgres is source of truth; `lead.claim_url` is populated and status POLLED. `lead.last_polled_at`/`activation_detected_at` columns are reserved now; the poller (Phase 1) is the `actor` for `interested → physician_activated`, and the polled `activated` event uses a deterministic `event.dedup_key` so re-polling can't re-fire the terminal transition. The publish client targets the future endpoint but must not block Phase 0. **`_update_self` remote-exec dropped entirely** (supply-chain risk).
- **Postgres is the sole source of truth (decision #7).** This elevates DB durability to existential, especially `audit_log` (the CAN-SPAM legal defense). Mitigated by C4 (automated backups + PITR, forward-fix-only migration policy in prod).

**Still-needed inputs (must be supplied to finish Phase 0):**
1. **Cold domain name** (e.g. getcertuma.com) — blocks A1–A4.
2. **Cold-tolerant ESP + registrar accounts** (ownership, billing, creds) — blocks A1.
3. **Named accountable employee + title** under whom mailboxes are created and to whom escalations route (decisions #5, #9) — blocks A3, B14.
4. **Dashboard owner + backup approver** identities (decision #9) — blocks B15 access + `app_user` seed.
5. **Postgres instance** (managed or local) connection target for app + CI, plus the **secret store** choice and a **separate cold-ESP cred store** (C1) and backup/PITR provider (C4).
6. **Warmup cap schedule / provider warmup policy** — needed to seed the future Gate cap check (A4).
7. **Forensic-report (`ruf`) retention policy** for DMARC `fo=1` (A2) — decide what is stored and for how long.

*(The legacy single-stream campaign question from earlier drafts is now resolved in §5: all legacy `activation_status.csv` rows are assigned the seeded `legacy` campaign, so no open input remains there.)*

---

## 11. Secrets, durability & observability (operational floor)

Because Certuma's Postgres is the sole source of truth and the cold-ESP account must be firewalled from corporate, the following are first-class Phase-0 deliverables, not afterthoughts.

**Secrets/config (C1).** `certuma/config.py` exposes a `Settings` object (pydantic-settings) that reads the Postgres DSN, publish base URL/Bearer token, and ESP API key from environment/secret manager — never from repo files, never via scattered `os.environ` calls in business logic. Two separate stores: corporate app secrets (DSN, publish token) and **cold-ESP secrets** (ESP API key, mailbox creds, DMARC mailbox) in an isolated store with separate access, satisfying decision #1's firewall as a security control. CI uses ephemeral test creds only.

**Durability (C4).** Managed Postgres with automated daily backups + point-in-time recovery (WAL retention). Migration policy: **forward-fix only in production** — `downgrade` is a dev/test convenience (exercised by §8-F on throwaway DBs) and is never run against prod, since it destroys data including `audit_log`. A documented restore runbook (restore-to-timestamp) accompanies the backup config.

**Observability (C3).** `certuma/observability.py` provides structured (JSON) logging and a metrics sink (Prometheus client or logged counters). Minimum emitted signals: importer run (records in/out, skip histogram), migration reconciliation (counts, abort-on-unknown), ledger transitions (by old→new, actor, reason_code), Gate decisions (ALLOW/HOLD/BLOCK by reason_code), and kill-switch toggles. The `audit_log` table is the legal/forensic record; these logs/metrics are the *monitored* layer on top of it, so the two failure modes that matter most — silently clobbered live state and silently corrupted activation metric — surface as observable signals, not just rows nobody reads.

**Local dev / CI (C2).** `docker-compose.yml` stands up Postgres 15 with the `citext`-capable role; `make db-up`, `make db-seed`, `make test` give any developer a working DB and the same service CI runs against, removing the "how do I get a DB" gap for two new packages and 15 tables.