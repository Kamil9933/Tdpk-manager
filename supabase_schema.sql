-- TD Shopify Manager — Supabase schema
-- Run this once in your new Supabase project's SQL Editor (Database > SQL Editor > New query),
-- then click "Run". This creates every table the app needs.

create extension if not exists pgcrypto;

-- Login sessions (created after PIN + TOTP verification)
create table if not exists sessions (
    id uuid primary key default gen_random_uuid(),
    token text not null unique,
    expires_at timestamptz not null,
    last_active timestamptz,
    created_at timestamptz not null default now()
);

-- Audit log of every bulk operation run against Shopify
create table if not exists operation_log (
    id uuid primary key default gen_random_uuid(),
    operation_name text not null,
    scope text,
    details text,
    product_count integer default 0,
    performed_by text default 'admin',
    performed_at timestamptz not null default now()
);

-- Snapshot of affected products before an operation runs, so it can be rolled back
create table if not exists product_snapshots (
    id uuid primary key default gen_random_uuid(),
    operation_log_id uuid references operation_log(id) on delete cascade,
    snapshot_data text,
    created_at timestamptz not null default now()
);

-- User-defined automation rules (e.g. "tag contains X -> action Y")
create table if not exists automation_rules (
    id uuid primary key default gen_random_uuid(),
    name text not null default 'Unnamed rule',
    condition text,
    condition_value text,
    action text,
    action_params text,
    enabled boolean not null default true,
    created_at timestamptz not null default now()
);

-- In-app notifications
create table if not exists notifications (
    id uuid primary key default gen_random_uuid(),
    type text,
    title text,
    message text,
    read boolean not null default false,
    created_at timestamptz not null default now()
);

-- Saved product filter views
create table if not exists saved_views (
    id uuid primary key default gen_random_uuid(),
    name text not null default 'My View',
    filters text,
    created_at timestamptz not null default now()
);

-- Key/value cost settings used for margin calculations
create table if not exists cost_settings (
    key text primary key,
    value text,
    updated_at timestamptz not null default now()
);

-- The app connects with the Supabase *service_role* key (server-side only, never exposed
-- to a browser), which bypasses Row Level Security entirely. RLS is left off here since
-- these tables are never queried with the anon/public key. If you ever add a table that
-- IS queried from a browser with the anon key, enable RLS and add policies for it.
