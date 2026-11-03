-- SEO tracking for thirddimension.pk, written by seo/seo_report.py (run from
-- GitHub Actions twice a day, not from Render). Search Console data is
-- per-day with a ~3 day lag, so page/query rows are keyed by the date range
-- they cover and simply overwritten by the second run of the day.
-- PageSpeed and index checks are point-in-time, so every run adds rows.
-- Run after supabase_schema.sql (needs pgcrypto).

create table if not exists seo_runs (
  id uuid primary key default gen_random_uuid(),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'running' check (status in ('running','ok','partial','failed')),
  site_url text,
  errors jsonb not null default '[]'::jsonb
);

-- Search Console totals per target page, for one date window.
create table if not exists seo_page_metrics (
  id uuid primary key default gen_random_uuid(),
  page_key text not null,
  url text not null,
  window_start date not null,
  window_end date not null,
  clicks integer not null default 0,
  impressions integer not null default 0,
  ctr numeric not null default 0,
  position numeric,
  updated_at timestamptz not null default now(),
  unique (page_key, window_start, window_end)
);

-- Search Console per (query, page) for one date window: tracked queries on
-- any page, plus every query that brings impressions to a target page.
create table if not exists seo_query_metrics (
  id uuid primary key default gen_random_uuid(),
  query text not null,
  page_url text not null,
  window_start date not null,
  window_end date not null,
  tracked boolean not null default false,
  clicks integer not null default 0,
  impressions integer not null default 0,
  ctr numeric not null default 0,
  position numeric,
  updated_at timestamptz not null default now(),
  unique (query, page_url, window_start, window_end)
);

create table if not exists seo_pagespeed (
  id uuid primary key default gen_random_uuid(),
  run_id uuid references seo_runs(id) on delete cascade,
  captured_at timestamptz not null default now(),
  page_key text not null,
  url text not null,
  strategy text not null default 'mobile',
  performance integer,      -- Lighthouse scores, 0-100
  seo integer,
  accessibility integer,
  best_practices integer,
  lcp_ms numeric,           -- lab metrics
  cls numeric,
  tbt_ms numeric,
  field_lcp_ms numeric,     -- real-user (CrUX) metrics, when Google has enough traffic
  field_inp_ms numeric,
  field_cls numeric,
  error text
);

create table if not exists seo_index_status (
  id uuid primary key default gen_random_uuid(),
  run_id uuid references seo_runs(id) on delete cascade,
  captured_at timestamptz not null default now(),
  page_key text not null,
  url text not null,
  verdict text,             -- PASS / NEUTRAL / FAIL from the URL Inspection API
  coverage_state text,
  last_crawl_time timestamptz,
  google_canonical text,
  error text
);

-- Suggested changes. The script only ever writes 'draft' rows; nothing here
-- is applied to Shopify automatically. Approve or reject them yourself.
create table if not exists seo_suggestions (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  page_key text not null,
  kind text not null,       -- not_live / not_indexed / striking_distance / low_ctr / slow
  detail jsonb not null default '{}'::jsonb,
  suggestion text not null,
  status text not null default 'draft' check (status in ('draft','approved','rejected','done'))
);

-- Approval queue (core/seo_queue.py): Gemini's proposed copy, what was on
-- the page before it was applied (for a manual revert), and when.
alter table seo_suggestions
  add column if not exists proposed_title text,
  add column if not exists proposed_description text,
  add column if not exists previous_title text,
  add column if not exists previous_description text,
  add column if not exists decided_at timestamptz,
  add column if not exists applied_at timestamptz;

-- Daily Google positions for each tracked query (seo/rank_check.py, via
-- DataForSEO, morning run only). position is the organic position of the
-- first thirddimension.pk result, null when it isn't in the top 100.
create table if not exists seo_rankings (
  id uuid primary key default gen_random_uuid(),
  captured_at timestamptz not null default now(),
  query text not null,
  device text not null check (device in ('desktop','mobile')),
  position integer,
  ranking_url text,
  run_id uuid references seo_runs(id) on delete set null
);

create index if not exists seo_rankings_query_idx on seo_rankings(query, device, captured_at desc);
create index if not exists seo_page_metrics_page_idx on seo_page_metrics(page_key, window_end);
create index if not exists seo_query_metrics_query_idx on seo_query_metrics(query, window_end);
create index if not exists seo_pagespeed_page_idx on seo_pagespeed(page_key, captured_at);
create index if not exists seo_index_status_page_idx on seo_index_status(page_key, captured_at);
create index if not exists seo_suggestions_open_idx on seo_suggestions(status, page_key, kind);

alter table seo_runs          enable row level security;
alter table seo_page_metrics  enable row level security;
alter table seo_query_metrics enable row level security;
alter table seo_pagespeed     enable row level security;
alter table seo_index_status  enable row level security;
alter table seo_suggestions   enable row level security;
alter table seo_rankings      enable row level security;
