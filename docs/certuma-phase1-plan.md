# Certuma Reach - Phase 1 Plan: The First Real Email Send Loop

Phase 1 builds the email send loop **on top of** the completed Phase 0 spine. It changes
nothing about the load-bearing invariants Phase 0 established - `certuma.ledger_writer.transition`
remains the only writer of `lead.activation_status`; `certuma.gate.evaluate` remains the single
non-bypassable chokepoint; `certuma_core.status.ALLOWED_TRANSITIONS` remains the legality graph;
the activation metric stays protected by `ACTIVATION_ONLY_ACTORS = {'poller','activation_webhook'}`.
Phase 1 **extends** those seams; it does not fork them.

**One core-graph change is required and is called out explicitly (§2, §8).** The locked decision is
that a `claim_url` click **is itself the activation signal, decoupled from reply state** - the click is
the sole conversion metric and a terminal stop. But the frozen Phase 0 graph routes *every* edge into
`physician_activated` through `interested`, and the only edge into `interested` is from `replied`. Reply
classification (`replied → interested`) is a Phase 2 LLM node and is **out of scope here**, so without a
change the poller can never find an `interested` lead and the sole conversion metric is unreachable
end-to-end in Phase 1. Phase 1 therefore adds **two deterministic edges into `interested`** -
`email_sent → interested` and `awaiting_reply → interested` - to `certuma_core.status.ALLOWED_TRANSITIONS`
(the only Phase 0 logic file Phase 1 edits, a 2-line additive change; the DB status CHECK already lists
`interested`, so **no migration touches the status enum**). The poller then promotes a clicked lead in a
single unit of work: `email_sent|awaiting_reply → interested → physician_activated`, **both** steps as
`actor='poller'` (∈ `ACTIVATION_ONLY_ACTORS`). This is the deterministic, non-LLM "claim-click ⇒ interested"
promotion the Phase 2 reply path would otherwise own; it does not pre-empt or duplicate reply
classification (a real reply still drives `awaiting_reply → replied` independently).

Locked decisions in force: email-first; autonomy **Assisted** at launch (approve every send);
reply-confidence floor 0.8; high-value = `activation_priority='high' AND practice_group_size>=3`;
enrichment send filter **VALID-ONLY** (park risky/catch_all/unknown); SLA-expiry = HOLD + escalate
(never auto-send); sender identity = a real accountable employee on the cold domain; conversion =
`claim_url` click → `physician_activated` (sole metric, terminal); platform read/webhook endpoints
NOT built → poll `claim_url`; model tiering Haiku 4.5 (reply class.) / Sonnet 4.6 (volume drafting) /
Opus 4.8 (template authoring + high-value/objection). **No production sends in Phase 1**; local
**Mailpit** (SMTP `:11025`, UI `:18025`) is the dev provider until the cold domain exists.

---

## 0. Phase 1 goal & definition of done

**Goal:** a sales lead approves **1 campaign + 1 template**, and Certuma sends a **real (Mailpit)**
email, **every send human-approved (Assisted)**, with delivery/bounce events ingested and
`claim_url` polling wired to drive `physician_activated`.

**Definition of done (exit bar checklist):**

- [ ] **DoD-1 - Enrichment produces a sendable lead.** A scored lead in `not_contacted` runs the
  enrich→verify waterfall and, on a VALID email, transitions `enriching → sendable` via
  `ledger_writer.transition(actor='enricher', reason_code='valid_email_found')` **and stamps
  `lead.next_action_at = now()`** (so the Sender's `next_action_at <= now()` claim query can see it - see
  §3); a `contact` row exists with `email_status='valid'`, `verifier`, `verified_at`. No-valid →
  `enriching → needs_review`, `reason_code='no_valid_email'`, with the parked `contact` row still written.
- [ ] **DoD-2 - A campaign is activated and a template is approved.** Operator flips
  `campaign.is_active=true` and approves exactly one `template` row (`is_approved=true`,
  `approved_by=<app_user>`), with an `audit_log(entity='template')` row. The COPYWRITER refuses to
  draft from any unapproved template.
- [ ] **DoD-3 - A compliant draft passes the linter.** COPYWRITER (Sonnet 4.6) emits strict JSON
  `{subject, body, plaintext, variant_id, merge_token_audit}`; the deterministic
  `certuma_core.linter.lint()` passes (unsubscribe token present, postal address present, no banned
  claim, `claim_url` byte-equals `lead.claim_url`, no hallucinated fact, all required merge tokens).
- [ ] **DoD-4 - Every send is enqueued for human approval (Assisted).** On `sendable`, an
  `Approval(state='pending')` row is created; the lead does **not** transition; `lead.version` is
  unchanged (enqueue is a no-op against the ledger).
- [ ] **DoD-5 - Human approval actually sends.** `POST /approvals/{id}/decision {decision:'approved'}`
  re-runs `gate.evaluate`, then the SENDER calls
  `ledger_writer.transition(actor='sender', new_status='email_sent', idempotency={...Message...})`
  (Message row inserted **before** the ESP call), `MailpitProvider.send(...)` delivers, and
  `esp_message_id`/`sent_at` are back-filled. The email is visible in Mailpit (`GET :18025/api/v1/messages`)
  with `List-Unsubscribe` + `List-Unsubscribe-Post` headers, the postal address in the body, the exact
  `claim_url`, and `Reply-To: reply+<reply_token>@<cold_domain>` (the random, unguessable
  `thread.reply_token`, **not** the raw integer `thread_id` - see §5b-F).
- [ ] **DoD-6 - Double-send is structurally impossible, and re-approve is safe.** A second approve/send on
  the same `(npi,campaign,cadence_step)` outbound raises `IntegrityError` on the `uq_msg_idem_outbound`
  partial unique index inside `transition()`; the caller **rolls back the (now poisoned) session first**,
  re-reads the already-sent state, marks the approval `approved` idempotently, and does **not** call
  `provider.send` again. Mailpit shows exactly one message. Approving the *same* approval twice returns the
  same terminal result without a second send and without a `PendingRollbackError` (see §5b-G, §7).
- [ ] **DoD-7 - The full Gate works without breaking the Phase 0 contract.** `gate.evaluate` returns
  `HOLD can_spam_incomplete` for a campaign whose approved template lacks unsubscribe/postal or whose
  `Settings.postal_address`/sender identity is empty (a *transient config* state, re-queued when fixed -
  not a permanent suppression-class BLOCK); `HOLD quiet_hours` / `warmup_cap_exceeded` /
  `circuit_breaker_complaint` / `circuit_breaker_bounce` for those conditions; and the existing Phase 0
  decisions (`BLOCK suppression`, `HOLD kill_switch`, `HOLD campaign_paused`) are unchanged. The Gate
  performs **no writes** (the breaker *pause* is owned by the ingest-side breaker-trip action, not by
  `evaluate` - see §5a, §6). New kwargs default to `None` so `/gate/preview` behaves identically.
- [ ] **DoD-8 - Delivery, bounce, and opt-out events are ingested.** A provider/synthetic `delivered` event
  drives `email_sent → awaiting_reply`; a hard `bounced` event writes a `Suppression(reason='hard_bounce')`,
  sets the re-enrich flag, and stops the lead (`→ exhausted`); an `opt_out`/`unsubscribe_click` event
  deterministically writes a `Suppression(reason='opt_out')` and stops the lead (`→ do_not_contact`,
  **never LLM-gated**); a subsequent `gate.evaluate` for that npi/email returns `BLOCK suppression`. All
  event ingestion is deduped on `uq_event_dedup`.
- [ ] **DoD-9 - Activation polling is wired and reachable end-to-end.** `claim_poller.run_once` selects due
  leads with a `claim_url`, calls `publish.claim_status.poll_claim_urls(items, fetch=<injected>)`; on a
  click status it writes `Event(event_type='activated')` with a deterministic dedup key and promotes the
  lead in one unit of work `email_sent|awaiting_reply → interested → physician_activated`, **both** steps
  as `actor='poller'` (this is reachable from a real Phase-1 send, *not* by seeding `interested` directly -
  the `email_sent → interested` / `awaiting_reply → interested` edges are added to `ALLOWED_TRANSITIONS`,
  see §2/§8). A re-poll of an already-activated lead is a no-op (dedup-key collision short-circuits before
  any `transition`). With `default_fetch` (`ClaimStatusUnavailable`) the poller stamps `last_polled_at`,
  emits `poll_run{result=source_unavailable}`, and does nothing else.
- [ ] **DoD-10 - Observability & smallest E2E.** The smallest Assisted slice (1 active campaign + 1
  approved template + 1 valid contact + 1 `sendable` lead) runs end-to-end against Mailpit with all
  `METRICS` counters asserted - including the **full activation slice** (send → delivered →
  `awaiting_reply` → poll-click → `physician_activated`) reachable from a real Phase-1 send; a
  `/enrichment/funnel` read endpoint returns the funnel histogram; all new tests are **skip-without-DB**
  friendly (mirroring `tests/db/test_ledger_writer.py`).

**Non-goals (explicitly deferred):** cadence/follow-up touches beyond first-touch (Phase 3); reply
classification/drafting Claude nodes (Haiku/Sonnet reply path, Phase 2); the cold sending domain + ESP
account + named employee (stakeholder infra); Supervised/Autonomous auto-send; the platform read/webhook
endpoints (interim poll only). Production sends are out of scope - Mailpit only.

---

## 1. New components & repo layout

New modules sit beside the Phase 0 packages and import them; they never duplicate Phase 0 logic. The
**single** EmailProvider adapter (used for both sending and event parsing) is the seam that lets Mailpit
be the dev provider and a cold-tolerant ESP slot in later - mirroring the injected-creds + injectable-opener
pattern of `certuma/publish/client.py` and `certuma/publish/claim_status.py`.

```
certuma_core/
  status.py            EXTEND  the ONLY Phase 0 logic file Phase 1 edits: add two edges to
                            ALLOWED_TRANSITIONS - 'email_sent'→{...,'interested'} and
                            'awaiting_reply'→{...,'interested'} - so the poller's claim-click promotion
                            is reachable (§2). Strictly additive (no edge removed); all Phase 0 status
                            tests stay green. No other symbol changes.
  linter.py            NEW  pure deterministic copy linter: lint(rendered, *, seed, allowlist_literals,
                            required_tokens, postal_address, claim_url, unsubscribe_url) -> LintResult.
                            BANNED_CLAIMS frozenset + fact-allowlist hallucination check. No I/O/LLM/DB.
  copy_schema.py       NEW  pure value-objects: RenderedCopy, SeedFacts, MERGE_TOKEN_SPEC, the
                            Copywriter JSON schema dict + Pydantic mirror, variant_id derivation.

certuma/
  config.py            EXTEND  add cold-ESP + provider siblings to Settings.from_env (firewalled).
  gate.py              EXTEND  add CAN-SPAM BLOCK + quiet-hours/warmup/breaker HOLDs; signature
                            grows two keyword-only params with defaults (when=None, mailbox=None).
  compliance.py        NEW  pure: assert_can_spam_complete(rendered, headers, settings)->Optional[reason];
                            quiet_hours_blocked(practice_state, when)->bool with STATE_TZ map.
  breakers.py          NEW  rolling complaint/bounce window + trip/clear (hysteresis) -> reason|None.
  sender.py            NEW  deterministic SENDER (no LLM): claim lead, gate, render, transition with
                            idempotency BEFORE send, EmailProvider.send, back-fill esp_message_id.

  email/               NEW package - the ONE provider adapter (send + parse_events).
    __init__.py             EmailProvider Protocol + get_provider(settings) factory (dev vs ESP).
    provider.py             EmailProvider ABC: send(EmailMessage)->SendResult(esp_message_id);
                            parse_events(raw, *, signature)->list[NormalizedEvent]; verify_signature().
    message.py              EmailMessage dataclass + build_outbound(): From(name+title), Reply-To
                            reply+<reply_token>@domain (random per-thread token, NOT the integer
                            thread_id), RFC 8058 List-Unsubscribe[-Post], postal footer.
    mailpit.py              MailpitProvider: smtplib.SMTP 127.0.0.1:11025 (no auth, injectable
                            transport); parse_events reads Mailpit REST :18025 -> synth 'delivered'.
    esp.py                  ColdEspProvider stub (injected esp_api_key, injectable opener, raises
                            NotConfigured); the provider->canonical event_type mapping table.
    ingest.py               normalizer: ingest_events(session, events, *, provider) -> Event dedup +
                            correlate by esp_message_id + flip delivery flags + drive transition +
                            route hard-bounce/complaint to suppression(hard_bounce/complaint) +
                            route opt_out/unsubscribe_click to suppression(opt_out)+do_not_contact
                            (deterministic, never LLM-gated). Inbound reply -> Message correlated by
                            reply_token, carrying esp_message_id (so uq_msg_inbound_esp dedups replays).
    dedup.py                event_dedup_key(provider, provider_event_id);
                            activation_dedup_key(npi, campaign)='activate:{npi}:{campaign}'.
    suppress.py             suppress_address(session, npi, email, reason): idempotent Suppression +
                            re-enrich flag (hard_bounce only) + METRICS. reason in {hard_bounce,
                            complaint, opt_out}; opt_out is the one-click-unsubscribe / opt-out path.
    breaker_window.py       read-only rolling counters over Event rows for the Gate breaker check.

  enrich/              NEW package (mirrors certuma/publish/ layout).
    __init__.py
    adapter.py              EmailEnricher Protocol/ABC: find_emails(npi, fields)->list[EmailCandidate];
                            verify(email)->Verdict. HealthcareSource/B2BSource/PatternGuesser composite.
                            Injected creds + injectable opener; default raises ProviderUnavailable.
    fixtures.py             FakeEnricher/FixtureEnricher: deterministic in-memory per-NPI scripts;
                            also the Mailpit-local "safe valid address" source for E2E.
    verdict.py              Verdict/EmailCandidate dataclasses + classify(): raw sub-codes -> the 5
                            contact.email_status values; role-address demotion; catch_all rule.
    budget.py               Budget: per-priority call allowances + daily cap; allocate() yields work
                            in PRIORITY_RANK order, short-circuits at the cap. Injectable like ScoringConfig.
    cache.py                per-NPI "pay once" verdict cache: DB-backed (contact.verified_at freshness),
                            optional Redis read-through.
    loop.py                 orchestrator: select eligible -> claim(->enriching) -> waterfall+verify
                            with cache -> UPSERT contact -> transition ->sendable|needs_review.

  copywriter/          NEW package (the first Claude node).
    __init__.py
    provider.py             CopyProvider Protocol + AnthropicCopyProvider (injected anthropic.Anthropic
                            client, model-tier policy) + StubCopyProvider (deterministic, no network).
    node.py                 draft_copy(session, *, lead, prospect, score, campaign, claim_url, provider)
                            -> DraftResult. Loads approved template, builds SeedFacts + deterministic
                            fills, picks tier, calls provider, runs certuma_core.linter, 1 retry-with-feedback.
    render.py               deterministic token renderer; injects claim_url/unsubscribe_url/postal_address
                            (NEVER emitted by the model).

  templates/           NEW package (template governance).
    approval.py             list_templates / clone_template / approve_template(session, id, app_user)
                            (is_approved=true + approved_by + AuditLog) / preview_template (render+lint).

  orchestrator/        NEW package (the Assisted loop glue).
    loop.py                 process_lead(session, lead_id): durable per-lead step dispatched by status.
    policy.py               classify_action(proposed_action, value_tier, model_confidence, autonomy_level)
                            -> AUTO_EXECUTE | ENQUEUE_APPROVAL | ESCALATE. Pure.
    scheduler.py            build_eligible_queue(session, now) (reuses certuma_core.queue.rank_queue) +
                            claim_due(session, limit) via SELECT ... FOR UPDATE SKIP LOCKED.
    approvals.py            enqueue_approval(...) + execute_approved_send(session, approval) +
                            sla_sweep(session, now) (HOLD+escalate).
    runner.py               process-level worker: claim_due loop -> process_lead -> commit/rollback per lead.

  poller/              NEW package (activation).
    claim_poller.py         select_due_leads + run_once(session, *, fetch, now): poll, on click write
                            Event('activated') + transition interested->physician_activated as 'poller'.
    reenrich.py             hard-bounce re-enrich flagging the enrich loop reads.

  api/
    app.py               EXTEND  rewire /approvals/{id}/decision to actually send; add GET /approvals/{id}.
    templates.py         NEW  router: GET/POST /templates, POST /templates/{id}/approve|preview.
    enrichment.py        NEW  router: GET /enrichment/funnel (read-only histogram).
    webhooks.py          NEW  router: POST /webhooks/email/{provider} (verify+enqueue), POST /poll/run.

  prompts/
    copywriter_system.txt NEW  frozen system prompt (banned-claims policy, private-draft framing,
                            facts-only-from-seed rule, JSON-only contract) - frozen for prompt-cache.

  db/alembic/versions/
    0003_phase1.py       NEW  additive migration (see §8).
```

**Adapter seam (load-bearing).** `EmailProvider` is one Protocol with `send` + `parse_events` +
`verify_signature`. `MailpitProvider` is the dev impl; `ColdEspProvider` is the deferred impl behind the
**same** Protocol. `get_provider(settings)` selects on `settings.email_provider` (`'mailpit'` | `'<esp>'`).
A `test_adapter_interface_parity` test guards that both satisfy the Protocol so the cold-ESP swap is a
config change, not a code change.

---

## 2. The email send lifecycle, end to end

States are the real `certuma_core.status.STATES`; every status move below is a real
`certuma.ledger_writer.transition` call (the only writer). All edges cited are legal in
`ALLOWED_TRANSITIONS` **after** the additive two-edge change above (`email_sent → interested`,
`awaiting_reply → interested`); every other edge is already legal in the frozen Phase 0 graph
(verified - status.py L63-86). The two new edges are the *only* core-graph change Phase 1 makes.

```
not_contacted ──enricher──> enriching ──enricher──> sendable ──sender──> email_sent
   (claim)        (valid email)           (approved)    (List-Unsub +     ──ingest(delivered)──>
                                                         postal + claim)   awaiting_reply
                       └─enricher─> needs_review (no valid email)
email_sent ──ingest(hard bounce/complaint)──> exhausted   (+ Suppression, re-enrich flag)
email_sent | awaiting_reply ──ingest(opt_out/unsub click)──> do_not_contact  (+ Suppression(opt_out))

ACTIVATION (claim-click, the sole conversion metric, decoupled from reply state):
email_sent | awaiting_reply ──poller(NEW edge)──> interested ──poller──> physician_activated (TERMINAL)
                                                   (both steps actor='poller', one unit of work)

REPLY (independent, Phase 2 owns replied→interested classification):
awaiting_reply ──ingest(reply)──> replied   [Phase-1 ledger edge; classification deferred to Phase 2]
```

Step-by-step, naming the real symbols:

1. **Select & claim.** `scheduler.build_eligible_queue(session, now)` selects leads where
   `activation_status IN ('not_contacted','queued_today')` for an `is_active AND NOT is_paused` campaign,
   LEFT JOINs the latest `workflow_score`, builds `certuma_core.queue.QueueItem`s and calls
   `certuma_core.queue.rank_queue` (sort `(PRIORITY_RANK, -score, name, npi)`; drops
   `QUEUE_EXCLUDED_STATES` + `'low'`). `runner.claim_due` claims a batch via
   `SELECT ... FOR UPDATE SKIP LOCKED`.

2. **Enrich (→ enriching → sendable | needs_review).** `enrich/loop.py` claims the lead with
   `transition(lead_id, 'enriching', actor='enricher', reason_code='enrichment_started',
   expected_version=lead.version)`, runs the waterfall+verify (§3), UPSERTs `contact`, then either
   `transition(..., 'sendable', reason_code='valid_email_found')` or
   `transition(..., 'needs_review', reason_code='no_valid_email')`. (§3.)

3. **Publish private draft + capture claim_url.** For a `sendable` lead without `claim_url`, the publish
   step calls `certuma.publish.build_payload` + `publish(payload, base_url=..., token=..., opener=...)`,
   then `certuma.publish.claim_urls_by_npi(result)` and writes `lead.claim_url`. (Until the publish
   endpoint exists this is fixture-driven; `claim_url` is required before drafting - missing → `needs_review`.)

4. **Draft (COPYWRITER, the first Claude node).** `copywriter/node.draft_copy(...)` loads the latest
   `is_approved` `template` for the campaign (refuse if none), builds `SeedFacts`, derives the three
   compliance tokens deterministically, calls the provider (Sonnet 4.6 volume / Opus 4.8 high-value),
   parses strict JSON, runs `certuma_core.linter.lint()`. Pass → handoff; persistent reject →
   `needs_review` (no send). (§4.)

5. **Policy + enqueue (Assisted).** `orchestrator/policy.classify_action(...)` returns
   `ENQUEUE_APPROVAL` for every send under `autonomy_level='assisted'`. `gate.evaluate` is run; on
   `ALLOW`, `approvals.enqueue_approval` inserts `Approval(state='pending', proposed_subject/proposed_body,
   value_tier, model_confidence, gate_reason_code, sla_expires_at)`. The lead **stays** `sendable`
   (no transition; `lead.version` unchanged). (§7.)

6. **Human approves → SEND.** `POST /approvals/{id}/decision {approved}` →
   `orchestrator/approvals.execute_approved_send`: re-run `gate.evaluate` (catches a kill-switch flipped
   while queued); ensure `Thread` (abort if `is_locked`); `sender.py` builds the `EmailMessage`
   (`message.build_outbound`) and calls
   `ledger_writer.transition(lead.id, 'email_sent', actor='sender', reason_code='first_touch_sent',
   expected_version=lead.version, idempotency={'lead_id','thread_id','npi','campaign','cadence_step',
   'direction':'outbound','subject','body_rendered','variant_id'})`. The **idempotency Message is inserted
   and flushed inside transition() BEFORE the ESP call** (ledger_writer L79-83) - a duplicate
   `(npi,campaign,cadence_step)` outbound raises `IntegrityError` on `uq_msg_idem_outbound` ⇒ double-send
   impossible. Then `provider.send(msg)`; on success set `Message.esp_message_id` + `sent_at=func.now()`
   and `session.commit()`. On `ProviderError`, roll back the whole unit (Message + transition reverse,
   both uncommitted) and re-queue. (§5.)

7. **Delivered → awaiting_reply.** The EmailProvider event path (`email/ingest.ingest_events`) writes a
   deduped `Event(event_type='delivered')`, sets `message.delivered=true`, and
   `transition(lead.id, 'awaiting_reply', actor='monitor', reason_code='delivered',
   expected_version=lead.version)`. (§6.)

8. **Bounce/complaint.** Hard bounce → `suppress_address(npi, email, reason='hard_bounce')` (instant,
   non-gated) + `lead.needs_reenrich=true` + `transition(..., 'exhausted', actor='monitor')`. Complaint →
   `suppress_address(..., reason='complaint')` + feed `breaker_window`. (§6.)

9. **Activation poll → physician_activated.** `poller/claim_poller.run_once(session, fetch=<injected>,
   now=...)` selects non-terminal leads with `claim_url` and `last_polled_at` older than the interval,
   calls `certuma.publish.claim_status.poll_claim_urls(items, fetch=...)`. On a click status it writes
   `Event(event_type='activated', dedup_key=activation_dedup_key(npi, campaign))` in a savepoint; if the
   key already exists → no-op (re-poll cannot re-fire). Otherwise it promotes the lead in **one unit of
   work**: if the lead is in `email_sent` or `awaiting_reply`, `transition(..., 'interested',
   actor='poller', reason_code='claim_clicked', expected_version=lead.version)` (the NEW deterministic
   edge); then `transition(..., 'physician_activated', actor='poller', reason_code='claim_clicked',
   expected_version=<bumped version>)` and stamps `activation_detected_at`. A lead already in `interested`
   (e.g. a future Phase-2 reply path put it there) skips straight to the second step. `last_polled_at` is
   stamped on **every** attempt (incl. `ClaimStatusUnavailable`). `actor='poller'` is one of
   `ACTIVATION_ONLY_ACTORS` (status.py L61); any other actor raises `IllegalActor` (ledger_writer L75-77)
   on **both** steps - note the actor guard fires only on the `physician_activated` write, so the
   `→interested` write is intentionally restricted to the poller by convention + the Monitor never
   targeting `interested` (the only other `→interested` writer would be the Phase 2 classifier). (§6.)

The **message idempotency key** = the partial unique index `uq_msg_idem_outbound ON message
(npi,campaign,cadence_step) WHERE direction='outbound'` (0001 schema L210-213). The
**`claim_status.poll_claim_urls`** seam (publish/claim_status.py) is the activation source until the
platform webhook is built.

---

## 3. Enrichment

Turns a scored lead into **at most one** send-eligible email or marks it unfindable. Deliverability-focused
(the opposite of the deliverability-blind `certuma_core.scoring.py`). Fully deterministic - **no LLM**.

**Waterfall (verify-first).** For a lead in `enriching`:
1. **healthcare-specialized source** (NPI/practice-aware) → candidate set; else
2. **general B2B source** (name + practice-domain); else
3. **pattern-guess last resort** (`first.last@domain`, `flast@domain`) **only if a practice domain is
   known**. Each tier short-circuits on the first plausible candidate set. `METRICS.incr('enrich_source_hit',
   source='healthcare|b2b|pattern')`.

**Suppression pre-check before spending a verify credit.** For each candidate, call the **narrow
suppression predicate** `gate._is_suppressed(session, npi, candidate)` (read-only) - **not** the full
`gate.evaluate`. The enricher wants *only* the suppression floor here; the full extended Gate (§5a) also
runs CAN-SPAM/kill-switch/breaker checks, so calling it during enrichment would (a) drop or distort every
candidate when an unrelated kill-switch is ON (a `HOLD` is not a `BLOCK`, so reasoning on `decision.allowed`
would wrongly drop them, and reasoning on `BLOCK`-only would wastefully keep them) and (b) couple enrichment
to send-time policy it has no business consulting. A suppressed candidate is dropped without a verify call.
Exposing `_is_suppressed` as a thin public `gate.is_suppressed(...)` wrapper keeps the Gate's internals
encapsulated. The Gate is **not** modified-for-writes by this component (read-only).

**Mandatory verification.** Every surviving candidate goes through `adapter.verify()` →
`verdict.classify()` → exactly one of `{valid, risky, catch_all, unknown, invalid}` (the
`contact.email_status` CHECK values, models.py L111-113). Rules in `classify()`:
- **role-address demotion**: `info@/office@/admin@/contact@…` are capped at `'risky'` (**never `valid`**)
  even when the provider says valid.
- **catch_all** domains → `'catch_all'`.
- raw provider sub-codes → the 5 statuses via a per-vendor map (pinned-literal table tests).

**Write target.** UPSERT the result on the `(npi,email)` unique index `npi_email` (models.py L114) using
`pg_insert(Contact).on_conflict_do_update(...)` - the exact pattern of
`seed_importer._upsert_prospect_seed` (L141-143) - so a re-run re-verifies in place with no dup rows.
Write `email_status` verbatim, `verifier` = provenance string (e.g. `'zerobounce'`, `'clearout'`,
`'heuristic_pattern'`; or `'zerobounce/role'`, `'heuristic_pattern'` to encode role/source if migration
0003 is deferred), `verified_at=func.now()`. CITEXT makes dedup/lookup case-insensitive for free.

**VALID-ONLY send filter (the launch gate).** Pick the best address per NPI
(`valid > risky > catch_all > unknown`; `invalid` never selected). If best `== 'valid'` →
`transition(lead_id, 'sendable', actor='enricher', reason_code='valid_email_found',
expected_version=lead.version)` **and, in the same txn, set `lead.next_action_at = func.now()`**. This is
load-bearing: `lead.next_action_at` is nullable with no default (models.py L159), and the Sender claims on
`next_action_at <= now()` (§5b-A) - in SQL `NULL <= now()` is NULL/false, so a freshly-enriched `sendable`
lead with a NULL `next_action_at` would be **silently invisible to the Sender forever**. The enricher must
stamp it (the orchestrator may equivalently stamp it on enqueue, but the enricher is the single owner of
the `→sendable` transition, so it stamps here). The Sender's claim query also defensively uses
`(next_action_at IS NULL OR next_action_at <= now())` as belt-and-suspenders. Otherwise →
`transition(lead_id, 'needs_review', actor='enricher', reason_code='no_valid_email', ...)`. Parked
`contact` rows persist so a future relaxed filter or human review can re-promote (note `needs_review` is
**non-terminal** in the graph: `needs_review → {sendable, queued_today, enriching, do_not_contact,
exhausted}`, status.py L78). On `ConcurrencyConflict`, skip (another worker holds it).

**Budget (`enrich/budget.py`).** Per-priority provider-call allowances (high/medium/low) + a daily spend
cap. `allocate(leads_with_priority)` yields work in `PRIORITY_RANK` order (high=0,medium=1,low=2 - the
same `certuma_core.queue.PRIORITY_RANK`) and short-circuits when the cap or a per-priority allowance is
hit. **high-value** (`activation_priority='high' AND practice_group_size>=3`, both on `workflow_score`)
gets the largest per-NPI allowance. Un-enriched leads simply stay `not_contacted` for the next run (no
state change, no penalty). Injectable like `ScoringConfig`.

**Cache (`enrich/cache.py`) - pay once per NPI.** Before any provider call, check `contact` for a
non-stale verdict for the NPI; if present, reuse it (`METRICS.incr('enrich_cache_hit')`), no spend.
Staleness is a query predicate on `verified_at` (`verified_at < now() - interval`); no schema change. The
TTL differs by verdict (a `valid` decays slower than `catch_all`/`unknown`) - TTL number is an open
decision (§12). Optional Redis read-through (the stack has Redis), DB remains source of truth.

**Funnel math (`GET /enrichment/funnel`).** Surfaces coverage explicitly so low coverage reads as a
measured funnel, not a broken pipeline: `enrich_attempt` (by priority) → `enrich_source_hit`
(by source) → `enrich_verify` (verdict histogram) → `enrich_result{outcome=sendable|needs_review}` →
`valid_rate`, `needs_review_count`, `enrich_provider_call` (spend). The endpoint reconciles to the
`METRICS` counters (a `funnel-math` test drives 100 fixture NPIs with realistic low coverage, e.g. 35
found / 18 valid, and asserts `valid_rate`/`needs_review` match the counters).

**Adapter seam.** `EmailEnricher` mirrors `publish/claim_status`: injected creds (read in
`Settings.from_env` as `CERTUMA_ENRICH_API_KEY` / `CERTUMA_VERIFY_API_KEY`, firewalled cold store),
injectable HTTP opener, default raises a typed `ProviderUnavailable('no enrichment provider wired')` so
the not-yet-purchased provider is explicit. `FixtureEnricher` drives every test with zero network/spend
and emits a known-`valid` local address (a Mailpit-captured inbox) so the SENDER can be exercised E2E
**before** the cold domain exists.

---

## 4. COPYWRITER + linter + template approval

The copy-generation + copy-governance layer between an approved template and the deterministic SENDER.
Two **hard deterministic gates** around the one non-deterministic step.

### 4a. Template approval (one-time per campaign, before any send)

The seeded `template` from migration 0002 is `campaign=NULL, version=1, is_approved=false` carrying
`{last_name, pitch_angle, city, claim_url, unsubscribe_url, postal_address}` - it is the compliant-but-
unapproved starting asset and the linter's reference token contract (`unsubscribe_url` + `postal_address`
are the CAN-SPAM-critical pair the old Rox copy lacked).

`certuma/api/templates.py` router + `certuma/templates/approval.py`:
- `GET /templates` - list (shows the seeded NULL/v1 draft).
- `POST /templates` - `clone_template`: clone the seed → a real campaign draft (`campaign='dermatology'`,
  new row, `is_approved=false`); new versions are **inserted, never updated in place**, preserving
  `UniqueConstraint('campaign','version')` (models.py L240) so an approved v1 stays immutable while v2 is
  authored.
- `POST /templates/{id}/preview` - render against a sample physician + run `certuma_core.linter`; returns
  `LintResult` + a live preview, no send.
- `POST /templates/{id}/approve` - `approve_template(session, id, app_user)`: set `is_approved=true`,
  `approved_by=<app_user>`, write `AuditLog(entity='template', action='approve', actor=app_user)` mirroring
  the ledger's audit pattern. **Without an approved template the COPYWRITER refuses to draft.**

### 4b. COPYWRITER (the first Claude node)

`copywriter/node.draft_copy(session, *, lead, prospect, score, campaign, claim_url, provider)`:
- Loads the latest `is_approved` template `WHERE campaign=<name> ORDER BY version DESC LIMIT 1` (refuse if
  none).
- Builds `SeedFacts` = the **allow-listed** fact set only: `{last_name, city, primary_specialty,
  display_name}` from `Prospect` + `WorkflowScore`. `specialty_terms` are **not** exposed to the model.
- Assembles the **full hallucination allow-list corpus** that is handed to the linter (§4c) - this is wider
  than `SeedFacts` and **must** include everything that may legitimately appear in a render: (i) the
  `SeedFacts` literals, (ii) `campaign.pitch_angle` (operator-authored, approved free text fed verbatim as
  `{pitch_angle}`), (iii) the approved-template literal tokens (the static prose the operator approved),
  and (iv) the sender-identity literals (the accountable employee's name/title, an approved literal - §12).
  Exempting `pitch_angle` from the hallucination check *only* works if its tokens are in this corpus;
  otherwise a perfectly legitimate render containing the pitch angle would false-REJECT (R3). The corpus is
  the single source of "allowed proper nouns," computed deterministically and passed to the linter as
  `allowlist_literals`.
- Derives the **three compliance-critical tokens deterministically and does NOT send them to the model
  and does NOT request them in output**: `claim_url = lead.claim_url`, `unsubscribe_url`, `postal_address`
  (from Settings). These are injected by `render.py`.

**Model tiering** (`copywriter/provider.AnthropicCopyProvider`): `claude-sonnet-4-6` for volume first-touch
(value_tier `medium`/`low`, the common path); `claude-opus-4-8` for high-value (`activation_priority='high'
AND practice_group_size>=3`) + objection/high-value replies + template authoring. (`claude-haiku-4-5` is
the separate reply-classification node, out of scope here.)

**Structured output** (enforced, strict): `{subject:str, body:str, plaintext:str, variant_id:str,
merge_token_audit:{token->source}}` via `output_config.format` json_schema (`additionalProperties:false`)
or `client.messages.parse()` against the Pydantic mirror in `copy_schema.py`. `merge_token_audit` forces
the model to self-attribute every token it filled to a seed field; the linter then verifies that against
`SeedFacts`. `thinking` adaptive (effort low for Sonnet volume, high for Opus high-value);
`max_tokens ~2000` (non-streaming, under the timeout guard).

**Prompt caching.** The frozen system prompt (`prompts/copywriter_system.txt`: banned-claims policy +
private-draft framing + JSON contract) + the approved template body are placed first with
`cache_control:{type:'ephemeral'}`; only per-physician `SeedFacts` vary, so the stable prefix caches across
the batch (verify via `usage.cache_read_input_tokens`). **Assumptions to verify against the live Anthropic
API at P1.8 implementation time (do not treat as fact):** the model-id strings (`claude-sonnet-4-6`,
`claude-opus-4-8`, `claude-haiku-4-5`), the minimum cacheable-prefix token counts (the prefix must be padded
past whatever the current minimum is - historically on the order of a few thousand tokens, larger for the
bigger model), the Batches discount (~50%), and whether structured output is done via
`output_config.format` json_schema or `client.messages.parse()`. Pin exactly one structured-output
mechanism and the verified numbers before building; the cost-control DoD depends on these. (See the
`claude-api` reference; verify at implementation time.) The Batches API can be used for non-interactive
first-touch runs.

`variant_id` is deterministic: `campaign:version:variant_label` (e.g. `dermatology:1:a`) so
`Message.variant_id` idempotency + per-variant activation analytics hold.

### 4c. The deterministic LINTER (`certuma_core/linter.py`)

Pure, no I/O / no LLM / no DB - testable with plain dicts like the rest of `certuma_core`.
`lint(rendered, *, seed, allowlist_literals, required_tokens, postal_address, claim_url, unsubscribe_url)
-> LintResult(ok, reason_codes[], details)`. `allowlist_literals` is the **full hallucination allow-list
corpus** assembled by the COPYWRITER (§4b): `SeedFacts` ∪ `campaign.pitch_angle` tokens ∪ approved-template
literal tokens ∪ sender-identity literals. Without it the linter cannot know the pitch-angle / template
prose is allowed and would either over-reject legitimate copy (pitch angle absent from `seed`) or the §4b
exemption would be unenforceable. Checks in order; **REJECT** on any:
1. all required merge tokens present (`reason_code='missing_token'`);
2. `unsubscribe_url` token present (`missing_unsubscribe`);
3. `postal_address` present (`missing_address`);
4. **no banned claim** - `BANNED_CLAIMS` frozenset `{verified, board-certified, credentialed, endorsed,
   licensed, approved}` with **word-boundary** regex (must-pass near-misses like "approve your draft"
   must NOT trip "approved") (`banned_claim`);
5. rendered `claim_url` **byte-equals** `lead.claim_url` - any model-altered URL is a REJECT
   (`altered_claim_url`);
6. **hallucination guard** - every proper-noun/identifier in subject + body + plaintext must trace to a
   literal in `allowlist_literals` (the exact strings - `SeedFacts`, pitch angle, approved-template prose,
   or sender identity); else REJECT (`hallucination`). Allow-list (trace-to-source), not deny-list. The
   proper-noun extractor whitelists multi-word allow-list entries (e.g. a two-word city, a pitch angle
   phrase, the sender's full name) so they don't trip token-by-token.

The linter lints **subject + body + plaintext uniformly** (a banned claim or fabricated fact must not hide
in the `text/plain` alternative or the subject line). It exposes the structured `LintResult` so the Gate's
CAN-SPAM-completeness check is satisfied by "this draft passed the linter" rather than re-parsing the body.
The linter runs **before** the Gate (no point gating un-renderable copy).

### 4d. Recover or fail

On a recoverable reject (banned claim / light hallucination), `node.draft_copy` retries **once** with the
lint reason injected as feedback; on a second reject or any missing-required-token, **hard-fail** - emit
metric, write the draft to the Approval queue as needs-review (do **not** send), let a human fix/escalate.
**Never silently send linter-failed copy.** The COPYWRITER never imports `ledger_writer`; it cannot change
status (exactly like `seed_importer`).

**Observability:** `copywriter_call{model,variant_id,outcome}`, token/cost counters,
`copywriter_confidence`; `linter_result{outcome}` + `linter_reject{reason=banned_claim|missing_token|
missing_address|hallucination|missing_unsubscribe|altered_claim_url}`.

---

## 5. The full Gate + the SENDER

### 5a. Gate extension (`certuma/gate.py`, EXTEND in place)

Keep the exact signature plus **two new keyword-only params with defaults** so existing callers
(`api/app.py /gate/preview`, the Sender) keep working:

```python
def evaluate(session, *, npi, email, campaign, when=None, mailbox=None) -> GateDecision:
```

Reuse `GateDecision` (frozen) + `ALLOW/HOLD/BLOCK` + the `_decided()` helper verbatim - every new branch
returns through `_decided()` so `METRICS.incr('gate_decision', ...)` + the structured `emit` fire
unchanged, and `api/app.py` / `approval.gate_reason_code` render the new reason codes with **zero** struct
change. The Gate stays **strictly read-only** - the Phase 0 docstring invariant ("the Gate NEVER
transitions a lead... read-only") is preserved; no new branch writes to any table (critically, the breaker
*pause action* is NOT done here - see step 4 below and §6). This keeps `/gate/preview` (a GET) free of
side-effects. New check order (preserving the Phase 0 floor & precedence):

1. **suppression → BLOCK `'suppression'`** [unchanged Phase 0 floor, first - permanent/suppression-class].
2. **kill_switch → HOLD `'kill_switch'`** [unchanged].
3. **campaign_paused → HOLD `'campaign_paused'`** [unchanged].
4. **circuit breakers → HOLD** (`breakers.check`, **read-only**): read the persisted/computed rolling
   window - complaint rate > 0.1% → `'circuit_breaker_complaint'`; bounce rate > 2% →
   `'circuit_breaker_bounce'`. The Gate **only reads** the breaker state; it does **not** set
   `campaign.is_paused`. The actual pause write is owned by the ingest-side **breaker-trip action** (§6:
   "this component counts and trips; the Gate decides"), so a tripped breaker is also visible + un-pausable
   through the existing dashboard pause button. (Putting the `is_paused` write inside `evaluate` would
   break the read-only invariant and, worse, make `GET /gate/preview` pause a campaign - a side-effecting
   preview bug. It is deliberately excluded.)
5. **CAN-SPAM completeness → HOLD `'can_spam_incomplete'`** - confirm an approved template exists for the
   campaign AND carries unsubscribe + postal tokens AND `Settings.postal_address`/sender identity are
   non-empty (via `compliance.assert_can_spam_complete`, which validates the **rendered** output, not just
   token presence). This is a **transient config state** (a missing/unapproved template is fixable, not a
   permanent suppression), so it is a **HOLD** (re-queued when the template is approved), **not** a BLOCK -
   BLOCK is reserved for suppression-class permanence. It sits below the switches so a kill-switch HOLD is
   not masked by a config gap, and so the SENDER's BLOCK branch (§5b-D) stays purely the suppression path.
6. **quiet_hours → HOLD `'quiet_hours'`** - resolve the recipient's `practice_state` **inside `evaluate`**
   via `npi → prospect.practice_state` (one indexed lookup at the chokepoint; the caller passes only
   `npi/email/campaign/when/mailbox`, never `practice_state`), then `practice_state → tz` (STATE_TZ map
   covering 50 states + DC; multi-TZ states pick the widest quiet window; **blank/unknown state → HOLD**,
   fail-safe, never assume a TZ); if `when` (local) is outside `[start,end)` or weekend → HOLD. Skipped when
   `when=None`.
7. **warmup_cap_exceeded → HOLD `'warmup_cap_exceeded'`** - count today's **sent** outbound Messages
   (`mailbox_id = mailbox.id AND direction='outbound' AND sent_at IS NOT NULL AND sent_at::date = today`)
   vs `mailbox.daily_cap`. Filtering on `sent_at IS NOT NULL` excludes pending/rolled-back idempotency rows
   so they don't distort the cap (conservative). Skipped when `mailbox=None`.
8. else **ALLOW**.

Banned-claims is **out of scope** for the Gate - the linter (§4c) owns it. Because the new checks are
skipped when `when`/`mailbox` are absent, `/gate/preview` and every Phase 0 test behave identically.

### 5b. The deterministic SENDER (`certuma/sender.py`, no LLM)

`run_once(session, limit)`:
- **A. Claim.** `SELECT lead JOIN contact/prospect/workflow_score WHERE activation_status='sendable' AND
  (next_action_at IS NULL OR next_action_at<=now()) AND contact.email_status='valid' ORDER BY` queue rank,
  `FOR UPDATE SKIP LOCKED LIMIT n`. The `IS NULL OR` guard is belt-and-suspenders against an unstamped
  `next_action_at` (the enricher stamps `now()` on `→sendable`, §3 - but `NULL <= now()` is false in SQL,
  so without this guard an unstamped lead is invisible forever). Use `lead.version` as `expected_version`;
  require `lead.claim_url` (missing → `needs_review`, not a send).
- **B. Mailbox.** pick a mailbox with remaining warmup headroom (round-robin among active mailboxes).
- **C. Gate.** `gate.evaluate(session, npi=npi, email=email, campaign=campaign, when=now,
  mailbox=mailbox)` (mailbox carries `id` + `address`; `practice_state` is resolved inside the Gate from
  `npi`, §5a-6).
- **D. BLOCK.** Only suppression reaches BLOCK now (CAN-SPAM is a HOLD, §5a-5). Sender skips + logs;
  suppression handling stays in the suppression flow. No transition.
- **E. HOLD.** Re-queue **without transition**: set `lead.next_action_at = now + backoff(reason)`
  (`quiet_hours` → next window open; `warmup_cap` → tomorrow; `can_spam_incomplete` → re-check when a
  template is approved; breaker/kill/pause → short retry), commit, emit `'send_skipped_hold'`.
  `lead.activation_status` and `lead.version` are **unchanged** - the Phase 0 HOLD-is-a-no-op invariant.
  The Gate stays read-only; the Sender owns the re-queue clock.
- **F. ALLOW.** Ensure `Thread` (create if absent, **generating a random `reply_token`** - e.g.
  `secrets.token_urlsafe`; abort if `is_locked`); render body+subject from the approved template
  (deterministic merge incl. `claim_url`/`unsubscribe_url`/`postal_address`); build the `EmailMessage` via
  `message.build_outbound`: `From: "<name>, <title>" <sender_from_email>`, `Reply-To:
  reply+<reply_token>@<reply_to_domain>` (the **random `thread.reply_token`, NOT the integer `thread_id`** -
  the raw sequential id is an enumeration/IDOR concern and ignores the purpose-built unique
  `thread.reply_token` column, models.py L175; inbound correlation keys off `reply_token`), **RFC 8058**
  `List-Unsubscribe: <mailto:...>, <https://...>` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click`,
  postal-address footer.
- **G. Transition (idempotency BEFORE send).** `ledger_writer.transition(lead.id, 'email_sent',
  actor='sender', reason_code='first_touch_sent', expected_version=lead.version,
  idempotency={lead_id, thread_id, npi, campaign, cadence_step, direction:'outbound', subject,
  body_rendered, variant_id, mailbox_id})` - inserts + flushes the Message **before** return; on a
  duplicate `(npi,campaign,cadence_step)` outbound the flush raises `IntegrityError` on `uq_msg_idem_outbound`.
  **`mailbox_id` is a NOT-NULL-free (nullable) column added in migration 0003** (§8); the
  `Message(**idempotency)` constructor will raise `TypeError` on the unknown kwarg if the 0003 model-mirror
  has not been applied - so **the SENDER (P1.4) hard-depends on the migration (P1.1)**, not just on the
  provider/Gate (made explicit in §10). When no mailbox is selected, pass `mailbox_id=None` (the column is
  nullable). On the duplicate IntegrityError: **roll back the (poisoned) session first**, then skip
  (already sent). `sendable→email_sent` is already a legal edge (status.py L67); `'sender'` ∉
  `ACTIVATION_ONLY_ACTORS` so it can never set `physician_activated`.
- **H. Send.** `provider.send(msg)`; on `ProviderError` roll back the whole unit (Message + transition both
  reverse - not yet committed), advance `next_action_at` for retry, do **not** leave `email_sent` without a
  send.
- **I. Commit.** On success set `Message.esp_message_id` + `sent_at=func.now()`, then `session.commit()`.
- **J. Cadence.** advance `cadence_step`/`next_action_at` for the follow-up scheduler (Phase 3 builds full
  cadence; Phase 1 only needs first-touch).

**Crash-safety (scoped honestly).** The idempotency Message is inserted+flushed inside one uncommitted txn
**before** `provider.send`, and the txn commits only after a successful send + `esp_message_id`. A crash
*before* commit rolls back the Message **and** the transition together, freeing the idempotency key for a
safe retry. The one residual at-most-once gap is "ESP accepted but the DB then rolled back" - but **for
Mailpit (Phase 1) this gap is benign and there is no reconciliation job in scope**: the local SMTP capture
loses nothing meaningful, and by definition nothing persisted to reconcile against. A real outbox/pending-send
row + a reconciliation sweeper is **explicitly deferred** to the cold-ESP cutover (§12) and is **not**
claimed as a Phase-1 mitigation. For the real ESP, `execute_approved_send` also moves to a background worker
that owns the txn (sync is fine for the Mailpit Assisted demo).

---

## 6. Events & activation

The **no-LLM "Monitor" node** + the **claim_url Poller**. Never calls Claude.

### 6a. Ingestion (`certuma/email/ingest.py`)

- **RECEIVE** (`api/webhooks.py` `POST /webhooks/email/{provider}`): `provider.verify_signature(raw,
  headers)` → 400 on bad signature; persist raw + return 200 fast (normalize out-of-band so a slow
  transition can't trigger a provider retry-storm). For Phase 1's warmup volume, normalize synchronously
  behind a savepoint is acceptable (Redis/RQ is the scale path - open decision §12).
- **NORMALIZE**: `provider.parse_events(raw)` → `list[NormalizedEvent]` mapping provider shapes to the
  canonical `event_type` CHECK set (`delivered/opened/replied/bounced/complained/activated/opt_out/
  unsubscribe_click/sent`, models.py L204-208).
- **DEDUP + PERSIST**: per event, `event_dedup_key(provider, provider_event_id)`; insert `Event(dedup_key,
  ...)` in a savepoint; `IntegrityError` on `uq_event_dedup` (0001 L237) ⇒ duplicate ⇒ skip (at-least-once
  safe). `METRICS.incr('event_ingested', result='accepted|duplicate|unmatched')`.
- **CORRELATE**: look up `Message` by `esp_message_id`; flip `message.delivered/bounced/complained` in the
  same txn. Unmatched → emit `'event_unmatched'` + triage counter (still stored, not transitioned; a
  complaint can key off `recipient_email` even without a message match so it is never silently lost).
- **DRIVE TRANSITION**: `delivered` on an `email_sent` lead → `transition(..., 'awaiting_reply',
  actor='monitor', reason_code='delivered')`; inbound reply → create a `direction='inbound'` Message
  **carrying the provider's message-id in `esp_message_id`** (so the `uq_msg_inbound_esp` partial-unique
  index, `ON message(esp_message_id) WHERE direction='inbound' AND esp_message_id IS NOT NULL`, 0001 L214-216,
  structurally enforces at-most-once inbound Message creation on a replayed webhook - the Event-level dedup
  alone is not enough because the inbound Message has its own index), correlated to the thread by
  `reply_token`, then `transition(awaiting_reply/email_sent → 'replied', actor='monitor')`. **The `→replied`
  ledger edge lives in this model-free ingestion component (Phase 1 scope); the `replied → interested`
  *classification* is a Phase 2 LLM node and is NOT done here** - so ingestion stays LLM-free while a real
  reply still advances the ledger. Each call passes the lead's current version and catches
  `ConcurrencyConflict` (re-read + retry once) so at-least-once, out-of-order webhooks are safe; an
  `IllegalTransition` from a stale event (e.g. `opened` after `replied`) is logged as a no-op, not a crash
  (the `ALLOWED_TRANSITIONS` graph rejects backwards moves).
- **OPT-OUT / UNSUBSCRIBE (deterministic, never LLM-gated - the core compliance promise).** An `opt_out`
  or `unsubscribe_click` event (both in the `event_type` CHECK, models.py L206; emitted by the RFC 8058
  one-click `List-Unsubscribe-Post` POST the SENDER builds and by reply-based unsubscribes) →
  `suppress_address(npi, email, reason='opt_out', source='event_ingest')` (idempotent `Suppression` by `npi`
  AND `email`) → `transition(..., 'do_not_contact', actor='monitor', reason_code='opt_out')`
  (`do_not_contact` is a legal stop from `email_sent`/`awaiting_reply`/`replied`/etc. and is terminal). This
  is the deterministic opt-out handler: the one-click unsubscribe link is *sent* by the SENDER, and its
  inbound POST has a documented suppression path that is structurally not routable through any LLM. A later
  `gate.evaluate` for that npi/email returns `BLOCK suppression`.
- **HARD-BOUNCE**: `suppress_address(npi, email, reason='hard_bounce', source='event_ingest')` (instant,
  non-gated; writes `Suppression` by `npi` AND `email` so `gate._is_suppressed`'s OR matches both this and
  any sibling lead at that address) → set `lead.needs_reenrich=true` → `transition(..., 'exhausted',
  actor='monitor')` (a legal stop from email_sent/awaiting_reply, status.py L68-73). **Complaint**:
  `suppress_address(..., reason='complaint')` + same stop + feed the breaker.
- **BREAKER FEED + TRIP ACTION**: every bounce/complaint updates the rolling source; `breaker_window`
  exposes `complaint_rate`/`bounce_rate` per domain+campaign. **This component counts AND trips; the Gate
  only reads/decides** (clean separation, and the only place the pause *write* lives). When a window crosses
  the trip threshold (complaint > 0.1% / bounce > 2%) with hysteresis (§8 `circuit_breaker_state`), the
  ingest-side trip action **writes `campaign.is_paused=true`** (the same column the dashboard pause button
  toggles, so it's visible + un-pausable through the existing UI, and is read by the Gate's `campaign_paused`
  step). The Gate's `circuit_breaker_*` reasons are read-only signals; the *pause write* is owned here, so
  `gate.evaluate`/`GET /gate/preview` never mutate state (§5a-4).

`Suppression.reason` and `event_type` need **no new CHECK values** - verified both already include
`hard_bounce`/`complaint`/`opt_out` (models.py L225) and `activated`/`opt_out`/`unsubscribe_click`/etc.
(L205-207).

### 6b. The claim_url Poller (`certuma/poller/claim_poller.py`)

- `select_due_leads`: non-terminal leads with a `claim_url`, `last_polled_at` older than the interval,
  ordered by `activation_priority`. In Phase 1 the reachable pre-activation states are `email_sent` and
  `awaiting_reply` (a real send lands in one of these); the poller also handles `interested` for forward
  compatibility with the Phase 2 reply path.
- `run_once(session, *, fetch, now)`: call `certuma.publish.claim_status.poll_claim_urls(items=[(npi,
  lead.claim_url)], fetch=fetch)` with an **injected** fetch (real Certumalink read API when it exists;
  until then `default_fetch` raises `ClaimStatusUnavailable` → record a `'source_unavailable'` heartbeat,
  `METRICS.incr('poll_run', result='source_unavailable')`, never coerce to "no click").
- On a click status: write `Event(event_type='activated', dedup_key=activation_dedup_key(npi, campaign))`
  in a savepoint; if the key already exists → no-op (re-poll cannot re-fire). Otherwise promote in **one
  unit of work** (the claim-click is the activation signal, independent of reply state): if the lead is in
  `email_sent` or `awaiting_reply`, first `transition(lead.id, 'interested', actor='poller',
  reason_code='claim_clicked', expected_version=lead.version)` - the **NEW deterministic edge added to
  `ALLOWED_TRANSITIONS`** (§2/§8), reachable from a real Phase-1 send; if already `interested`, skip this
  step. Then `transition(lead.id, 'physician_activated', actor='poller', reason_code='claim_clicked',
  expected_version=<the bumped version returned by the first transition>)` and stamp
  `lead.activation_detected_at=now`. Both writes are `actor='poller'`, same txn, same poll; the
  `activation_dedup_key` Event guarantees the whole promotion runs at most once.
- `lead.last_polled_at=now` is stamped on **every** attempt (incl. failures) so due-selection always
  advances and the poller can't hot-loop a single lead; commit per lead so one failure doesn't roll back
  the batch.

**Belt-and-suspenders for the sole conversion metric:** (1) `actor='poller'` ∈ `ACTIVATION_ONLY_ACTORS`
(any other actor → `IllegalActor`, ledger_writer L75-77); (2) `physician_activated` is terminal
(`assert_transition` rejects any move out of it, status.py L106-107); (3) the deterministic
`activation_dedup_key` makes a re-poll collide on `uq_event_dedup` and short-circuit **before**
`transition` is even called. A spoofed email webhook can at worst suppress an address (fail-safe), never
fabricate a conversion - activation is only ever taken from the poller/webhook actor.

---

## 7. Orchestrator + approval flow (the Assisted loop)

The durable per-lead workflow glue. Deterministic plumbing; Claude (COPYWRITER) only proposes - the
linter + Gate + Policy Engine + human dispose.

**`orchestrator/loop.process_lead(session, lead_id)`** dispatches on `activation_status`:
`not_contacted/queued_today` → enrich (§3); `sendable` (no `claim_url`) → publish draft + capture claim_url;
`sendable` (with claim_url) → draft (§4) + lint + policy + enqueue. Idempotent and re-entrant. On
`ConcurrencyConflict`/`IllegalTransition` it re-queues (HOLD-like no-op), never crashes the worker.

**`orchestrator/policy.classify_action(proposed_action, value_tier, model_confidence, autonomy_level)`**
(pure function of the locked decisions):
- `assisted` → **always `ENQUEUE_APPROVAL`** (every send human-approved - Phase 1 launch).
- `supervised` (future) → `AUTO_EXECUTE` unless first-touch | high-value | `model_confidence < 0.8`.
- `autonomous` (future) → `AUTO_EXECUTE` unless escalation.
- `value_tier='high'` iff `activation_priority=='high' AND practice_group_size>=3`.

**Enqueue (`orchestrator/approvals.enqueue_approval`).** Re-run `gate.evaluate`; on `BLOCK` →
suppress/skip (no approval); on `HOLD` → leave lead, advance `next_action_at`, emit reason; on `ALLOW` →
insert `Approval(state='pending', proposed_subject, proposed_body, value_tier, model_confidence,
gate_reason_code, sla_expires_at=now+SLA)`. The lead stays `sendable` (no transition) until approved.

**Make the dashboard approval actually send.** `api/app.py` currently only flips `appr.state` + naively
`db.commit()` (L110-121) with **no `try/except`**. Rewire `decide()`:
- `approved` → within the same request txn call `orchestrator.approvals.execute_approved_send(db, approval)`
  (= the SENDER path: re-Gate, render, `ledger_writer.transition` with idempotency, `provider.send`,
  back-fill `esp_message_id`), then set `appr.state='approved'`/`decided_by`/`decided_at` and commit.
- **Poisoned-session + double-approve safety (load-bearing on the HTTP path).** `execute_approved_send`
  runs inside the FastAPI request session (`get_db` yields one session, app.py L32-38). If the idempotency
  flush inside `transition()` raises `IntegrityError` (the same `(npi,campaign,cadence_step)` was already
  sent - e.g. the approval is clicked twice), the SQLAlchemy session is **poisoned**: no statement can run
  until rollback. `execute_approved_send` therefore **catches `IntegrityError`, `db.rollback()` FIRST**,
  then re-reads the already-sent Message/lead, marks the approval `approved` **idempotently** (the prior
  send already happened - do not call `provider.send` again, do not re-transition), and returns
  "already sent." Without the rollback-first ordering, the subsequent `appr.state='approved'` write throws
  `PendingRollbackError`. This makes "approve the same approval twice" safe (DoD-6) and is covered by an
  explicit test (§9).
- On a re-Gate `HOLD` (kill-switch flipped while queued), keep `Approval` **pending** (do not mark
  approved), record `gate_reason_code`, emit, re-queue - surface "approved-but-held: <reason>". On a re-Gate
  `BLOCK` (suppression appeared while queued, e.g. an opt-out), do **not** send; mark the approval
  terminally and stop the lead per the suppression flow. Never silently drop, never auto-send later without
  the gate clearing.
- `edited` → persist edited `proposed_subject/proposed_body`, **re-lint the edited body** (§4c), then the
  same send path using the edited body. A failed re-lint keeps the approval pending with the lint reason.
- `rejected` → `transition(sendable → needs_review | exhausted, actor='dashboard:<user>')`, no send. (Note:
  a *human opt-out signal* discovered at review time is not a "reject" - it routes through the deterministic
  opt-out handler, §6a: `Suppression(reason='opt_out')` + `→ do_not_contact`, never `needs_review`.)
- Add `GET /approvals/{id}` returning `proposed_subject/proposed_body` + physician context (currently
  omitted) so the queue UI can show the message.

`/kill-switch` and `/campaigns/{name}/pause` already feed `gate.evaluate`, so they gate the approve-send
automatically.

**Scheduler / eligible queue (`orchestrator/scheduler.py`).** `build_eligible_queue(session, now)` selects
due leads (`next_action_at<=now`, non-terminal), builds `QueueItem`s (joining `workflow_score` for
priority/score, filtering `contact.email_status='valid'`), calls `certuma_core.queue.rank_queue`
**verbatim**, then layers Phase-1-only concerns **around** it (not inside): per-owner daily caps +
quiet-hours pre-filter (cheap, to avoid drafting work the Gate would HOLD anyway). `claim_due(session,
limit)` claims via `SELECT ... FOR UPDATE SKIP LOCKED`. **Single source of truth per concern:** the Gate
owns authoritative send-time compliance and re-decides before send; the scheduler's pre-filters only avoid
wasted drafting; the Policy Engine never re-implements compliance.

**SLA hold + escalate (`approvals.sla_sweep(session, now)`).** Marks expired `pending` items
`state='expired'`, emits `'approval_sla_expired'`, performs **no transition and no send** (lead untouched).
A second threshold escalates to the backup approver (`app_user.role='backup'`) - decision #4: HOLD +
escalate, never auto-send.

---

## 8. Data model changes & migrations (Alembic `0003_phase1`)

The Phase 0 schema (0001) was deliberately shaped for this loop, so 0003 is **small and additive**
(nullable/defaulted columns + a couple of new tables). Rule respected: **migration owns DDL; `models.py`
mirrors it.** `down_revision='0002_seed'`. Keeps the 0002 seeded template valid.

**Code-vs-DB note on the activation edges (§2).** The two new graph edges (`email_sent → interested`,
`awaiting_reply → interested`) are a **`certuma_core/status.py` code change, NOT a migration** - the DB's
`lead_status_valid` CHECK constrains only the *state enum* (`activation_status IN (...)`), and `interested`
is already a legal value (models.py L32-36); transitions are enforced **only** in Python by
`assert_transition` against `ALLOWED_TRANSITIONS`. So no DDL touches the status CHECK; the graph edit ships
with the `certuma_core` package (P1.1's sibling code change, called out in §10).

**Required (for the §5 warmup/breaker Gate checks + §6 re-enrich):**
- `mailbox` table - `(id, address citext unique, display_name, title, is_active bool, warmup_started_on
  date, daily_cap int)`. The per-mailbox warmup-cap source the Gate counts against. No mailbox roster
  values are seeded (blocked on the cold-domain/named-employee inputs); the table + a single dev mailbox
  for Mailpit is enough for Phase 1.
- `message.mailbox_id BIGINT NULL FK→mailbox(id)` - per-mailbox attribution for the warmup count; the
  Sender writes it as part of the idempotency mapping. Nullable for legacy rows.
- `lead.needs_reenrich BOOLEAN NOT NULL DEFAULT false` - set by the hard-bounce path; a cheap WHERE clause
  for the enrichment worker. (`Event(event_type='bounced')` records the fact but isn't an actionable flag.)
- `circuit_breaker_state` table - `(campaign, kind CHECK('complaint'|'bounce'), is_tripped bool,
  window_start, sample_count, rate numeric, tripped_at, cleared_at)`. Persists rolling-window state +
  hysteresis so a single late/out-of-order complaint webhook doesn't flap the campaign on/off; powers the
  deliverability panel. (Could be computed on the fly, but persisting gives an auditable, stable signal -
  recommended.)
- **Index review** (verified against 0001 - add the missing ones): `ix_event_occurred_at` on
  `event(occurred_at)` (**absent in 0001** - needed for the rolling breaker window) and
  `ix_msg_esp_message_id` on `message(esp_message_id)` for **outbound** webhook correlation (0001's
  `uq_msg_inbound_esp` is a *partial* unique index `WHERE direction='inbound'`, 0001 L214-216 - it does
  **not** index outbound rows, which the delivery/bounce/complaint correlation looks up). 0001 already has
  `uq_event_dedup` (L237), `ix_event_type` (L239), `ix_event_lead` (L238), `ix_msg_lead`/`ix_msg_thread`
  (L218-219), and the inbound-esp partial unique - so add `ix_event_occurred_at` + the outbound
  `ix_msg_esp_message_id` to keep correlation + the breaker query `O(window)`.

**For template governance + attribution (§4):**
- `template.created_by BIGINT NULL FK→app_user(id)` - authoring attribution (`approved_by` already exists).
  Nullable so the 0002 system-seeded row stays valid.
- `template.variant_label TEXT NOT NULL DEFAULT 'a'` - so `variant_id = campaign:version:variant_label` is
  deterministic and unique per send for `Message.variant_id` idempotency + per-variant analytics. Default
  `'a'` keeps single-variant campaigns trivial.
- partial index `ix_template_active ON template(campaign) WHERE is_approved` - makes "latest approved
  template for campaign X" a single indexed read (the Copywriter does it per draft).

**Optional / encode-in-string-interim (defer the migration if desired):**
- `contact.is_role_address BOOLEAN DEFAULT false` + `contact.discovery_source TEXT` - make role-address
  demotion + waterfall-source first-class & queryable. If deferred, encode both in the `verifier` string
  (`'zerobounce/role'`, `'heuristic_pattern'`). Low-risk additive; trades query-ability vs. a migration.

**Explicitly NOT changed (DDL):**
- **No new `event_type` or `suppression.reason` CHECK values** - the loop uses only existing enum values
  (verified: `hard_bounce`/`complaint`/`opt_out` and `activated`/`opt_out`/`unsubscribe_click`/`delivered`/etc.
  already present, models.py L205-207, L225).
- **No change to the `lead` status CHECK** - `sendable`, `needs_review`, `exhausted`, `interested`,
  `physician_activated` already exist; no new states. (The two new `…→interested` *transition* edges are a
  `certuma_core/status.py` code change, not a DDL change - see the code-vs-DB note above.)
- **No new `lead` column for scheduling** - `next_action_at` already exists (models.py L159); Phase 1 only
  *populates* it (enricher stamps `now()` on `→sendable`; the Sender's claim query handles NULL defensively).
- **No new approval-state table** - template approval is just `template.is_approved/approved_by` + audit
  rows; per-send approval reuses the existing `Approval` table. Reuse, don't duplicate.
- **No new `Contact` column for the happy path** - `(npi, email, email_status, verifier, verified_at)` +
  the `npi_email` unique index already support UPSERT + NPI-keyed caching.
- **No `Thread`/`Message`/`Lead` change to bypass the single-writer contract** - the Copywriter only
  *produces* `variant_id/subject/body_rendered`; the SENDER inserts the Message via `ledger_writer`.
  Warmup/per-rep cap counts derive from `message.sent_at` + `lead.owner` (no extra log table).
- **Quiet-hours window** stays a code constant in `compliance.py` (e.g. Mon-Fri 08:00-17:00 local) with a
  documented per-campaign override seam - lowest-risk at launch, no migration.

---

## 9. Testing strategy

All new DB tests mirror `tests/db/test_ledger_writer.py`: `try/except` import of SQLAlchemy → `HAVE_SA`;
`@unittest.skipUnless`; `DB_URL` from `CERTUMA_DATABASE_URL` or `Settings().database_url`; per-test
rolled-back session; `SkipTest` if Postgres unreachable or schema un-migrated. Pure tests
(`certuma_core.linter`, `compliance`, `policy`, `verdict.classify`, `breakers` math) need no DB / no
network / no LLM. The `METRICS` counters are the assertable signal (per `observability.py`).

**Pure (no DB, no network, no LLM):**
- `linter`: reject missing `{unsubscribe_url}`; missing postal address; each banned claim (`verified`,
  `board-certified`, `credentialed`, `endorsed`, `licensed`, `approved`) incl. case/word-boundary variants
  AND benign near-misses that must PASS (e.g. "approve your draft"); a fabricated fact not in
  `allowlist_literals` (wrong city, invented credential, hospital name); a model-altered `claim_url`; a
  missing required token; **allow-list corpus tests** - a render containing the `campaign.pitch_angle`
  string PASSES (it is in `allowlist_literals`, not in `SeedFacts`), a render containing the sender-identity
  name PASSES, and a multi-word city / pitch-angle phrase is not tripped token-by-token; PASS on the
  0002-seeded compliant template rendered with a real `SeedFacts`+corpus. Lint subject + body + plaintext
  uniformly.
- `verdict.classify`: pinned-literal table of provider sub-codes → the 5 statuses; role-address demotion
  (`info@`/`office@` capped at `risky` even when provider says valid); `catch_all → catch_all`.
- `compliance`: `assert_can_spam_complete` returns the `can_spam_incomplete` reason on empty
  `Settings.postal_address` / missing token (the Gate maps it to **HOLD**, not BLOCK - §5a-5);
  `quiet_hours_blocked` parametrized over `practice_state` TZ (CA→America/Los_Angeles, NY→
  America/New_York) with injected `when` - HOLD at 02:00 local, ALLOW at 10:00 local; blank state → HOLD.
- `policy.classify_action`: `assisted` always `ENQUEUE_APPROVAL`; `value_tier='high'` iff
  `priority=='high' AND group>=3`; `confidence<0.8` path asserted for `supervised` (moot in Assisted).
- `copy_schema`: `variant_id` derivation stable (`campaign:version:variant_label`); Pydantic/json_schema
  round-trips the strict shape; `merge_token_audit` referencing a token not in `SeedFacts` is flagged.

**LLM-node tests with a fake model (`StubCopyProvider`, no network, skip-without-DB friendly):**
- happy path lint PASS → `DraftResult.ok`; banned-claim from stub → one retry-with-feedback → success;
  persistent banned-claim → hard-fail `needs_review` (no Message produced, no `transition` called - assert
  `ledger_writer` is never invoked); no approved template for campaign → refuse.
- cost guard: the model-tier policy picks `claude-sonnet-4-6` for `value_tier ∈ {medium,low}` and
  `claude-opus-4-8` for `high`.
- live prompt-cache (optional, gated on `ANTHROPIC_API_KEY`): two consecutive calls with the same
  template+system prefix → second call `usage.cache_read_input_tokens > 0`.

**Enrichment (FixtureEnricher, no network/spend):**
- waterfall order (healthcare hit short-circuits b2b/pattern; pattern only fires when prior tiers miss AND
  a domain exists); VALID-ONLY filter (`valid → sendable`; each of risky/catch_all/unknown/invalid + no-
  candidate → `needs_review`, asserted via audit_log + `lead.activation_status`, contact row still parked);
  budget/priority (high-value enriched first, low left `not_contacted`, `enrich_provider_call == cap`,
  never overspends); pay-once cache (re-run → zero new provider calls, `enrich_cache_hit++`, stale
  `verified_at` triggers exactly one re-verify); suppression pre-check (suppressed candidate dropped before
  verify - no verify call, no `valid` Contact row); funnel-math (100 fixture NPIs, 35 found / 18 valid →
  `/enrichment/funnel` reconciles to the counters).

**Gate / SENDER / circuit-breaker (DB):**
- **Gate contract preservation:** existing Phase-0 gate tests still pass; a test that
  `when=None, mailbox=None` makes the new checks no-ops so `/gate/preview` is unchanged.
- **Gate is read-only:** `/gate/preview` (GET) over a campaign whose breaker window is over-threshold returns
  `HOLD circuit_breaker_complaint` **and does NOT flip `campaign.is_paused`** (assert the column is unchanged
  after a preview) - proves the pause write is not inside `evaluate` (§5a-4, §6).
- **CAN-SPAM HOLD:** template lacking `{unsubscribe_url}`/`{postal_address}` or empty
  `Settings.postal_address` → `HOLD can_spam_incomplete` (a re-queueable config state, **not** BLOCK); add
  them → ALLOW. Assert ordering: with kill-switch ON *and* a missing template, the Gate returns the
  kill-switch HOLD is not masked (kill_switch precedes can_spam_incomplete, §5a).
- **Quiet hours / warmup cap:** as above; quiet-hours resolves `practice_state` from `npi` inside the Gate
  (seed `prospect.practice_state` and assert the lookup drives the decision); `mailbox.daily_cap=2` + 2
  **sent** outbound today (`sent_at IS NOT NULL`) → `HOLD warmup_cap_exceeded`; cap=3 → ALLOW; a
  pending/rolled-back idempotency row (`sent_at IS NULL`) does **not** count.
- **Circuit breaker (trip write owned by ingest, not the Gate):** feed bounce/complaint events to push the
  window rate > 0.1% → the **ingest trip action** flips `campaign.is_paused=true`; the Gate then returns
  `HOLD campaign_paused`/`circuit_breaker_complaint` read-only; hysteresis: rate below clear-threshold →
  breaker clears.
- **SENDER happy path against Mailpit** (`provider='mailpit'`, SMTP `127.0.0.1:11025`): `run_once` on one
  `sendable` lead → (a) a Message row with `esp_message_id` + `sent_at` + outbound idempotency +
  `mailbox_id`, (b) lead `sendable→email_sent`, version bumped, audit_log `actor='sender'`, (c)
  `GET :18025/api/v1/messages` shows one email with `List-Unsubscribe` + `List-Unsubscribe-Post` headers,
  postal address in body, `Reply-To=reply+<reply_token>@<domain>` (the random token, asserted to NOT equal
  the integer `thread_id`), the exact `claim_url`, and **no banned claim**.
- **Idempotency / no double-send:** run twice → second insert raises `IntegrityError` inside `transition()`;
  Sender rolls back, does **not** call `provider.send` again, does not re-transition; Mailpit shows exactly
  one message.
- **Provider failure atomicity:** inject a transport raising `ProviderError` → no `email_sent` persists
  (rollback), lead stays `sendable`, `next_action_at` advanced, no orphan Message with NULL
  `esp_message_id` committed.
- **HOLD re-queue invariant:** force each HOLD reason → `lead.activation_status` and `lead.version`
  unchanged while `next_action_at` moved forward; no transition, no Message.
- **Firewall/secrets:** Sender/Gate read sender identity / cold_domain / postal_address / smtp creds only
  through `Settings`, never `os.environ`; `ColdEspProvider` unused in dev.

**Status graph (pure, no DB - guards the one core-graph change):**
- `status_new_edges`: assert `is_legal_transition('email_sent','interested')` and
  `is_legal_transition('awaiting_reply','interested')` are now True (the two added edges), while every
  pre-existing edge is unchanged and no edge was removed (diff `ALLOWED_TRANSITIONS` against the Phase 0
  snapshot); `interested → physician_activated` still legal; terminals still terminal.

**Events & activation (DB + FakeProvider for the paths Mailpit can't do):**
- `ingest_dedup` (same event twice → one Event, one transition, `event_ingested{result=duplicate}=1`);
  `inbound_message_dedup` (same inbound reply webhook replayed → the `direction='inbound'` Message carries
  `esp_message_id` so `uq_msg_inbound_esp` raises on the replay → still exactly one inbound Message);
  `delivered_drives_awaiting_reply`; `hard_bounce_suppresses_and_stops` (Suppression(hard_bounce) +
  `needs_reenrich=true` + `→exhausted` + a later `gate.evaluate` returns `BLOCK suppression`);
  `opt_out_suppresses_and_stops` (an `opt_out`/`unsubscribe_click` event → `Suppression(reason='opt_out')`
  + `→do_not_contact` via `actor='monitor'`, **no LLM in the path** + a later `gate.evaluate` returns
  `BLOCK suppression`); `complaint_feeds_breaker_and_trips_pause` (complaint over threshold → ingest-side
  trip flips `campaign.is_paused=true`); `out_of_order_and_concurrency` (bump version between read &
  transition → one retry converges, no double transition); `unmatched_event_triaged` (no transition, Event
  stored, `event_unmatched=1`).
- `poller_activates_from_real_send`: seed a lead in **`email_sent`** (the state a real Phase-1 send lands
  in - **not** seeded directly in `interested`), with `claim_url`; a fake fetch returns a click → the poller
  promotes `email_sent → interested → physician_activated`, both audit rows `actor='poller'`,
  `activation_detected_at` + `last_polled_at` set, exactly one `Event('activated')`. A variant seeds
  `awaiting_reply` to exercise that edge. This is the test that proves activation is reachable from the
  actual send loop. `poller_reentrant_terminal` (run twice → dedup-key collision → still one Event, status
  stays `physician_activated`, no `IllegalTransition`); `poller_actor_guard` (a non-poller actor attempting
  `→physician_activated` → `IllegalActor`); `poller_source_unavailable` (`default_fetch` → no transition,
  `last_polled_at` stamped, `poll_run{result=source_unavailable}=1`).
- `adapter_interface_parity` (FakeProvider + MailpitProvider both satisfy the EmailProvider Protocol).
- `mailpit_roundtrip` (integration, `HAVE_DOCKER`/Mailpit gate): send via MailpitProvider → read back via
  REST → synth `delivered` → ingest → assert `awaiting_reply`. Skips cleanly when Mailpit absent.

**Approval HTTP-path safety (DB):**
- `approve_twice_is_safe`: enqueue → `POST /approvals/{id}/decision {approved}` sends; a **second** approve
  of the same approval hits the idempotency `IntegrityError` inside `transition()`, `execute_approved_send`
  rolls back the poisoned session **first**, re-reads the sent state, returns the same terminal result with
  **no** second `provider.send` and **no** `PendingRollbackError`; Mailpit shows exactly one message
  (blocker-class regression test for the HTTP path).

**The smallest end-to-end Assisted slice (the headline test, Mailpit, skip-without-DB):** seed 1
`is_active` campaign + 1 `is_approved` template + 1 prospect (with `practice_state` set) +
`contact.email_status='valid'` + 1 lead in `sendable` with a `claim_url` and `next_action_at=now()`.
(1) `process_lead` → exactly one `Approval(state='pending')`, lead still `sendable`, `lead.version`
unchanged. (2) `POST /approvals/{id}/decision {approved}` → lead `→email_sent`, exactly one outbound Message
with `esp_message_id` + `mailbox_id`, `Approval.state='approved'`, Mailpit shows exactly one email
containing the `claim_url`, an unsubscribe link, the postal address, and `Reply-To` keyed on the random
`reply_token`. (3) double-fire guard: `execute_approved_send` twice → second `IntegrityError` → rollback-first
→ only one Mailpit message, version bumped once. (4) Gate-on-approve: flip kill-switch ON after enqueue,
then approve → **no send**, Approval stays pending/held, Mailpit empty (proves `gate.evaluate` is re-checked
at send time). (5) ingest a synthesized `delivered` → lead `→awaiting_reply`. (6) `claim_poller.run_once`
with a fake `fetch` returning a click → `awaiting_reply → interested → physician_activated`, **reachable
from the send in step 2** (the lead was never seeded in `interested`). This step is the proof that DoD-9's
sole conversion metric is reachable end-to-end against code the plan actually builds.

---

## 10. Phased sub-steps (ordered, dependency-aware → PRs)

Sizes: **S** ≈ 1 PR / ≤ ~1 day, **M** ≈ 1-2 PRs, **L** ≈ 2-3 PRs. Each ships green, skip-without-DB.

**P1.0 - Settings + provider seam (S).** Extend `Settings.from_env` with cold-ESP/provider siblings
(`CERTUMA_ESP_API_KEY` already exists; add `CERTUMA_COLD_DOMAIN`, `CERTUMA_SENDER_FROM_NAME/TITLE/EMAIL`,
`CERTUMA_REPLY_TO_DOMAIN`, `CERTUMA_POSTAL_ADDRESS`, `CERTUMA_EMAIL_PROVIDER` default `'mailpit'`,
`CERTUMA_SMTP_HOST/PORT` default `127.0.0.1:11025`, `CERTUMA_ENRICH_API_KEY`, `CERTUMA_VERIFY_API_KEY`).
*Accept:* firewall test - business logic reads these only via `Settings`, never `os.environ`.

**P1.1 - Migration 0003 + model mirror + status-graph edges (M).** Add `mailbox`, `circuit_breaker_state`,
`message.mailbox_id` (nullable), `lead.needs_reenrich`, `template.created_by/variant_label`,
`ix_template_active`, `ix_event_occurred_at`, the outbound `ix_msg_esp_message_id`; mirror in `models.py`.
**Also add the two `certuma_core/status.py` `ALLOWED_TRANSITIONS` edges** (`email_sent → interested`,
`awaiting_reply → interested`) - a pure code change (no DDL; the status CHECK already lists `interested`),
shipped here because the SENDER (P1.4) and poller (P1.10) both depend on the new `message.mailbox_id`
column **and** the new edges. *Accept:* the drift-guard schema test passes; `status_new_edges` pure test
passes (additive only, no edge removed); 0002 seed template still valid; `alembic upgrade head` +
`downgrade 0002` clean.

**P1.2 - EmailProvider adapter + Mailpit (M).** `email/provider.py` Protocol, `email/message.py`
`build_outbound` (List-Unsubscribe/-Post, Reply-To, postal footer), `email/mailpit.py` (SMTP :11025,
injectable transport), `email/esp.py` stub, `get_provider(settings)`. *Accept:* `adapter_interface_parity`
test; Mailpit roundtrip (gated); pure `build_outbound` header test.

**P1.3 - The full Gate extension (M).** `compliance.py` + `breakers.py` (read-only window reader); extend
`gate.evaluate` with the 4 new **read-only** checks + 2 keyword-only params; expose `gate.is_suppressed`
wrapper for the enricher. *Accept:* all Phase-0 gate tests still green; `when=None/mailbox=None` no-op test;
`/gate/preview` does-not-write test (breaker over-threshold preview leaves `is_paused` unchanged); CAN-SPAM
**HOLD** (not BLOCK), quiet-hours-from-npi, warmup-cap (`sent_at IS NOT NULL`), breaker-read tests;
HOLD-is-a-no-op invariant. (Depends P1.1 for `mailbox`/`circuit_breaker_state`.)

**P1.4 - The deterministic SENDER (M).** `sender.py`: claim (`(next_action_at IS NULL OR <=now())`, FOR
UPDATE SKIP LOCKED) → gate → render → ensure Thread (random `reply_token`) →
`ledger_writer.transition` (idempotency incl. `mailbox_id`, before send) → `provider.send` → back-fill.
*Accept:* Mailpit happy path (`mailbox_id` + `reply_token` asserted); idempotency no-double-send with
rollback-first; provider-failure atomicity; HOLD re-queue invariant. **(Depends P1.1 - the
`message.mailbox_id` column is in the idempotency dict, so the SENDER cannot construct `Message(**idem)`
until 0003's model mirror exists - P1.2, P1.3.)**

**P1.5 - Enrichment loop (L).** `enrich/` package (adapter, fixtures, verdict, budget, cache, loop) +
`api/enrichment.py` `/enrichment/funnel`. *Accept:* waterfall-order, VALID-ONLY filter, budget/priority,
pay-once cache, suppression pre-check, funnel-math tests. (Independent of P1.2-P1.4; can land in parallel.)

**P1.6 - Linter + copy schema (M).** `certuma_core/linter.py` + `certuma_core/copy_schema.py` (pure).
*Accept:* the full banned-claims/hallucination/token/altered-url table tests; PASS on the seeded template.
(Independent; can land early - it's the contract the Copywriter targets.)

**P1.7 - Template approval flow (M).** `templates/approval.py` + `api/templates.py`. *Accept:*
`POST /templates/{id}/approve` flips `is_approved` + sets `approved_by` + writes one
`audit_log(entity='template')`; `/templates/{id}/preview` returns a `LintResult` + preview without sending;
Copywriter (next step) refuses an unapproved template. (Depends P1.6.)

**P1.8 - COPYWRITER node (M).** `copywriter/` (provider with StubCopyProvider + AnthropicCopyProvider,
node, render). *Accept:* StubCopyProvider happy/retry/hard-fail tests; refuse-without-approved-template;
model-tier cost-guard; `ledger_writer` never invoked. (Depends P1.6, P1.7.)

**P1.9 - Event ingestion + Monitor (M).** `email/ingest.py`, `email/dedup.py`, `email/suppress.py`,
`email/breaker_window.py` (+ ingest-side breaker-trip pause write), `api/webhooks.py` receive endpoint.
*Accept:* event dedup, inbound-Message dedup (inbound `esp_message_id` + `uq_msg_inbound_esp`),
delivered→awaiting_reply, hard-bounce→suppress→exhausted (+ later `BLOCK suppression`),
**opt_out/unsubscribe_click→suppress(opt_out)→do_not_contact** (deterministic, never LLM-gated),
complaint→breaker→ingest-side `is_paused` trip, out-of-order/concurrency, unmatched-triage. (Depends P1.1,
P1.2.)

**P1.10 - claim_url poller (S).** `poller/claim_poller.py` + `poller/reenrich.py` + `POST /poll/run`.
*Accept:* `poller_activates_from_real_send` (seed `email_sent`/`awaiting_reply`, two-step
`→interested→physician_activated`, both `actor='poller'` - proves reachability, NOT seeded in `interested`),
reentrant-terminal, actor-guard, source-unavailable. (Depends P1.1 - needs the two new `…→interested`
edges.)

**P1.11 - Orchestrator + wired approval send (L).** `orchestrator/` (loop, policy, scheduler, approvals,
runner); rewire `api/app.py` `decide()` to call `execute_approved_send` **with rollback-first poisoned-session
+ idempotent double-approve handling**; add `GET /approvals/{id}`; SLA sweep. *Accept:* policy unit tests;
scheduler cap/quiet-hours/valid-only + SKIP LOCKED disjoint-claim; Gate-on-approve re-check;
`approve_twice_is_safe` (no `PendingRollbackError`, one send); SLA-expiry no-send; the **smallest end-to-end
Assisted slice** (§9 headline, including the send→activation reachability proof). (Depends P1.4, P1.5,
P1.8.)

**P1.12 - Demo seed + DoD walkthrough (S).** Seed/approve one campaign (`dermatology`, `is_active=true`) +
approve the template via the API (or a one-off seed flip for the demo), one fixture-enriched valid contact,
run the slice against Mailpit, poll a fixture activation. *Accept:* the full §0 DoD checklist passes
locally end-to-end.

**Critical path:** P1.0 → P1.1 → {P1.2, P1.3} → P1.4 → P1.11. **P1.1 is upstream of P1.3 and P1.4** (both
need 0003's `mailbox`/`circuit_breaker_state`/`message.mailbox_id`; P1.4 also needs the two
`…→interested` graph edges shipped in P1.1, and P1.10 needs them too). P1.5/P1.6 are parallel early;
P1.6 → P1.7 → P1.8 → P1.11; P1.9/P1.10 hang off P1.1. P1.12 is last.

---

## 11. Risks & mitigations

| # | Risk | Mitigation |
|---|------|-----------|
| R1 | **Enrichment coverage is realistically low** (well under half of NPIs yield any email; only a fraction verify `valid`) → thin `sendable` funnel that looks broken. | `/enrichment/funnel` + `METRICS` make low coverage a *measured* funnel (attempts→found%→verdict histogram→valid-rate→needs_review). Parked `contact` rows are written (not discarded); VALID-ONLY is a single config predicate that can widen to `valid+catch_all` later; `needs_review` is non-terminal so re-promotion needs no re-enrich. |
| R2 | **Copywriter hallucination** (invents a plausible practice/affiliation/credential not in the seed) - the headline fatal flaw given uncredentialed self-reported data. | Defense in depth: model never receives free-form facts (only allow-listed `SeedFacts`); `merge_token_audit` forces self-attribution the linter cross-checks; linter's proper-noun extractor must trace every identifier in subject+body+plaintext to the **full `allowlist_literals` corpus** (`SeedFacts` ∪ pitch_angle ∪ approved-template prose ∪ sender identity) else REJECT; private-draft framing; periodic Opus-as-judge sampling out-of-band. |
| R3 | **Linter false-rejects** (flags a legit multi-word city / apostrophe name / the pitch angle) → burns retries, stalls the queue. | The linter receives the full `allowlist_literals` corpus (not just `SeedFacts`), so pitch-angle and approved-template prose PASS; exact literal strings (match known-good, not heuristics); allow-list (trace-to-source) not deny-list; multi-word entries whitelisted whole; one retry then route to human `needs_review` (no infinite loop); studio preview-against-sample surfaces over-strict cases before launch. |
| R4 | **CAN-SPAM bypass** - model emits/alters `claim_url`/unsubscribe/address, or a token renders empty. | Model is never asked for the 3 compliance tokens (stripped if present); `render.py` injects them deterministically; linter asserts `claim_url` byte-equality; `compliance.assert_can_spam_complete` validates the **rendered** output (the Gate **HOLDs** `can_spam_incomplete` on empty postal/unsub - re-queueable config gap, not a silent send), enforced structurally in the Gate/SENDER, never trusted to the LLM. |
| R5 | **Double-send / crash mid-send** (Message inserted, ESP fails or process dies). | Idempotency Message inserted+flushed **before** the ESP call inside one uncommitted txn; commit only after `provider.send` ok + `esp_message_id` set; a crash rolls back Message+transition together (key freed, safe retry). The residual ESP-accepted/DB-rolled-back gap is **out of scope for Phase 1** (Mailpit loses nothing meaningful; nothing persisted to reconcile) - **not** claimed as a Phase-1 mitigation; a real outbox+sweeper is deferred to the cold-ESP cutover (§12). |
| R6 | **Gate signature change breaks Phase 0 callers, or the Gate starts writing.** | New params keyword-only **with defaults** (`when=None`, `mailbox=None`); new checks are no-ops when absent and **read-only** (the breaker pause write lives in the ingest trip action, not `evaluate`); suppression-BLOCK-first ordering and the `GateDecision`/`_decided` struct unchanged; regression tests pin identical `/gate/preview` behavior AND that a preview never mutates `is_paused`. |
| R7 | **At-least-once / out-of-order webhooks** double-transition or move backwards. | Event-first dedup on `uq_event_dedup` makes processing idempotent; transitions go only through `ledger_writer` whose `ALLOWED_TRANSITIONS` graph rejects backwards/illegal moves (stale-event `IllegalTransition` logged as no-op, not a crash); optimistic-concurrency retry handles interleaving. |
| R8 | **Re-poll re-fires the terminal activation** → corrupts the sole conversion metric. | Deterministic `activation_dedup_key(npi,campaign)` collides on `uq_event_dedup` and short-circuits before `transition`; belt-and-suspenders: `physician_activated` is terminal + the `ACTIVATION_ONLY_ACTORS` guard. |
| R9 | **Circuit-breaker flap** from out-of-order complaint webhooks. | Persisted `circuit_breaker_state` with hysteresis (trip at threshold, clear only below a lower threshold after a cooldown) + min-sample-size guard; the ingest trip action sets sticky `campaign.is_paused` so clearing requires the breaker to actively un-pause; the Gate only *reads* the state. |
| R10 | **Quiet-hours TZ mapping wrong/blank** → sends in quiet hours. | Static STATE_TZ map for 50 states + DC; `practice_state` resolved inside `evaluate` from `npi → prospect.practice_state`; multi-TZ states pick the widest quiet window; blank/unknown `practice_state` → HOLD (fail-safe); unit-tested per state. |
| R11 | **Provider overspend on enrichment** (re-paying per NPI, burning budget on low-priority). | DB-backed pay-once cache + `budget.py` priority-ordered allocation with hard daily cap + per-priority allowances; `enrich_provider_call` makes spend observable and the cap is asserted in tests. |
| R12 | **Role addresses** (`info@`/`office@`) verify `valid` at the provider but aren't the physician. | Deterministic role-address demotion in `verdict.classify` caps them at `risky` so VALID-ONLY excludes them; flagged (`is_role_address`/verifier string) for review. |
| R13 | **Dashboard decide() runs the ESP call in-request** → slow/failing provider, or a duplicate approve, poisons the request session. | Crash-safe ordering (Message before send) means a failed send leaves a blocking Message row, not a double-send; on the duplicate `IntegrityError`, `execute_approved_send` **rolls back the poisoned session FIRST** then reconciles idempotently (no `PendingRollbackError`, no second send - `approve_twice_is_safe` test); for the real ESP move `execute_approved_send` to a background worker (sync is fine for Mailpit). |
| R14 | **Re-Gate at approve flips ALLOW→HOLD/BLOCK** (kill switch/quiet hours/opt-out appeared) → an approved item that won't send. | On HOLD: keep `Approval` pending, record `gate_reason_code`, surface "approved-but-held: <reason>", re-queue. On BLOCK (suppression appeared, e.g. opt-out): do not send, mark terminally, stop the lead per the suppression flow. Never silently drop, never auto-send later without the gate clearing. |
| R15 | **Mailpit has no bounce/complaint/inbound/opt-out semantics** → can't exercise the dangerous paths locally. | The EmailProvider interface is the test seam: a `FakeProvider` emits synthetic bounce/complaint/reply/opt_out `NormalizedEvent`s so ingestion (incl. the deterministic opt-out → `do_not_contact` path) is fully unit-tested with no ESP; Mailpit covers SMTP send + delivered roundtrip; the cold-ESP mapping table is validated by the parity test before any production send. |
| R16 | **Activation path unreachable** - the only edge into `physician_activated` is from `interested`, and the only edge into `interested` was from `replied` (a Phase 2 LLM node), so the poller could never fire from a real Phase-1 send. | Add two deterministic edges to `ALLOWED_TRANSITIONS` (`email_sent → interested`, `awaiting_reply → interested`, §2/§8) - a pure `certuma_core` change, additive, no edge removed, status CHECK already lists `interested`. The poller promotes a clicked lead `email_sent|awaiting_reply → interested → physician_activated` in one unit of work, both `actor='poller'`. The headline E2E proves reachability from the send (never seeds `interested`). |
| R17 | **Opt-out / one-click unsubscribe has no inbound handler** → the compliance link is sent but its POST does nothing → a recipient cannot actually opt out. | The deterministic opt-out handler (§6a) maps `opt_out`/`unsubscribe_click` events → `Suppression(reason='opt_out')` + `→ do_not_contact` via `actor='monitor'`, structurally outside any LLM path; the RFC 8058 `List-Unsubscribe-Post` POST and reply-based opt-outs both route here; a later `gate.evaluate` returns `BLOCK suppression`. Covered by `opt_out_suppresses_and_stops`. |

---

## 12. Open decisions for the stakeholder

**Deferred infra (blocking real, non-Mailpit sends - keep explicit):**
- **Cold sending domain + ESP account.** No production send until the domain is stood up. The
  `EmailProvider`/`ColdEspProvider` adapter, `Reply-To` plus-address domain, and the `List-Unsubscribe`
  mailto+https one-click endpoint all need `CERTUMA_COLD_DOMAIN`. Until then Mailpit + a placeholder
  `<cold_domain>` from Settings.
- **Sender identity** - the real accountable employee `name + title + email` for the `From`/footer
  (decision #5). Recommend an org/sender-identity config (Settings or a tiny table), surfaced read-only to
  the Copywriter and **included in the linter's `allowlist_literals` corpus** so the sender's own name never
  false-rejects as a hallucination (§4b/§4c). Lives campaign-level vs org-level?
- **Mailbox roster + selection policy** - round-robin vs least-recently-used vs weighted-by-remaining-cap,
  and whether the Sender chooses the mailbox or it's pre-assigned per lead. Blocked on the cold-domain +
  named-employee inputs; the `mailbox` table ships empty (one dev mailbox for Mailpit).
- **Concrete enrichment/verifier vendors** for each waterfall tier (healthcare-specialized, general B2B,
  verifier). Adapter is vendor-agnostic; this only sets credentials + the per-vendor `classify()` sub-code
  map.
- **Platform read API contract** - the JSON shape from the claim-status endpoint that means
  "claimed/clicked" (decision #7). The poller's `fetch→status` mapping is a stub until the endpoint owner +
  timeline land.
- **Where `execute_approved_send` runs** - synchronous in the HTTP request (simplest for the Mailpit
  Assisted demo; the poisoned-session rollback-first handling, §7, makes the sync path safe) vs a Redis/RQ
  background worker (needed once real ESP latency/retries matter; Redis is available locally). Recommend
  sync for the demo, worker before production.
- **Send reconciliation at the cold-ESP cutover** - a real outbox/pending-send row written *before* the
  provider call + a sweeper that reconciles "ESP-accepted but DB-rolled-back" by provider message-id. **Out
  of scope for Phase 1** (Mailpit cannot lose the txn meaningfully); add it with the cold ESP. Listed here
  so it is not forgotten - it is a real gap only for a real ESP.
- **Anthropic API specifics to pin at P1.8 (verify, do not assume):** the model-id strings
  (`claude-sonnet-4-6` / `claude-opus-4-8` / `claude-haiku-4-5`), the minimum cacheable-prefix token counts
  for prompt caching, the Batches discount, and the single structured-output mechanism
  (`output_config.format` json_schema vs `client.messages.parse()`). These are load-bearing for the
  cost-control DoD; verify against the live API / `claude-api` reference at implementation time and treat
  the values quoted in §4b as assumptions until then.

**Policy / numbers (needed before Supervised, but Assisted launch can proceed with placeholders):**
- **Verdict staleness TTL** (days before re-pay) - differs by verdict (`valid` decays slower than
  `catch_all`/`unknown`). Placeholder defaults in `cache.py`; needs provider guidance.
- **Per-priority enrichment budget numbers** (high/medium/low call allowances + daily cap) - placeholder
  defaults in `budget.py` pending real provider pricing.
- **Circuit-breaker window math** - rolling-window length, min sample N, clear-threshold + cooldown
  (arch gives trip points complaint>0.1% / bounce>2% but not the window). Default proposal: 24h trailing
  window, min N=20, clear at half-threshold after 6h. Window math must be agreed across the ingest counter
  and the Gate threshold owners. Per recipient-domain vs per-campaign vs per-mailbox?
- **Quiet-hours window + weekend policy** - propose Mon-Fri 08:00-17:00 local, code constant with a
  per-campaign override seam.
- **Warmup ramp schedule** (start ~50/day) and per-rep daily cap values.
- **First-touch `model_confidence` auto-pass threshold** (the locked 0.8 floor is for *replies*;
  first-touch may warrant a separate, possibly higher, floor). Moot under Assisted (every send approved);
  decide before Supervised.
- **SLA durations** (first HOLD threshold vs second escalation threshold) and the backup approver identity
  (`app_user.role='backup'`) for decision #4.

**Schema / design choices (cheap to decide now):**
- **Migration 0003 optional columns** - add `contact.is_role_address`/`discovery_source` and
  `template.created_by`/`variant_label` now, or encode role/source in the `verifier` string + defer
  (trades query-ability vs. a migration). Recommend: ship `template.variant_label` + `lead.needs_reenrich`
  + `mailbox` now (required); make `contact.is_role_address/discovery_source` optional.
- **Postal address source** - a single org-level config string (the cold domain's accountable employee's
  physical address) vs `prospect.practice_address`. Recommend config-injected sender postal address, **not**
  the physician's.
- **`merge_token_audit` hard vs advisory** - recommend hard at launch (stronger hallucination guard),
  relax only if false-reject rate is high.
- **Hard-bounce stop target** - `exhausted` (chosen; legal stop, terminal non-success) vs `do_not_contact`
  (may overstate intent for a bad address). Confirm which the funnel analytics expect.
- **`plaintext` source** - model emits it vs renderer derives it; recommend model emits but linter lints
  **both** parts identically.
- **Inbound reply boundary (resolved in this plan; confirm).** The `→replied` *ledger edge* lives in the
  model-free ingestion component (Phase 1, §6a) - a real reply advances the ledger to `replied` with the
  inbound Message carrying `esp_message_id` for at-most-once. The `replied → interested` *classification* is
  the separate Phase 2 LLM node and is NOT done here. Note this boundary is independent of the
  claim-click activation path, which the poller drives `email_sent|awaiting_reply → interested →
  physician_activated` deterministically (§2/§6b) regardless of reply state.
- **Receive endpoint** - normalize synchronously behind a savepoint (fine at ~50/day warmup) vs thin
  raw-inbox + background worker (crash-safe at scale). Recommend a raw-inbox seam, ship synchronous for
  Phase 1.
