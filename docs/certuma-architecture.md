# Certuma — Agentic Salesforce Architecture Brief

> **Status:** Design (no implementation yet). Email-first, end-to-end autonomous outbound salesforce built on the Certumalink Doctor Importer seed data.
> **Provenance:** Synthesized from a 4-architecture design panel (agent-loop-first, event-driven/durable, compliance-&-deliverability-first, pragmatic-MVP) scored by a diverse judge panel. All four converged on the same spine; the two production-killing flaws the judges flagged (cold-email ESP incompatibility, LLM-gated opt-out) are designed out below.
> **Open decisions** are tracked at the bottom and resolved interactively with the stakeholder.

---

## Recommended architecture (the thesis)

Build Certuma as a **deterministic event-driven Orchestrator (plain Python + Postgres + durable queue) that owns the per-physician state machine, with Claude confined to four bounded, stateless nodes** (draft, classify-reply, objection-draft, score-calibration-proposal) that can *propose* but never *act*. Every outbound message — first touch, follow-up, or auto-reply — funnels through a **single non-bypassable Compliance & Deliverability Gate** that returns `ALLOW / HOLD / BLOCK + reason_code` after checking suppression, an explicit idempotency key, a real transition graph, CAN-SPAM completeness, quiet hours, and reputation circuit breakers. The model proposes; the harness disposes.

The sales lead **approves the *shape* (campaign launch + each template once) and the *exceptions* (low-confidence / high-value / escalations); Certuma runs the *volume* autonomously**, with a per-campaign autonomy dial (Assisted → Supervised → Autonomous) so the leash loosens as outcomes prove out. We reuse the importer spine verbatim as the cold-start prioritizer and system-of-record seed.

**Two flaws are fatal-if-ignored and are designed out from day one:**
1. Amazon SES / Postmark will terminate cold outreach and can cascade-kill Certumalink's transactional email — so we send on isolated cold-tolerant infrastructure on a separate domain, never the corporate domain or a shared ESP account.
2. Opt-out detection must never depend on an LLM confidence score — a deterministic keyword/regex pre-filter forces suppression *before* the classifier runs.

---

## The Certuma agent loop, end to end

The loop is **code-driven**. Claude is invoked inside named nodes with a serialized thread + structured facts assembled from Postgres; every call is stateless, schema-validated, replayable, and logged (`prompt_hash`, `model`, `confidence`, tokens). The Orchestrator advances a durable per-lead workflow.

**States** (the extended ledger): `not_contacted → enriching → sendable / unfindable → queued → awaiting_send_window → email_sent → (delivered/bounced) → awaiting_reply → replied → [classify] → interested / objection_in_progress / not_now / wrong_person → physician_activated (terminal) | do_not_contact (terminal) | needs_review | exhausted (terminal)`.

**The lifecycle, step by step:**

1. **Discover & score** (`PROSPECTOR`, no LLM). Existing importer: ZIP → NPPES → NPI-keyed `DoctorRecord`. `_workflow_fields` computes `activation_score` / `activation_priority` / `priority_reason` / `profile_completeness_score`; `practice_group_id` / `other_doctors_at_location` set account context. Persisted to `prospect` + `workflow_score` (time-series, versioned).

2. **Enrich email** (`ENRICHER`, no LLM — hard prerequisite gate). NPPES has **no email** (the live `output/live-78701.csv` has no email column and shared switchboard `512-324-7000`), so this is mandatory. Provider waterfall → **mandatory verification** → `contact` row with `verification_status` + `confidence`. `valid` → sendable; `risky/catch-all/unknown` → held/gated; `invalid` → discarded; nothing findable → `unfindable`, parked in `needs_review`, **never guessed-and-blasted**. Enrich high-`activation_priority` NPIs first to cap spend.

3. **Publish private draft** (`PROSPECTOR`). `_publish_to_certumalink` → `POST /api/admin/imports/physician-profiles` (NPI-upsert, **private draft**), capture per-NPI `claim_url` — the single CTA and conversion target.

4. **Draft** (`COPYWRITER` — Claude reasons here). Given an *approved* template + physician facts + campaign `pitch_angle` + `claim_url`, emits strict JSON `{subject, body, plaintext, variant_id, merge_token_audit}`. A deterministic **linter** rejects: missing unsubscribe token, missing physical address, banned claims ("verified/board-certified/credentialed"), or any fact not in the seed record (hallucination guard). Refuses if a required merge token is missing.

5. **Gate** (deterministic, non-bypassable). The drafted touch hits the Compliance & Deliverability Gate. `BLOCK` if suppressed / CAN-SPAM-incomplete / banned-claim. `HOLD` if outside quiet hours, over warmup cap, or a circuit breaker is tripped → durably re-queued. `ALLOW` only if an **idempotency key** `(npi, campaign, cadence_step)` is not already used.

6. **Send** (`SENDER`, no LLM). Acquires a per-lead advisory lock (`SELECT … FOR UPDATE SKIP LOCKED`), writes the `message` row + idempotency key **before** the ESP call, injects `List-Unsubscribe` (RFC 8058 one-click) + physical address + plus-addressed `Reply-To` (`reply+<thread_id>@…`), sends, records `esp_message_id`. Transition `email_sent` via the **only** ledger writer.

7. **Monitor** (`EVENT INGEST`, no LLM). ESP webhooks (delivered/open/bounce/complaint) are deduped by `esp_event_id` (at-least-once, out-of-order safe), drive transitions, feed circuit breakers, and hard-bounce → instant suppress + re-enrich flag.

8. **Reply → intent** (`REPLY-HANDLER` — Claude reasons here, *behind a deterministic pre-filter*). Inbound threaded by plus-addressed `Reply-To` token (primary) → `In-Reply-To`/`References` (secondary) → fuzzy from-address (tertiary) → unmatched-triage queue. **Before** Claude runs, a deterministic opt-out regex (`unsubscribe|stop|remove me|opt out|take me off|do not (email|contact)`) forces suppression. Then Claude classifies into a fixed enum `{interested, question, objection, not_now, wrong_person, opt_out, auto_reply_OOO, angry_legal}` with confidence, grounded only in an approved FAQ/objection store (RAG) to prevent claims about an uncredentialed platform.

9. **Respond** (closes the loop). `opt_out / angry_legal / wrong_person` → **deterministic handlers**, never free-generation (opt_out → suppress + `do_not_contact`; angry_legal → escalate). `interested` → autonomous reply with `claim_url` + activation nudge (re-passes the Gate). `objection` ≥ confidence threshold + grounded → autonomous objection reply; else escalate. The conversation is **locked** so the Scheduler can't fire a follow-up while a reply is being handled (kills the "day-3 follow-up after they already said yes" race).

10. **Cadence & stop** (`SCHEDULER`, no LLM). Computes `next_action_at`. **Explicit stop conditions:** activated · opted-out · hard-bounced · `max_touches` (e.g. 4) · `no_engagement_after_K` · wrong_person · needs_review. Exhaustion → `exhausted` (terminal), not infinite dribble.

11. **Activate = convert.** `claim_url` click → activation webhook → `physician_activated` (terminal, authoritative). This is the conversion event, the learning ground-truth, and a stop condition.

---

## Gated vs. autonomous (the approval boundary)

**Principle: the lead approves the *shape* and the *exceptions*; Certuma runs the *volume*.** The boundary is a deterministic Policy/Gating Engine evaluated *after* the Gate — never by the model.

| Decision | Default | Configurable? |
|---|---|---|
| **Campaign launch** (targeting/ZIPs/cap) | **GATED** | Always gated (safety floor) |
| **New/edited template or variant** | **GATED** | Always gated (safety floor) |
| **First sends on a freshly-warmed domain** | **GATED** | Always gated (safety floor) |
| Send to a `risky`/catch-all enrichment email | **GATED** | Toggle: gate / hard-block |
| Reply classified `angry_legal` or ambiguous opt-out | **GATED** | Always gated (safety floor) |
| Reply classified low-confidence (< threshold) | **GATED** | Slider: confidence floor (default 0.8) |
| Send/reply to a **high-value** lead | **GATED** | Rule: `activation_priority=high` AND `practice_group_size ≥ N` |
| Enrichment, scoring, publish private draft | **AUTONOMOUS** | — |
| Draft from approved template (per-physician) | **AUTONOMOUS** | — |
| Send approved template, standard tier, in-cap, in-quiet-hours | **AUTONOMOUS** | — |
| Reply classification | **AUTONOMOUS** | — |
| Grounded reply to interested/question/objection ≥ threshold | **AUTONOMOUS** | Autonomy level L0/L1/L2 |
| Follow-up cadence, stop conditions | **AUTONOMOUS** | — |
| **Opt-out / suppression / hard-bounce** | **AUTONOMOUS, INSTANT, NEVER GATED** | Non-overridable |

**Mechanism:** each pending action emits `proposed_action{type, value_tier, model_confidence, policy_matches}`; the engine maps to *auto-execute* or *enqueue-for-approval*. **Three dials per campaign:** (1) value-tier threshold, (2) reply-confidence floor, (3) **autonomy level — Assisted** (approve every send) → **Supervised** (approve first-touch + high-value + low-confidence; *default*) → **Autonomous** (approve only escalations + new templates). Dial changes affect only *future* actions; in-flight approvals are untouched. SLA timers on gated items; SLA-expiry behavior **defaults to HOLD** (an absent lead must never silently auto-ship). Global **kill switch** + per-campaign pause read by the Gate before every send.

---

## The Certuma sales dashboard

- **Campaign console** — launch from `CAMPAIGN_PRESETS` (primary-care / dermatology / cardiology / urgent-care), set ZIPs, daily cap, autonomy level + thresholds. The **Launch button is the campaign gate**.
- **Template studio** — Claude-drafted subject/body variants side-by-side, edit/approve/reject, with merge-token audit, banned-claims lint results, and a live preview against a sample physician. Approval is the copy gate.
- **Approval queue** — gated sends/replies with physician context, proposed message, intent + confidence, gate `reason_code`; one-click Approve / Edit-and-send / Reject.
- **Escalations inbox** — `angry_legal`, wrong-person, unfindable-but-high-value, new-objection.
- **Conversation inbox** — threaded transcript per physician, Claude's intent + confidence, inline override before send.
- **Pipeline view** — physicians by `activation_status` (live, auto-transitioning) with per-touch history.
- **Funnel** — enriched → verified → sent → delivered → opened → replied → interested → activated (`claim_url` click), by campaign and variant.
- **Deliverability panel** — bounce rate, complaint rate, DKIM/DMARC health, warmup progress, **circuit-breaker state**, pacing vs cap.
- **Compliance panel** — suppression list (search/add/export), opt-out log, audit-trail export, quiet-hours config.
- **Learning panel** — variant win-rates; current `_workflow_fields` weights with **proposed adjustments awaiting lead approval** (never auto-mutating).
- **Global kill switch + per-campaign pause**, prominently placed.

---

## Data & state model

**Postgres replaces every CSV** (`profile_drafts.csv`, `rox_outreach.csv`, `rox_today.csv`, `activation_status.csv`). NPI stays the universal key. Core entities:

- **`prospect`** — NPI-keyed seed: `display_name`, `credential`, `primary_specialty`, `primary_taxonomy_code`, practice address/phone, `practice_group_id`, `other_doctors_at_location`, `claim_url`, `profile_id`.
- **`workflow_score`** — persisted `_workflow_fields` output as **time-series** with `model_version` (today it's recomputed and thrown away).
- **`contact`** — the missing email layer: `email`, `email_source`, `verification_status`, `confidence`, `is_role_address`, `unfindable`. 0..n candidates per NPI.
- **`lead`** — live state machine: `activation_status`, `campaign`, `cadence_step`, `next_action_at`, `stop_reason`, `owner`, `version` (optimistic concurrency).
- **`thread` / `message`** — conversation state + per-touch history (today: only `last_seen_at`). `message` carries `esp_message_id`, `variant_id`, `body_rendered`, idempotency key, delivery/bounce/complaint flags.
- **`event`** — normalized delivered/opened/replied/bounced/complained/activated/opt_out feed, deduped by `esp_event_id`.
- **`suppression`** — keyed by **both `email` AND `npi`**, with `reason`; checked before every send; never deleted.
- **`template`** — versioned, `approval_status`, `approved_by`.
- **`approval`** + **`audit_log`** (append-only, retains rendered bodies) — CAN-SPAM defensibility.

**Migration off the CSV:** reuse the importer's own `_merge_status_rows` / `_normalize_activation_status` / `LEGACY_ACTIVATION_STATUS_MAP` as a **one-time seed importer**, run dry-run with a reconciliation report, keep the CSV as immutable backup. **Critical fix:** today `VALID_ACTIVATION_STATUSES` (line 37) is a flat *set* — there is no transition graph. We add an explicit **`ALLOWED_TRANSITIONS` table** (terminal states; no backward `physician_activated → email_sent`), enforced inside the single ledger-writer. And the nightly importer re-run must **upsert seed columns only** — never stomp `activation_status` / `next_touch_at` / `email_status` mid-conversation (today's `_merge_status_rows` is last-write-wins-by-NPI and would clobber live state).

---

## Email stack & deliverability

**ESP — fatal flaw #1, fixed.** Amazon SES and Postmark **both prohibit/terminate cold unsolicited outreach**, and an SES suspension can cascade to *all* Certumalink transactional email if the account is shared. So:
- **Send cold outreach on isolated, cold-tolerant infrastructure** — a dedicated cold-outreach ESP/MTA pool (e.g. Instantly/Smartlead-class managed cold infra, or a self-managed Postmaster setup), on a **separate sending domain**, with its **own account**, **never** sharing the corporate domain or the platform's transactional sender.
- Keep an **`EmailProvider` adapter interface** so the cold-send provider and the (separate) transactional provider are swappable, and so SMS/voice attach later as sibling adapters.

**Topology for cold volume:** plan for **multiple sending domains + many low-volume mailboxes with per-mailbox daily caps** (tens of sends/mailbox), not one subdomain with a global cap. Full **SPF + DKIM + DMARC** (p=none → quarantine → reject) with aligned MAIL FROM, custom Return-Path.

**Warmup is reputation-based, not calendar-based** (start ~50/day, ramp on engagement). **Go/no-go ramp gates:** don't increase volume unless complaint < target and bounce < target and seed/inbox-placement tests pass (Google Postmaster Tools / GlockApps).

**Inbound/reply handling:** provider inbound-parse webhook → match by plus-addressed `Reply-To` token (primary), `In-Reply-To`/`References` (secondary), fuzzy from-address (tertiary), unmatched → triage queue. Quoted history stripped before classification.

**Throttling & circuit breakers (inside the Gate, on rolling real-time windows):** per-recipient-domain and per-tenant pacing (physician mail clusters behind Proofpoint/Mimecast/EOP — one bad batch tenant-blocks you), randomized intra-day pacing, and **auto-pause** when complaint > ~0.1% or bounce > ~2%. A complaint-rate breaker is the control that actually saves the domain — a human kill switch is not a substitute.

---

## Email enrichment

NPPES exposes **no email** and often only a shared switchboard, so enrichment is a hard prerequisite. **Verification-first waterfall behind one adapter:**

1. **Discovery** — healthcare-specialized NPI→email source first (Definitive/IQVIA-class), then general B2B (Apollo/Hunter/People Data Labs) keyed on name + practice domain; pattern-guess (`first.last@domain`) only as a low-confidence last resort.
2. **Mandatory verification** — every candidate through ZeroBounce/NeverBounce/Kickbox-class (SMTP+MX+catch-all+role detection). Only `valid` (and policy-permitting `risky`) is sendable. **Reputation beats coverage** — prefer cheap verification spend over speculative discovery volume; never send guessed addresses.

**Reality check to price in:** individual-physician verified-email coverage is realistically **low (often well under half, possibly low-teens)**; physician/practice domains are heavily **catch-all**, so verification returns `unknown` for a large share. Plan the funnel math (low coverage × warmup cap × gated approval) accordingly — the sendable pool is a *fraction* of the import. Cache every verdict by NPI (pay once), allocate enrichment budget by `activation_priority`, hard-bounce → suppress + re-enrich + downgrade the source provider's trust weight. `is_role_address` (info@/office@) → office/account-based variant, not a personal pitch.

---

## Compliance & safety floor

CAN-SPAM is **enforced structurally in the deterministic `SENDER`/Gate, never trusted to the LLM** — necessary because today's Rox `email_body_draft` (line 1571) ships with **no unsubscribe link and no physical address** and a constant subject (line 1570): non-compliant by construction. Every send guarantees: accurate non-deceptive From/Reply-To on the cold domain; truthful subject (banned-claims linter blocks credential/clinical claims — data is provider-self-reported/uncredentialed, so copy says "a *draft* profile we prepared," never implies endorsement); valid **physical postal address** in the footer; working **RFC 8058 one-click `List-Unsubscribe`** + visible link, honored in minutes.

- **Dual-path, classifier-independent opt-out (fatal flaw #2, fixed):** (a) unsubscribe click → deterministic suppression; (b) deterministic **opt-out regex pre-filter on every inbound *before* Claude** → forces suppression. The LLM may only *add* suppressions, never override/downgrade an opt-out. Suppression checked **first** on every send, by email AND NPI.
- **Private-draft posture:** profiles publish as private drafts (the API contract already mandates private default + `ready_for_rox` lifecycle); draft→patient-visible is a separate human gate.
- **Kill switch** (global + per-campaign + per-domain) read before every send.
- **Audit:** append-only `audit_log` + retained rendered bodies = exportable CAN-SPAM/AG-inquiry evidence.
- **Per-state / jurisdiction:** encode a `practice_state` rule table (e.g. CA Bus. & Prof. 17529.5 has a private right of action; CASL applies to any Canadian-located recipient). Treat "cold B2B is fine under CAN-SPAM" as the *federal floor only*, pending legal sign-off.

---

## Learning loop

Outcomes are first-class `event`s attributed `message → variant → physician`. Three feedback paths, all **human-approved before behavior changes**:

1. **Copy** — template variants compete on reply-rate and activation-rate (`claim_url` click) with significance gating; losers retired; Claude generates challengers seeded by winners; **lead approves challengers**.
2. **Scoring** — persist `_workflow_fields` output, join to activation outcomes, periodically **propose** re-weighted coefficients. **Correction:** the current rubric is *deliverability-blind* — its single largest weight is `+25` for *having a practice phone* (line 1332), which doesn't predict email reachability. First learning priority is adding email-deliverability/engagement features; until then, prioritize the send queue with a deliverability-aware override, not raw `activation_score`.
3. **Targeting** — campaigns/specialties/ZIPs with high activation yield get budget + enrichment priority. Reply classifications sampled by LLM-as-judge + human spot-checks → prompt/few-shot updates + grown FAQ store.

**Caveat:** at cold-start volume × low coverage, meaningful learning is *weeks-to-months* out. Ship the frozen rubric on day one.

---

## Reuse of the existing importer spine

**Keep verbatim:** the ZIP→NPPES→`DoctorRecord` pipeline (becomes `PROSPECTOR`); `_workflow_fields` scoring as cold-start prioritizer + enrichment-budget allocator; `CAMPAIGN_PRESETS` (targeting + Copywriter `pitch_angle` + dashboard console); `practice_group_id`/`other_doctors_at_location` for account-based outreach; `_publish_to_certumalink` → `POST /api/admin/imports/physician-profiles` (NPI-upsert, private drafts) + `claim_url` capture; the 9 `VALID_ACTIVATION_STATUSES` + `LEGACY_ACTIVATION_STATUS_MAP` as the migration seed.

**Extend:** the flat status *set* → a real `ALLOWED_TRANSITIONS` graph; `_merge_status_rows` → seed-column-only upsert (no live-state clobber); throw-away score → versioned `workflow_score`; the three contract-only-but-unbuilt endpoints (**read/query API, status write-back, activation webhook**) — the API doc's lifecycle map proves write-back is expected.

**Replace:** the static `_rox_editable_drafts` generator (constant subject, 3 tokens, no unsubscribe/address, hardcoded "Rox" persona) → the `COPYWRITER` node + governed `template` assets, re-voiced Rox→Certuma. Its tokens (`last_name`, `pitch_angle`, `city`, `claim_url`) become the Copywriter's required merge tokens.

**Honest scope note:** all load-bearing logic (`_workflow_fields`, `CAMPAIGN_PRESETS`, ledger merge, Rox tokens) lives **only in the 1,827-line `portable/certumalink-doctor-import.py` monolith**, *not* the leaner `src/certumalink_importer` package (fetch/normalize/export only). "Reuse as a library" requires **first extracting** scoring/ledger/presets out of the monolith — budget that as real Phase 0 work.

---

## Phased build plan

- **Phase 0 — Spine + isolation foundations (wks 1–2).** Postgres + schema + Alembic; extract scoring/ledger/presets from the monolith into a callable library; migrate `activation_status.csv` (dry-run + reconciliation); suppression + audit + event tables; `ALLOWED_TRANSITIONS` + single ledger-writer + idempotency keys; stand up the **isolated cold-sending domain** (separate account, SPF/DKIM/DMARC) and **begin reputation-based warmup**. Thin dashboard skeleton (approval queue + kill switch). *No sends.*
- **Phase 1 — Enrichment + first lead-approved real send (wks 2–4).** Enrichment waterfall + mandatory verification; deterministic `SENDER` + Gate; `COPYWRITER` + linter; lead approves 1 campaign + 1 template; **first real cold sends, every one approved (Assisted)**, low warmup volume. Activation webhook → `physician_activated`.
- **Phase 2 — Inbound + reply autonomy (wks 4–6).** Inbound ingestion + threading + unmatched triage; deterministic opt-out pre-filter; `REPLY-HANDLER` classification + grounded replies; hard-routed opt-out/angry/legal; **Supervised becomes default**.
- **Phase 3 — Cadence + autonomy dial-up (wks 6–9).** Full cadence + explicit stop conditions; conversation locking; Policy/Gating Engine + the three dials + value-tier gating; **Autonomous level** available.
- **Phase 4 — Learning + scale (wks 9+).** Variant competition with Claude challengers; deliverability-aware score re-weighting (lead-approved); targeting reallocation; LLM-as-judge sampling; multi-domain/mailbox scale-out; SMS/voice adapter seams confirmed (stubbed).

**Explicitly deferred:** ML score re-fit, multi-armed bandit, LLM-as-judge, BIMI, dedicated-IP decisions, SMS/voice — none ship before there's send volume and labeled outcomes.

---

## Top risks & mitigations

| Risk | Mitigation |
|---|---|
| **Cold-tolerant ESP / domain isolation** — SES/Postmark terminate cold use and can cascade-kill transactional mail | Isolated cold-outreach provider + separate domain + separate account; corporate/transactional sender never touched; adapter interface |
| **Complaint-rate reputation collapse** (physicians are a high-complaint cold audience) | Real-time complaint-rate circuit breaker (auto-pause) inside the Gate; multi-domain/mailbox topology; ramp gates |
| **Low email coverage starves the funnel** | Verify-before-send, park unfindable, enrich high-priority first, cache by NPI, role-address office variant; realistic funnel expectations |
| **Double-send / race** | Idempotency key `(npi,campaign,step)` written *before* ESP call; `FOR UPDATE SKIP LOCKED` per-lead lock; event dedup by `esp_event_id`; conversation lock during reply handling |
| **Opt-out missed by classifier** | Deterministic regex opt-out pre-filter before the LLM + unsubscribe-click path; LLM can only add suppressions |
| **Illegal/oscillating state transitions** | `ALLOWED_TRANSITIONS` graph enforced by the single ledger-writer; terminal states |
| **LLM hallucinates platform claims** (uncredentialed data) | Banned-claims linter + RAG-grounded replies + facts-only-from-seed guard + LLM-as-judge sampling + private-draft framing |
| **Activation webhook (platform team) slips** | Poll `claim_url` status in the interim; degrade cadence stop to opens/replies; escalate as a *gating dependency* |
| **Importer re-run clobbers live state** | Seed-column-only upsert; never overwrite live conversation fields |

---

## Resolved decisions (stakeholder, 2026-06-23)

1. **Cold-sending infrastructure → Separate dedicated domain + cold-tolerant provider.** A brand-new domain (e.g. `getcertuma.com`), own registrar + ESP account, multi-mailbox reputation warmup, on Instantly/Smartlead-class managed cold infra. Fully firewalled from `certumalink.com` — a blocklisting can never touch platform/transactional mail. (A separate domain, **not** a subdomain, to avoid org-domain reputation bleed.)
2. **Enrichment send filter → Valid-only at launch.** Send only to verifier-confirmed `valid` addresses; park `risky`/`catch-all`/`unknown`, discard `invalid`. Verify-first waterfall (healthcare-specialized NPI→email source → general B2B → pattern-guess last resort), budget allocated by `activation_priority`. Loosen to risky-with-approval later once reputation is proven.
3. **Launch autonomy → Assisted first campaign, then Supervised default.** Campaign 1 (warmup): approve every send. Then Supervised becomes the standing default (approve first-touch + high-value + low-confidence). Autonomous is a Phase-3 per-campaign opt-in. Reply-confidence floor `0.8`; high-value = `activation_priority=high` AND `practice_group_size ≥ 3`.
4. **SLA-expiry → Hold + escalate.** Expired approval items stay held (never auto-send); after a second threshold they escalate (re-notify, surface, ping backup approver). Unreviewed cold email is never auto-shipped.
5. **Sender identity → Real accountable team member.** From/Reply-To = an actual Certumalink employee (name + title) on the cold domain; Certuma drafts/sends/replies under that identity; hard escalations route to that person. No fabricated persona. *Still needed before first send: footer postal address, approved uncredentialed-data disclaimer copy, and legal sign-off; encode per-state (CA 17529.5) + CASL rules as federal-floor-plus.*
6. **Conversion → `claim_url` click = `physician_activated`** is the single success metric, learning label, and terminal stop condition. `interested` replies are a leading indicator (raise priority, not success); interested-not-activated leads continue cadence toward activation; draft→patient-visible publishing is a separate human gate Certuma does not own.
7. **Platform endpoints → Decouple + poll interim.** Certuma's Postgres is the source of truth for outreach; it polls `claim_url`/activation status until a real webhook exists. The three endpoints (read/query, status write-back, activation webhook) are a parallel platform ask, **activation webhook first**. Certuma never blocks on the platform team. *Still needed: endpoint owner + rough timeline.*
8. **Model tiering → Haiku / Sonnet / Opus.** Haiku 4.5 for reply-intent classification (high volume, constrained enum); Sonnet 4.6 for first-touch drafting + routine replies; Opus 4.8 for template authoring + high-value/objection replies. Exact model IDs + per-token pricing confirmed against the Claude API reference at build time.
9. **Dashboard operating model → Named owner + backup.** One named sales lead owns the approval queue, SLA, campaign config, and autonomy dials; a designated backup covers absences and is the SLA escalation target (ties to decision #4).

### Still-needed inputs (not blockers to start Phase 0)
- Chosen cold-outreach **domain name** + provider account.
- **Legal:** footer postal address, uncredentialed-data disclaimer language, legal sign-off, per-state/CASL confirmation.
- **People:** the named From identity (employee), the dashboard primary owner, and the backup approver.
- **Platform:** owner + timeline for the three endpoints.
