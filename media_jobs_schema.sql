-- Media editing (Phase 4 add-on). Safe to re-run.
--
-- media_jobs: one row per edit (image, video or carousel). Edits never touch
-- the source file: the result is a NEW object in the social-media bucket,
-- linked back here through source_paths -> result_paths.
create table if not exists media_jobs (
  id uuid primary key default gen_random_uuid(),
  kind text not null check (kind in ('image', 'video', 'carousel')),
  preset text not null,
  params jsonb not null default '{}'::jsonb,
  source_paths text[] not null,
  status text not null default 'queued' check (status in ('queued', 'running', 'done', 'failed')),
  progress integer not null default 0,
  result_paths text[] not null default '{}',
  warnings jsonb not null default '[]'::jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists media_jobs_created_idx on media_jobs (created_at desc);
alter table media_jobs enable row level security;  -- service role only, like every other table

-- media-assets: PRIVATE bucket for the brand logo (PNG, transparency kept),
-- headline font (TTF/OTF) and background music. Only the backend reads it.
insert into storage.buckets (id, name, public, file_size_limit)
values ('media-assets', 'media-assets', false, 20971520)
on conflict (id) do update set public = false, file_size_limit = excluded.file_size_limit;
