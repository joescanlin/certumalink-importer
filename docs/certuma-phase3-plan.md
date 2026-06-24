# Certuma Reach - Phase 3 plan (parity: intelligence, evidence, engagement)

Status: APPROVED (2026-06-24). Grounded in the merged Phase 0-2 code. Closes the gaps between what we
shipped and the Rox proposal so Certuma Reach reaches capability parity. The recommended default was
confirmed for every **[DECISION]**:
1. Warehouse target = Postgres `reporting` schema + parquet/DuckDB data-room export now; external
   warehouse (Snowflake/BigQuery) behind the WarehouseExporter seam later.
2. Learning loop = measure + surface the winning variant for operator promotion; per-campaign
   auto-promote flag defaults OFF.
3. Multi-channel = build the Channel abstraction + a stub LinkedIn channel now; real LinkedIn
   deferred behind the seam.
4. Auth = add minimal authentication + operator/leadership roles + a leadership view now.

The evidence and intelligence layers are the critical path.

## Why Phase 3

Phases 0-2 built a strong autonomous send-and-reply ENGINE (enrich -> draft -> gate -> send ->
cadence -> classify -> escalate -> activate, on an autopilot tick). The Rox proposal sells two
layers we have barely touched, plus parity items around them:

- the **evidence layer** (warehouse-native governed data + Customer-Intelligence analytics for the
  Series A data room) - Rox's #1 selling point, our biggest hole;
- the **intelligence layer** (a rich per-clinician knowledge graph + trigger-signal scoring +
  recommended actions + a learning loop that adapts to outcomes);
- **engagement signals** (open tracking, went-quiet re-engagement, churn risk), **multi-channel**
  (email + LinkedIn), **real send infra at scale**, and **auth/RBAC + leadership visibility**.

Phase 3 closes all of these. Per the stakeholder steer, the **evidence and intelligence layers are
the critical path**; the rest bring full parity.

## Architecture stance (unchanged from Phase 1-2)

Operational truth stays in the Postgres operational schema (the single audited ledger). Analytics is
a SEPARATE read-optimized layer fed by an ELT, never a second writer of operational state. Every
external dependency (warehouse target, enrichment/signal vendors, LinkedIn, ESP, auth provider) sits
behind a clean interface with a deterministic stub now and a real adapter at the infra cutover -
the same stub-and-seam discipline that made Phases 1-2 testable against Mailpit.

## Sub-steps

### Evidence + analytics layer (CRITICAL)

- **P3.0 analytics schema + ELT foundation.** A `reporting` schema of conformed facts + dimensions
  built from the operational tables: `dim_clinician` (npi, specialty, region, group size, signals),
  `dim_campaign`, `dim_specialty`, `dim_date`, `fact_touch` (one row per outbound send: channel,
  variant, cadence_step, cost), `fact_event` (delivered/opened/replied/bounced/activated), and a
  `fact_lead_funnel` snapshot. A deterministic `rebuild(session, as_of)` materializes them (Postgres
  materialized views or insert-select), suppression-aware. A `WarehouseExporter` interface targets
  Postgres now and an external warehouse / parquet later. **[DECISION 1 - warehouse target]**
  *Recommended:* Postgres `reporting` schema + a parquet/DuckDB export now, external warehouse
  (Snowflake/BigQuery) behind the exporter seam later. *Alternative:* wire a real warehouse now
  (procurement + infra).

- **P3.1 Customer Intelligence analytics + dashboard.** The queries the proposal sells: full funnel
  (universe -> enriched -> sent -> delivered -> opened -> replied -> activated) sliced by specialty
  / region / campaign / cohort; open-rate, reply-rate, activation-rate, time-to-activation;
  cohort-over-time conversion. A new dashboard **Analytics** screen (replaces the basic Activity
  funnel) renders Customer Intelligence: which specialties/regions/campaigns convert, trend lines,
  cohort tables. All read-only over the `reporting` schema.

- **P3.2 evidence / data-room export.** A governed export of the analytics dataset (CSV + parquet)
  for Series A diligence: conversion by specialty, retention, growth-efficiency, unit economics
  (needs a cost model - per-send + per-enrichment cost columns on `fact_touch`). Suppression / opt-out
  aware (no exporting PII for opted-out clinicians beyond the suppression record itself). A
  `make evidence` target.

### Intelligence layer (CRITICAL)

- **P3.3 clinician knowledge graph + rich enrichment.** Extend enrichment beyond a contact to
  SIGNALS: a `clinician_signal` table (npi, signal_type, value, source, observed_at, confidence) and
  a `SignalProvider` interface returning license, specialty board, region, practice group size, and
  public activity signals - plus EHR / panel-size / message-burden behind the vendor seam. Stub
  providers wire the free/public signals now; paid signals (EHR/panel) stay stubbed. This is the
  "knowledge graph + Clever Columns" the proposal pitches.

- **P3.4 trigger-signal scoring + Recommended Actions.** Extend `certuma_core/scoring.py` to fold the
  P3.3 signals + recency-decayed trigger signals into the fit/priority score (today it is only
  profile-completeness + group size). A `recommend_action(lead, signals)` engine produces the
  next-best-action (enrich / send / follow-up / re-engage / escalate / wait) surfaced on the
  dashboard - the proposal's "Recommended Actions." Scoring stays pure + deterministic.

### Engagement + learning (HIGH)

- **P3.5 open tracking + engagement events.** The `opened` event_type already exists; add a tracking
  seam (open-pixel / ESP open webhook -> monitor.ingest_event 'opened', deduped) and an engagement
  rollup per lead (opens, last_open_at). Open data is treated as a WEAK signal (see latent issue 1).

- **P3.6 engagement plays: re-engage + churn risk.** Cadence becomes engagement-aware: opened-no-reply
  -> a reply-bump touch; a lead that engaged then went quiet past a window -> a re-engagement play
  ("Usage Signal Plays"); a post-activation disengagement flag -> Churn Risk surfaced on the
  dashboard. Time-based cadence (P2.4) stays as the floor.

- **P3.7 learning loop / variant optimization.** Use the existing `template.variant_label` /
  `message.variant_id` seam: assign variants per touch, attribute outcomes (reply, activation) to the
  variant under a chosen attribution model, and select winners. **[DECISION 2 - learning autonomy]**
  *Recommended:* measure + surface the winning variant for the operator to promote, with an optional
  per-campaign auto-promote flag (default off). *Alternative:* full auto-optimization (bandit) by
  default.

### Parity supporting pillars

- **P3.8 multi-channel.** Generalize the email-only sender into a channel-agnostic `Channel`
  interface (email today) and add a LinkedIn channel behind it; cadence sequences across channels
  (email + LinkedIn + reply-bump). **[DECISION 3 - multi-channel scope]** *Recommended:* build the
  channel abstraction + a stub LinkedIn channel now (so nothing is email-hardcoded); the real
  LinkedIn integration (gated, ToS-sensitive) is deferred. *Alternative:* stay email-only, defer the
  abstraction entirely.

- **P3.9 auth + RBAC + leadership visibility.** Real authentication for the dashboard and an operator
  / leadership role model (the proposal's "role-based access" + "Leadership Visibility"); a
  leadership analytics view; access logging toward a SOC 2 posture (the rest of SOC 2 is process,
  not code). **[DECISION 4 - auth now]** *Recommended:* add minimal auth + operator/leadership roles
  now, since more eyes will be on the tool and leadership visibility is an explicit parity item.
  *Alternative:* keep it internal/unauthenticated for now.

- **P3.10 real send infra cutover (the Phase 2 P2.9 carryover).** Real `EspProvider` + warmup ramp
  across the mailbox roster + inbound IMAP/ESP adapter + cold domain / sender identity. Code seams
  are built; the cutover is stakeholder-gated infra.

- **P3.11 parity demo + evidence.** End to end: enrich-with-signals -> score + recommend ->
  multi-touch autopilot -> open/reply engagement -> learning picks a winner -> activate -> the
  Customer-Intelligence analytics + data-room export reflect it. A deterministic e2e test + a live
  run.

## Critical path

Evidence: **P3.0 -> P3.1 -> P3.2**. Intelligence: **P3.0/P3.3 -> P3.4**. Engagement+learning:
**P3.5 -> P3.6 -> P3.7**. These three tracks are the priority and largely parallel after P3.0/P3.3.
Multi-channel (P3.8), auth (P3.9), and infra (P3.10) are independent. P3.11 is the capstone.

## Latent issues this plan surfaces (before they bite)

1. **Open data is unreliable.** Apple Mail Privacy Protection (and corporate proxies) pre-fetch
   pixels, inflating opens. The learning loop and scoring must weight replies/activations far above
   opens, and "opened" must be a soft signal, never a hard trigger for a high-stakes action.
2. **Warehouse PII governance.** The evidence layer exports clinician contact + reply data. It MUST
   inherit suppression/opt-out (an opted-out clinician's PII is not re-exported or retained beyond
   the suppression record) and sit behind RBAC - otherwise the Series-A data room becomes a
   compliance liability. Governance is a P3.0/P3.2/P3.9 cross-cut, not an afterthought.
3. **Outcome attribution is ambiguous.** In a multi-touch, multi-channel cadence, which touch/variant
   gets credit for an activation? The learning loop is meaningless without an explicit attribution
   model (recommend last-touch-before-activation for v1, with the data retained to revisit).
4. **Signal freshness/decay.** Trigger signals (went-quiet, recent activity) are time-sensitive;
   stale signals mislead scoring. Signals carry observed_at and the score applies recency decay.
5. **Specialty breadth multiplies the compliance surface.** Per-specialty content (48 voices) still
   has to pass the linter + CAN-SPAM gate; more templates = more places a banned claim can slip in.
   Every specialty variant goes through the existing approval/lint flow - no bypass.

## What is explicitly NOT in scope (sales positioning, not capability)

The Rox-specific service wrapper (Forward-Deployed Engineers, a Growth Strategist, "proven at
MongoDB/Ramp/Nasdaq," investor logos, the 750+ customer base) is positioning for a vendor sale, not
capability to clone. Certuma is building this in-house; the deliverable is the platform parity above.
