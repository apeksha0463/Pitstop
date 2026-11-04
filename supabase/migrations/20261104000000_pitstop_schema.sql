-- Pitstop schema: restaurants on the Mall of Asia -> Big Bay route, their reviews,
-- washroom mentions and grades, and review image metadata.
--
-- Idempotent: safe to re-apply (scripts/upload_to_supabase.py applies it on every
-- real run). It never drops or truncates anything.
--
-- Access: Row Level Security is on for every table, with one read-only SELECT
-- policy for anon and authenticated and no insert/update/delete policies, so the
-- publishable key can only read. Writes go through the secret key, which bypasses RLS.

create table if not exists public.restaurants (
    data_id             text primary key,
    name                text not null,
    location            text,
    lat                 double precision not null,
    lng                 double precision not null,
    google_rating       double precision,
    google_review_count integer,
    cuisine             text,
    position            text,                       -- as listed in the seed CSV
    route_km            double precision not null,  -- distance along the route (build_route_map.py)
    side                text not null check (side in ('west', 'east')),
    detour_km           double precision,           -- null when on the highway
    maps_link           text,
    position_note       text                        -- why the position needs a manual check, if it does
);

create table if not exists public.reviews (
    data_id     text not null references public.restaurants (data_id) on delete cascade,
    review_id   text not null,
    iso_date    timestamptz,
    rating      double precision,
    text        text,
    language    text,
    translated  boolean not null default false,
    sources     text[] not null default '{}',
    matched_by  text[] not null default '{}',
    review_link text,
    primary key (data_id, review_id)               -- also the data_id index
);

create table if not exists public.washroom_mentions (
    mention_id       text primary key,
    restaurant       text,
    data_id          text not null,
    review_id        text not null,
    review_date      text,
    iso_date         timestamptz,
    star_rating      double precision,
    washroom_snippet text not null,
    passage_count    integer,
    full_review_text text,
    review_link      text,
    aspect           text,
    sentiment        text check (sentiment in ('positive', 'neutral', 'negative')),
    sentiment_score  double precision,
    confidence       double precision,
    p_negative       double precision,
    p_neutral        double precision,
    p_positive       double precision,
    needs_review     boolean not null default false,
    foreign key (data_id, review_id) references public.reviews (data_id, review_id) on delete cascade
);

create table if not exists public.washroom_summary (
    data_id                     text primary key references public.restaurants (data_id) on delete cascade,
    -- washroom_summary.csv
    google_review_count         integer,
    sample_reviews              integer,
    sample_reviews_with_text    integer,
    washroom_in_sample          integer,
    washroom_share              double precision,
    washroom_search_reviews     integer,
    washroom_related_reviews    integer,
    latest_mention_date         timestamptz,
    -- washroom_sentiment_summary.csv
    washroom_reviews            integer not null default 0,
    positive                    integer not null default 0,
    neutral                     integer not null default 0,
    negative                    integer not null default 0,
    mean_sentiment_score        double precision,
    share_negative              double precision,
    recent_reviews              integer,
    recent_mean_sentiment_score double precision,
    needs_review                integer,
    low_data                    boolean not null default true,
    -- same thresholds as the route map: >= 0.3 good, >= -0.3 moderate, else bad
    grade                       text not null check (grade in ('good', 'moderate', 'bad', 'none'))
);

create table if not exists public.review_images (
    storage_path    text primary key,                -- <data_id folder>/<file> in the review-images bucket
    data_id         text not null references public.restaurants (data_id) on delete cascade,
    review_id       text,                            -- null for the restaurant's main photo
    image_index     integer not null,
    sha256          text not null,
    content_type    text not null,
    file_size_bytes bigint not null,
    foreign key (data_id, review_id) references public.reviews (data_id, review_id) on delete cascade
);

create index if not exists reviews_iso_date_idx           on public.reviews (iso_date);
create index if not exists washroom_mentions_data_id_idx  on public.washroom_mentions (data_id);
create index if not exists washroom_mentions_iso_date_idx on public.washroom_mentions (iso_date);
create index if not exists review_images_data_id_idx      on public.review_images (data_id);

-- Read-only public access
do $$
declare t text;
begin
    foreach t in array array['restaurants', 'reviews', 'washroom_mentions', 'washroom_summary', 'review_images'] loop
        execute format('alter table public.%I enable row level security', t);
        -- RLS has no TRUNCATE check, so take the write privileges away as well
        execute format('revoke insert, update, delete, truncate on public.%I from anon, authenticated', t);
        execute format('grant select on public.%I to anon, authenticated', t);
        if not exists (select 1 from pg_policies where schemaname = 'public' and tablename = t and policyname = 'Public read') then
            execute format('create policy "Public read" on public.%I for select to anon, authenticated using (true)', t);
        end if;
    end loop;
end $$;
