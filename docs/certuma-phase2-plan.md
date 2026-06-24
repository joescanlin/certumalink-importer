# Certuma Reach - Phase 2 plan (two-way + autonomous)

Status: APPROVED (2026-06-23). Grounded in the merged Phase 1 code. The recommended default was
confirmed for every **[DECISION]** below:
1. Activation = claim-click only (a positive reply is `interested`, not `physician_activated`).
2. Autonomy headline = `supervised` (auto-send routine touches, escalate high-value / objections /
   low-confidence); `autonomous` stays behind a per-campaign flag.
3. External integrations = deterministic stub + clean seam now, real IMAP/ESP + enrichment vendors
   at the infra cutover.
4. Objection/question replies always human-reviewed, even in autonomous mode.
5. Cadence (provisional) = 3 touches at day 0 / +3 / +7 business days, campaign-configurable.
6. Reply-drafting autonomy = only routine first-touches/follow-ups auto-send.

## Where Phase 1 left us

Phase 1 is a one-way Assisted loop: enrich (hand-seeded) -> draft -> human approves -> send ->
delivered -> claim-click activates. Every send is human-approved; there is no handling of what comes
back, no follow-up, and no real autonomy. The plumbing for all of that is already in the schema (see
the seams above), deliberately left inert.

Phase 2 makes the loop **two-way** (we read and act on replies) and **autonomous** (the policy
engine actually sends without a human for routine touches, escalating only what matters). The
internal sales lead's job shifts from "approve every email" to "handle the exceptions."

## Goals

1. Read inbound replies, classify intent, and act deterministically on the result.
2. Draft responses to objections/questions (the second Claude use, plus Haiku for classification).
3. Multi-step cadences (follow-ups) instead of a single touch.
4. Real enrichment (a waterfall behind a provider interface) replacing the demo hand-seed.
5. The autonomy tiers (`supervised`/`autonomous`) actually acting, with guardrails.
6. A deterministic scheduler tick that runs the whole loop on its own, surfacing only escalations.

## The conversation state machine (the heart of Phase 2)

The status graph already supports it; Phase 2 wires the transitions the monitor records today as
no-ops. An inbound reply moves `email_sent|awaiting_reply -> replied`, then the **classifier** maps
intent to a deterministic transition:

| Reply intent | Deterministic effect | Actor |
|---|---|---|
| interested / positive | `replied -> interested`; queue an immediate claim-link nudge | classifier |
| objection / question | `replied -> needs_review`; draft a response (Opus) -> Approval or auto-send | classifier |
| not_interested | `replied -> do_not_contact` + suppress(opt_out) | classifier |
| unsubscribe | `-> do_not_contact` + suppress(opt_out) (same as the deterministic opt-out path) | classifier |
| out_of_office / auto_reply | no transition; reschedule `next_action_at` past the OOO window | classifier |
| wrong_person | `replied -> needs_review` (a human re-routes) | classifier |

**[DECISION 1 - activation definition.]** A positive reply is `interested`, NOT
`physician_activated`. Activation stays defined as the claim-url click (the single conversion
metric, protected by `ACTIVATION_ONLY_ACTORS`); a positive reply triggers an immediate claim-link
nudge and the poller still converts on the click. *Recommended:* keep activation = claim-click.
*Alternative:* let a strong "yes set me up" reply auto-activate (weakens the metric; not advised).

The classifier can NEVER set `physician_activated` (enforced by the ledger-writer actor guard, so
this is structurally safe even if the prompt misfires).

## Sub-steps

- **P2.0 migration 0004** (additive). `message.reply_classification` (text) + `message.in_reply_to`
  (thread linkage); `lead.cadence_step` already exists; `campaign` cadence config columns
  (`cadence_steps`, `cadence_interval_days`) or a small `cadence_step` table; `approval` gains an
  `escalation_reason`; a `contact_candidate` table for the enrichment waterfall (npi, email,
  source, score, verify_status). No destructive changes; downgrade reverses.

- **P2.1 inbound ingestion.** Parse the plus-addressed token from a delivered reply
  (`reply+<token>@domain`) -> `Thread` -> `Lead`; store `Message(direction='inbound')` deduped by
  the existing `uq_msg_inbound_esp` index; emit a `replied` `Event`. A normalized seam + a dev
  simulator now; the prod adapter (IMAP poll of the mailbox vs ESP inbound webhook) is **[DECISION
  3]** and lands with the infra cutover. *Recommended:* normalized seam + simulator now, defer the
  prod adapter (Mailpit-first, same as Phase 1).

- **P2.2 reply classifier** (`certuma/classifier/`, the Haiku node). Reply text + thread context ->
  `{intent, confidence, rationale}` via structured output; `StubClassifier` (deterministic, keyword
  rules) backs tests, `AnthropicClassifier` is the real node (model `claude-haiku-4-5`, verified via
  /claude-api; handle `stop_reason == "refusal"`). Intent -> transition per the table above, all
  through the single ledger-writer; suppression recorded before any transition (same discipline as
  the monitor).

- **P2.3 reply drafting + conversation.** Objection/question -> Opus draft (reuse the copywriter
  render+lint pipeline with a conversation prompt + the prior message as context) -> an `Approval`
  (assisted/supervised) or auto-send (autonomous, guardrailed). **[DECISION 6]** objections always
  human-reviewed even in autonomous mode. *Recommended:* yes, objections always escalate in Phase 2
  (high-stakes); only first-touches and routine follow-ups auto-send.

- **P2.4 cadence engine.** For an `awaiting_reply` lead past `next_action_at` with no reply,
  increment `cadence_step` (NEW idempotency key, so the at-most-once guarantee holds per step),
  re-draft the next template variant, and re-send (`awaiting_reply -> email_sent` is already a legal
  edge). Stop on reply/suppress/activate or at the max step. **[DECISION 5]** cadence shape.
  *Recommended:* 3 touches at day 0 / +3 / +7 business days, campaign-configurable, provisional
  defaults (like the breaker/SLA numbers).

- **P2.5 enrichment waterfall** (real P1.5). `EnrichProvider` interface (discovery) +
  `VerifyProvider` (email validation) with deterministic stubs now + real vendors later;
  `is_suppressed` check before spending; valid-only filter; role-address demotion;
  `contact_candidate` -> best `Contact`; budget by `activation_priority`; drives
  `enriching -> sendable` (or `needs_review`/`exhausted` if nothing valid). Replaces the demo
  hand-seed. **[DECISION 4]** real vendor vs stub now. *Recommended:* waterfall + deterministic stub
  now; slot vendors behind the interface later.

- **P2.6 autonomy policy + auto-executor.** `policy.decide(campaign, value_tier, confidence,
  gate_reason) -> {auto_send | escalate}`. `assisted` always escalates (Phase 1 behavior).
  `supervised` auto-sends routine first-touches/follow-ups, escalates high-value, objections, and
  low-confidence. `autonomous` auto-sends all non-objection touches within guardrails (daily caps,
  gate ALLOW, breaker clear), escalates only gate HOLDs/edges. The auto-executor runs
  `execute_approved_send` for non-escalated proposals. **[DECISION 2]** which tier is the Phase 2
  headline + the guardrails. *Recommended:* ship `supervised` as the working default, keep
  `autonomous` behind a campaign flag. Needs a real `model_confidence` (P2.2 classifier emits one;
  the copywriter gets a deterministic confidence proxy from lint-cleanliness + token coverage).

- **P2.7 scheduler tick.** One deterministic `tick(now)` that runs enrich -> propose -> auto-execute
  -> cadence -> poll -> expire-SLA in order, idempotently, so the system advances on its own. Driven
  by a cron/worker (a `make tick` for dev; the loop, not a daemon, so it stays testable). The
  dashboard shows autonomous activity.

- **P2.8 dashboard additions.** A Conversations view (the thread: outbound, inbound, classified
  intent, drafted reply) + an Escalations queue (what autonomy kicked to a human) + autonomy status
  per campaign. Same vendored certuma-link styling.

- **P2.9 cold-domain/ESP cutover** (infra, partly out of code). Real `EspProvider` send +
  inbound adapter + warmup ramp; flip `email_provider` from `mailpit` to `esp`. Gated on the
  sender-identity/domain decisions still open from Phase 1.

- **P2.10 autonomous demo.** End to end with the human only seeing escalations: enrich -> send ->
  reply -> classify -> (nudge | draft+escalate | suppress) -> follow-up -> claim -> activated. A
  deterministic e2e test (stubs + fixed clock) + a live Mailpit run.

## Critical path

P2.0 -> P2.1 -> P2.2 -> {P2.3, P2.4} -> P2.6 -> P2.7 -> P2.10. Enrichment (P2.5) and the dashboard
(P2.8) are parallel. Infra (P2.9) is independent and stakeholder-gated.

## Latent issues this plan surfaces (before they bite)

1. **Follow-up idempotency.** The at-most-once key is `(npi, campaign, cadence_step)`. A follow-up
   MUST increment `cadence_step` before re-sending or it collides with the prior touch's Message
   (IntegrityError) and silently no-ops. The cadence engine owns the increment; the SENDER is
   unchanged. (Safe-by-construction, but the increment is load-bearing.)
2. **`interested` has no autonomous next action today.** Once a lead is `interested` (positive reply
   but not yet claimed), nothing drives it forward. The cadence engine must treat `interested` as a
   first-class cadence state (nudge with the claim link), or interested leads stall. New edge use,
   no schema change.
3. **Classifier confidence feeds autonomy.** Auto-send risk gating needs a real confidence; the
   Phase 1 copywriter emits none. P2.2 must produce `model_confidence` (classifier) and a
   deterministic proxy for the copywriter, or `supervised`/`autonomous` degrade to "send everything."
4. **Inbound has no dev transport.** Mailpit receives outbound only; there is no inbound mail to
   parse in dev. P2.1 needs the simulator seam or P2.2+ are untestable end to end.

## Open Phase-1 carryovers folded in

cold domain / ESP / sender identity / mailbox roster (infra), enrichment vendors, the platform
claim-status read API, breaker window-math + SLA durations + cadence intervals (all provisional
defaults to be confirmed with stakeholder).
