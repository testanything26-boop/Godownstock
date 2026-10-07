-- Godown Stock database schema.
-- In Supabase: open your project -> SQL Editor -> paste this -> Run.

create table if not exists rolls (
  id text primary key,
  fabric_type text default '',
  color text default '',
  dia text default '',
  gsm text default '',
  weight numeric default 0,
  current_weight numeric default 0,
  manufacturer text default '',
  created_date text default '',
  status text default 'in-stock',
  styles text default '',
  last_used_date text default '',
  notes text default ''
);

create table if not exists history (
  hid serial primary key,
  roll_id text not null,
  date text default '',
  type text default '',
  style text default '',
  weight_used numeric default 0,
  remaining numeric default 0,
  notes text default ''
);
create index if not exists idx_history_roll on history(roll_id);
create index if not exists idx_history_date on history(date);

create table if not exists users (
  username text primary key,
  pass_hash text not null,
  role text default 'staff',
  created_at text default ''
);

create table if not exists meta (
  key text primary key,
  value text default ''
);

-- v2.1: multiple godown locations + company name (safe to re-run;
-- the app also auto-applies this on login via db._ensure_location_schema).
create table if not exists locations (
  id serial primary key,
  name text unique not null
);
alter table rolls add column if not exists location_id integer references locations(id);
-- company name lives in meta under key 'companyName' (app defaults to 'Chakra Production').

-- v2.2: wastage bags (safe to re-run; the app also auto-applies this on login).
create table if not exists wastage_bags (
  id text primary key,
  weight numeric default 0,
  created_date text default '',
  status text default 'in-stock',
  buyer_name text default '',
  buyer_phone text default '',
  sold_date text default '',
  notes text default ''
);
