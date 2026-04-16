-- Run this in your Supabase SQL Editor (https://supabase.com/dashboard → SQL Editor)

create table if not exists check_results (
  id          bigint generated always as identity primary key,
  checked_at  timestamptz not null default now(),
  service     text not null,            -- 'reachability', 'sampling', 'openai_compatible', 'training_infra'
  status      text not null,            -- 'up' or 'down'
  latency_ms  real,
  error       text,
  meta        jsonb                     -- response_snippet, supported_models, etc.
);

-- Index for the status page: latest results per service, and time-range queries
create index idx_check_results_service_time on check_results (service, checked_at desc);

-- Enable RLS (table is in public schema, exposed via Data API)
alter table check_results enable row level security;

-- Read-only policy for the anon key (status page reads)
create policy "Public read access"
  on check_results for select
  using (true);

-- No insert/update/delete via anon key — writes go through service_role key in check.py
