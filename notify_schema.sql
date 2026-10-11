-- Reminders the Manager agent can set for itself mid-conversation (e.g. "I'll
-- check this ad's performance tomorrow and tell you") and the digest
-- send-log that keeps the twice-daily digest from firing twice for the same
-- slot when the external cron pings more often than the digest interval.
-- Run this once in the Supabase SQL editor.

create table if not exists agent_reminders (
  id uuid primary key default gen_random_uuid(),
  due_at timestamptz not null,
  message text not null,
  context jsonb default '{}'::jsonb,
  status text not null default 'pending' check (status in ('pending','sent','cancelled')),
  created_at timestamptz default now(),
  sent_at timestamptz
);
create index if not exists idx_agent_reminders_due on agent_reminders(status, due_at);
alter table agent_reminders enable row level security;

create table if not exists digest_log (
  id uuid primary key default gen_random_uuid(),
  slot text not null unique,  -- e.g. '2026-10-11:09:00'
  sent_at timestamptz default now()
);
alter table digest_log enable row level security;
