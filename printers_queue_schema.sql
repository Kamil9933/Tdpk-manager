-- Printer live status + print queue. These were applied to the live Supabase
-- project as migrations (add_bambu_live_status_to_printers,
-- create_print_queue, Oct 2026) and are copied here so the database can be
-- rebuilt from the repo like every other *_schema.sql file. Run after
-- accounting_schema.sql (needs the printers table).

-- Links a printer row to its Bambu Cloud device and holds the latest
-- live-telemetry snapshot pulled over MQTT (percent, ETA, file, temps).
-- Not a history table on purpose -- this is "what's it doing right now"
-- for a dashboard tile, overwritten on every poll.
alter table printers
  add column if not exists bambu_dev_id text unique,
  add column if not exists live_status jsonb,
  add column if not exists live_status_updated_at timestamptz;

insert into printers (name, model, status, bambu_dev_id)
values
  ('bambu1', 'Bambu Lab A1', 'active', '03919D470900302'),
  ('bambu2', 'Bambu Lab A1', 'active', '03900D5A1906211')
on conflict (bambu_dev_id) do nothing;

-- The shared print queue -- replaces q.thirddimension.pk's queue_data.json
-- as the single source of truth. legacy_id preserves the original q.tdpk
-- job id so existing links/bookmarks (e.g. track.html?q=...) keep working.
create table if not exists print_queue (
  id uuid primary key default gen_random_uuid(),
  legacy_id text unique,
  name text not null,
  type text default 'custom',
  priority text default 'normal',
  customer text,
  order_id text,
  phone text,
  cod_amount numeric default 0,
  print_time_hours numeric default 0,
  qty integer default 1,
  material text,
  colour text,
  printer text,
  postex_tracking text,
  notes text,
  status text not null default 'queued',
  image text,
  shopify_order_id text,
  shopify_item_id text,
  shopify_customer_id text,
  fails jsonb default '[]'::jsonb,
  created_at timestamptz default now(),
  printing_started_at timestamptz,
  completed_at timestamptz,
  shipped_at timestamptz,
  auto_archived_at timestamptz,
  updated_at timestamptz default now()
);

create index if not exists print_queue_status_idx on print_queue(status);
create index if not exists print_queue_printer_idx on print_queue(printer);
create index if not exists print_queue_shopify_order_idx on print_queue(shopify_order_id);
create index if not exists print_queue_phone_idx on print_queue(phone);

alter table print_queue enable row level security;
-- The backend's service-role key bypasses RLS; this only locks out anon/
-- authenticated keys, matching every other table here.
create policy "service role only" on print_queue for all using (false);
