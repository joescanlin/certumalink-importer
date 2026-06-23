"""seed campaigns and a compliant placeholder template (Phase 0 task B10)

Seeds the four CAMPAIGN_PRESETS plus two sentinel campaigns that the FKs require:
  - 'legacy'  : every pre-campaign activation_status.csv lead is assigned here (B11).
  - ''        : un-campaigned workflow_score rows reference this (monolith scores with campaign='').
Also seeds ONE placeholder outreach template that, unlike the old Rox copy, carries an
unsubscribe token and a postal-address token. It is is_approved=false: nothing may send from it
until a human approves it (decision: the lead approves every template once).

Values are inlined (not imported from certuma_core) so this migration is self-contained and
does not change if the source constant later changes. Idempotent.

Revision ID: 0002_seed
Revises: 0001_initial
Create Date: 2026-06-23
"""
from alembic import op

revision = "0002_seed"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


SEED_CAMPAIGNS = """
INSERT INTO campaign (name, label, specialty_terms, priority_boost, pitch_angle, is_active) VALUES
  ('primary-care','Primary Care',
   ARRAY['family medicine','internal medicine','general practice','pediatrics',
         '207q00000x','207r00000x','208000000x','208d00000x'], 18, 'primary care practice', false),
  ('dermatology','Dermatology',
   ARRAY['dermatology','207n00000x'], 22, 'dermatology practice', false),
  ('cardiology','Cardiology',
   ARRAY['cardiology','cardiovascular disease','207rc0000x'], 22, 'cardiology practice', false),
  ('urgent-care','Urgent Care',
   ARRAY['urgent care','emergency medicine','family medicine','207p00000x','207q00000x'], 18, 'urgent care practice', false),
  ('legacy','Legacy (pre-campaign)', ARRAY[]::text[], 0, '', false),
  ('','(none)', ARRAY[]::text[], 0, '', false)
ON CONFLICT (name) DO NOTHING;
"""

# Placeholder body. The {unsubscribe_url} and {postal_address} tokens are mandatory and are
# what the old Rox copy lacked. is_approved=false; this is a starting point, not a send-ready asset.
SEED_TEMPLATE = """
INSERT INTO template (campaign, version, subject, body, merge_tokens, is_approved)
SELECT NULL, 1,
  'Your Certumalink profile draft is ready to review',
  $body$Hi Dr. {last_name},

We prepared a draft Certumalink profile for your {pitch_angle} in {city}. You can review and claim it here:
{claim_url}

If you would prefer not to hear from us, you can unsubscribe here: {unsubscribe_url}

{postal_address}$body$,
  ARRAY['last_name','pitch_angle','city','claim_url','unsubscribe_url','postal_address'],
  false
WHERE NOT EXISTS (
  SELECT 1 FROM template
  WHERE campaign IS NULL AND version = 1
    AND subject = 'Your Certumalink profile draft is ready to review' AND is_approved = false
);
"""

# Downgrade deletes ONLY our seed and ONLY campaigns no live row references, so the stepwise
# `downgrade 0001` cannot FK-violate after the seed importer has assigned leads to 'legacy'
# (or workflow_score/message rows to ''). NOT adding ON DELETE CASCADE to the campaign FKs:
# that would let a campaign deletion wipe live leads.
DROP_TEMPLATE = """
DELETE FROM template
WHERE campaign IS NULL AND version = 1
  AND subject = 'Your Certumalink profile draft is ready to review' AND is_approved = false;
"""

DROP_CAMPAIGNS = """
DELETE FROM campaign c
WHERE c.name IN ('primary-care','dermatology','cardiology','urgent-care','legacy','')
  AND NOT EXISTS (SELECT 1 FROM lead           WHERE campaign = c.name)
  AND NOT EXISTS (SELECT 1 FROM workflow_score WHERE campaign = c.name)
  AND NOT EXISTS (SELECT 1 FROM message        WHERE campaign = c.name);
"""


def upgrade() -> None:
    op.execute(SEED_CAMPAIGNS)
    op.execute(SEED_TEMPLATE)


def downgrade() -> None:
    op.execute(DROP_TEMPLATE)
    op.execute(DROP_CAMPAIGNS)
