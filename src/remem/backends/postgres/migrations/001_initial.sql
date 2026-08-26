create type entry_kind     as enum ('memory','doc','rule');
create type entry_scope    as enum ('personal','team');
create type entry_origin   as enum ('human','agent','capture');
create type principal_kind as enum ('user','team');

-- array_to_string() is STABLE, not IMMUTABLE, so it cannot be used directly
-- inside a generated column expression. Wrap it in an IMMUTABLE function:
-- for text[] input this is safe, since text-to-text joining has no
-- locale/setting dependence the way e.g. numeric or timestamp formatting does.
create function remem_array_to_string_immutable(text[], text) returns text
  language sql immutable strict parallel safe as
  $$ select array_to_string($1, $2) $$;

create table principals (
  id uuid primary key,
  handle text not null unique,
  display_name text,
  kind principal_kind not null default 'user',
  created_at timestamptz not null default clock_timestamp()
);

create table entries (
  id uuid primary key,
  kind entry_kind not null,
  title text not null,
  body  text not null,
  project text,
  scope entry_scope not null default 'personal',
  owner_id uuid not null references principals(id),
  tags  text[] not null default '{}',
  links uuid[] not null default '{}',
  agent text,
  session_id text,
  origin entry_origin not null default 'agent',
  superseded_by uuid references entries(id),
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  search tsvector generated always as (
    setweight(to_tsvector('english', title), 'A') ||
    setweight(to_tsvector('english', body),  'B') ||
    setweight(to_tsvector('english', remem_array_to_string_immutable(tags,' ')), 'C')
  ) stored
);
create index entries_search_idx on entries using gin(search);
create index entries_tags_idx   on entries using gin(tags);
create index entries_owner_idx  on entries (owner_id, project);

create table collections (
  id uuid primary key,
  slug text not null,
  title text not null,
  description text,
  project text,
  scope entry_scope not null default 'personal',
  owner_id uuid not null references principals(id),
  query jsonb not null default '{}',
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  unique (owner_id, slug)
);

create table collection_members (
  collection_id uuid not null references collections(id) on delete cascade,
  entry_id      uuid not null references entries(id)     on delete cascade,
  position int not null default 0,
  primary key (collection_id, entry_id)
);
