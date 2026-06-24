# Certuma Reach - production send-infra cutover (Phase 3 task P3.10)

The code seams for real cold-email sending are built and tested against deterministic stubs. Cutting
over to production is a CONFIGURATION + procurement step (stakeholder-gated), not a code change. This
is the checklist.

## What is already wired (code-side, done)

- **Outbound provider seam.** `CERTUMA_EMAIL_PROVIDER=esp` selects `certuma.email.esp.EspProvider`, a
  real HTTP send behind the same `EmailProvider` interface as Mailpit. Set `CERTUMA_ESP_API_KEY` +
  `CERTUMA_ESP_BASE_URL`. The request JSON is generic; map it to the chosen ESP's exact fields in
  `esp.py` (one function). An unconfigured ESP fails loudly - it never silently drops a send.
- **Warmup ramp.** A new mailbox ramps from `WARMUP_START_CAP` (10/day) to its `daily_cap` over
  `WARMUP_DAYS` (14), enforced by the Gate's warmup check (`certuma_core.warmup`). Seed mailboxes with
  the real cold addresses and their target caps; the ramp is automatic from each mailbox's created_at.
- **Inbound seam.** `POST /inbound/esp` accepts a provider inbound-reply webhook;
  `inbound.parse_esp_inbound` normalizes it (extracts the `reply+<token>@` thread token) and runs the
  classifier loop. For IMAP instead of a webhook, poll the mailbox and post each message to the same
  endpoint.
- **Machine webhook auth.** Set `CERTUMA_WEBHOOK_SECRET`; the provider posts to `/events/email`,
  `/inbound/reply`, `/inbound/esp` with header `X-Certuma-Webhook-Secret`. Without the secret these
  endpoints require an operator session (closed by default).
- **Session secret.** Set `CERTUMA_SESSION_SECRET` (else logins reset on restart / break across
  workers - the app warns loudly).

## Stakeholder / procurement steps (gated)

1. **Cold sending domain + DNS.** Register the cold domain (e.g. getcertuma.com, separate from the
   corporate domain), set SPF, DKIM, DMARC, and a custom return-path. Decision #1 firewall: keep this
   infrastructure separate from corporate mail.
2. **ESP account.** Open a cold-tolerant ESP account; get the API key + base URL; confirm inbound
   (parse/webhook) support. Set `CERTUMA_ESP_API_KEY`, `CERTUMA_ESP_BASE_URL`,
   `CERTUMA_EMAIL_PROVIDER=esp`.
3. **Sender identity + mailbox roster.** The real accountable employee (decision #5) and the
   warmup mailboxes; seed the `mailbox` table (addresses + target `daily_cap`). The ramp handles the
   rest.
4. **Inbound transport.** Choose ESP inbound webhook vs IMAP poll; point it at `/inbound/esp` with
   `CERTUMA_WEBHOOK_SECRET`.
5. **Open/event webhooks.** Point the ESP's delivered/bounce/complaint/open webhooks at
   `/events/email` (normalize per provider), and the open pixel domain at `/track/open`.
6. **Claim-status source.** Wire the real platform claim-status read API into the poller's `fetch`
   (replaces the simulated claim in the demo).
7. **Real enrichment / signal vendors.** Slot the discovery + verification + knowledge-graph vendors
   behind the existing `EnrichProvider` / `VerifyProvider` / `SignalProvider` interfaces (swap the
   stubs).
8. **Secrets + session.** `CERTUMA_SESSION_SECRET` (stable), and create the operator/leadership
   logins (`make create-user`).

## Provisional numbers to confirm with stakeholder

Warmup (10/day start, 14-day ramp), cadence (3 touches at day 0/3/7), breaker thresholds, SLA window,
quiet-hours window, the per-send cost used for unit economics. All live as named constants and are
safe defaults until tuned.
