-- In-app Claude agent (Phase 3) -- conversation + message history for the
-- chat-based marketing/ops assistant inside TDPK. Mirrors Anthropic's own
-- message format (role + content blocks) so a conversation can be replayed
-- straight back into the API with no translation.
create table if not exists agent_conversations (
  id uuid primary key default gen_random_uuid(),
  title text not null default 'New conversation',
  total_input_tokens integer not null default 0,
  total_output_tokens integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists agent_messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references agent_conversations(id) on delete cascade,
  role text not null check (role in ('user','assistant')),
  content jsonb not null,
  tokens_in integer not null default 0,
  tokens_out integer not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists agent_messages_conversation_idx on agent_messages(conversation_id, created_at);

alter table agent_conversations enable row level security;
alter table agent_messages enable row level security;
