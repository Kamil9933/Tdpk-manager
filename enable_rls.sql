-- Enable Row Level Security on every table, with no policies attached.
-- The app only ever connects with the service_role key, which bypasses RLS
-- regardless of this setting -- so this changes nothing about how the app
-- behaves. What it does is close off the anon/public key: with RLS on and
-- no policies defined, that key is denied on every table by default. This
-- silences Supabase's "RLS disabled" warning and adds a safety net in case
-- the anon key is ever exposed or used by mistake.

alter table sessions           enable row level security;
alter table operation_log      enable row level security;
alter table product_snapshots  enable row level security;
alter table automation_rules   enable row level security;
alter table notifications      enable row level security;
alter table saved_views        enable row level security;
alter table cost_settings      enable row level security;
