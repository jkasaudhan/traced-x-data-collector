-- ============================================================================
--  Disinformation Evidence Collector — PostgreSQL schema
--  Case-based, multi-platform. Twitter/X implemented first.
-- ============================================================================

-- Extensions. Both exist on Supabase and on a stock local Postgres, but a
-- restricted role may not be allowed to create them, so a failure here must
-- not take the whole schema down with it.
--   pgcrypto : gen_random_uuid() — built in from PG13, so this is belt-and-braces
--   pg_trgm  : fuzzy handle matching for impersonation lookups
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
EXCEPTION WHEN insufficient_privilege OR feature_not_supported THEN
    RAISE NOTICE 'pgcrypto not created (gen_random_uuid is built in from PG13)';
END $$;

DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EXCEPTION WHEN insufficient_privilege OR feature_not_supported THEN
    RAISE NOTICE 'pg_trgm not created — trigram indexes will be skipped';
END $$;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

DO $$ BEGIN
    CREATE TYPE platform_t AS ENUM ('twitter', 'tiktok', 'instagram', 'youtube',
                                    'facebook', 'reddit', 'bluesky', 'other');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- How an item relates to the subject of the case.
--   subject_own          author is a confirmed genuine account of the subject
--   subject_affiliated   author is a confirmed campaign/office/staff account
--   impersonation_suspect handle resembles a subject handle but ID is not registered
--   third_party          anyone else
--   unknown              author could not be resolved
DO $$ BEGIN
    CREATE TYPE attribution_t AS ENUM ('subject_own', 'subject_affiliated',
                                       'impersonation_suspect', 'third_party', 'unknown');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Role a registered account plays for the subject.
DO $$ BEGIN
    CREATE TYPE account_role_t AS ENUM ('personal', 'campaign', 'office', 'staff', 'known_impersonator');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE run_status_t AS ENUM ('pending', 'running', 'succeeded', 'failed', 'budget_stopped');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- Case: one investigation about one public figure / entity
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            TEXT        NOT NULL UNIQUE,     -- 'mamdani-2025-nyc'
    subject_name    TEXT        NOT NULL,            -- 'Zohran Mamdani'
    subject_role    TEXT,                            -- 'Mayor of New York City'
    description     TEXT,
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    -- Free-form terms used to decide whether an item is "about" the subject.
    about_terms     TEXT[]      NOT NULL DEFAULT '{}',
    config          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (window_end > window_start)
);

-- ---------------------------------------------------------------------------
-- Registered genuine accounts — the ground truth for attribution.
-- Validity window matters: an *office* handle (e.g. @NYCMayor) belongs to a
-- different person before the subject takes office.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS subject_accounts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID        NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    platform            platform_t  NOT NULL,
    handle              TEXT        NOT NULL,         -- without '@', lowercased
    platform_user_id    TEXT,                         -- authoritative; handles can change
    role                account_role_t NOT NULL DEFAULT 'personal',
    verified_genuine    BOOLEAN     NOT NULL DEFAULT TRUE,
    valid_from          TIMESTAMPTZ,                  -- NULL = always
    valid_until         TIMESTAMPTZ,                  -- NULL = still valid
    source_url          TEXT,                         -- how you verified it
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id, platform, handle)
);

CREATE INDEX IF NOT EXISTS idx_subject_accounts_lookup
    ON subject_accounts (case_id, platform, platform_user_id);
DO $$ BEGIN
    CREATE INDEX IF NOT EXISTS idx_subject_accounts_handle_trgm
        ON subject_accounts USING gin (handle gin_trgm_ops);
EXCEPTION WHEN undefined_object THEN
    RAISE NOTICE 'skipping trigram index on subject_accounts (pg_trgm missing)';
END $$;

-- ---------------------------------------------------------------------------
-- Collection runs — provenance + cost accounting
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID        NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    platform            platform_t  NOT NULL,
    tier                TEXT,                         -- 'subject_own' | 'mentions' | 'claim'
    actor_id            TEXT,                         -- 'jacquemus/x-tweet-scraper'
    query               TEXT        NOT NULL,
    slice_start         TIMESTAMPTZ,
    slice_end           TIMESTAMPTZ,
    max_items           INTEGER,
    items_returned      INTEGER     NOT NULL DEFAULT 0,
    items_inserted      INTEGER     NOT NULL DEFAULT 0,
    est_cost_usd        NUMERIC(10,4) NOT NULL DEFAULT 0,
    status              run_status_t NOT NULL DEFAULT 'pending',
    external_run_id     TEXT,                         -- Apify run id, for audit
    error               TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_runs_case ON collection_runs (case_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- Authors — one row per account seen, per platform (global, not per case)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS authors (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform            platform_t  NOT NULL,
    platform_user_id    TEXT        NOT NULL,
    handle              TEXT,
    display_name        TEXT,
    description         TEXT,
    account_created_at  TIMESTAMPTZ,
    followers_count     INTEGER,
    following_count     INTEGER,
    post_count          INTEGER,
    is_verified         BOOLEAN,
    is_blue_verified    BOOLEAN,
    profile_image_url   TEXT,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw                 JSONB,
    UNIQUE (platform, platform_user_id)
);

CREATE INDEX IF NOT EXISTS idx_authors_handle ON authors (platform, lower(handle));

-- ---------------------------------------------------------------------------
-- Content items — the canonical post table, platform-agnostic
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS content_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id             UUID        NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    platform            platform_t  NOT NULL,
    platform_item_id    TEXT        NOT NULL,
    author_id           UUID        REFERENCES authors(id) ON DELETE SET NULL,

    -- >>> the attribution the user asked for <<<
    attribution         attribution_t NOT NULL DEFAULT 'unknown',
    attribution_reason  TEXT,                         -- auditable: why we decided that

    posted_at           TIMESTAMPTZ,
    collected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    url                 TEXT,
    text                TEXT,
    lang                TEXT,

    -- engagement (nullable: not every platform has every metric)
    like_count          INTEGER,
    reply_count         INTEGER,
    repost_count        INTEGER,
    quote_count         INTEGER,
    view_count          BIGINT,
    bookmark_count      INTEGER,

    -- structure
    is_reply            BOOLEAN NOT NULL DEFAULT FALSE,
    is_repost           BOOLEAN NOT NULL DEFAULT FALSE,
    is_quote            BOOLEAN NOT NULL DEFAULT FALSE,
    parent_item_id      TEXT,                         -- platform id of parent
    conversation_id     TEXT,

    -- enrichment slots, filled by later stages (classifier, archiver)
    mentions_subject    BOOLEAN,
    archive_url         TEXT,
    content_sha256      TEXT,
    labels              JSONB NOT NULL DEFAULT '{}'::jsonb,

    collection_run_id   UUID REFERENCES collection_runs(id) ON DELETE SET NULL,
    raw                 JSONB       NOT NULL,
    UNIQUE (case_id, platform, platform_item_id)
);

CREATE INDEX IF NOT EXISTS idx_items_case_time   ON content_items (case_id, posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_attribution ON content_items (case_id, attribution);
CREATE INDEX IF NOT EXISTS idx_items_author      ON content_items (author_id);
DO $$ BEGIN
    CREATE INDEX IF NOT EXISTS idx_items_text_trgm
        ON content_items USING gin (text gin_trgm_ops);
EXCEPTION WHEN undefined_object THEN
    RAISE NOTICE 'skipping trigram index on content_items (pg_trgm missing)';
END $$;

-- ---------------------------------------------------------------------------
-- Media — one row per attached image/video. Hash columns are the evidence hook.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS media_assets (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_item_id     UUID        NOT NULL REFERENCES content_items(id) ON DELETE CASCADE,
    media_type          TEXT,                         -- 'photo' | 'video' | 'animated_gif'
    source_url          TEXT        NOT NULL,
    local_path          TEXT,
    sha256              TEXT,
    phash               TEXT,
    duration_sec        NUMERIC,
    width               INTEGER,
    height              INTEGER,
    raw                 JSONB,
    UNIQUE (content_item_id, source_url)
);

CREATE INDEX IF NOT EXISTS idx_media_phash ON media_assets (phash);

-- ---------------------------------------------------------------------------
-- Convenience view
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_case_items AS
SELECT
    ci.id,
    c.slug                AS case_slug,
    ci.platform,
    ci.attribution,
    ci.posted_at,
    a.handle              AS author_handle,
    a.followers_count,
    ci.like_count, ci.repost_count, ci.reply_count, ci.view_count,
    ci.url,
    ci.text
FROM content_items ci
JOIN cases c   ON c.id = ci.case_id
LEFT JOIN authors a ON a.id = ci.author_id;
