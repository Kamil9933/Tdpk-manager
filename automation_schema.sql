-- Automation autonomy toggles -- governs which Meta ads/social actions are
-- allowed to run without a human approving each one first. Everything
-- defaults to OFF (always asks). New ad/campaign creation deliberately has NO
-- toggle here -- it always requires explicit approval, by design, not an
-- oversight.
create table if not exists automation_settings (
  key text primary key,
  enabled boolean not null default false,
  config jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table automation_settings enable row level security;

insert into automation_settings (key, enabled, config) values
  ('ads_pause_resume', false, '{}'::jsonb),
  ('ads_budget_change', false, '{"ceiling_minor": null}'::jsonb),
  ('social_publish', false, '{}'::jsonb),
  ('comment_reply', false, '{}'::jsonb)
on conflict (key) do nothing;
