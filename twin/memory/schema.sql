-- Digital-twin memory model (spec section 7).
-- Single-user by design: user_profile always holds exactly one row (id = 1).

CREATE TABLE IF NOT EXISTS user_profile (
    id                       INTEGER PRIMARY KEY DEFAULT 1,
    -- Priority weighting seeded by the onboarding questionnaire and adjusted
    -- as the agent observes what the user actually engages with.
    priorities               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Concrete interest tags, used for Exa opportunity matching.
    interests                JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- Which integrations the user explicitly opted into.
    connected_apps           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    staleness_threshold_days INTEGER     NOT NULL DEFAULT 14,
    communication_style      TEXT        NOT NULL DEFAULT '',
    -- Durable observed facts: {"key": {"value": ..., "observed_at": ...}}
    facts                    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    onboarded_at             TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_profile_singleton CHECK (id = 1)
);

-- Added after the initial schema; ALTER ... IF NOT EXISTS keeps this file
-- idempotent for databases created before these columns existed.
--
-- Free-time detection needs to know when this user is actually available.
-- Both are user settings rather than defaults baked into the code, so the
-- agent never assumes a life stage (spec section 3).
ALTER TABLE user_profile
    ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC';
ALTER TABLE user_profile
    ADD COLUMN IF NOT EXISTS active_hours JSONB NOT NULL
    DEFAULT '{"start": 9, "end": 22}'::jsonb;

-- Free text ("Lagos, Nigeria"). Opportunity discovery needs somewhere to
-- anchor "near me"; an empty value just makes searches non-local.
ALTER TABLE user_profile
    ADD COLUMN IF NOT EXISTS location TEXT NOT NULL DEFAULT '';

-- Cooldown marker for the opportunity scan. A dedicated column rather than a
-- `facts` entry, because facts are injected into the agent's system prompt and
-- scheduler bookkeeping has no business being there.
ALTER TABLE user_profile
    ADD COLUMN IF NOT EXISTS last_opportunity_scan TIMESTAMPTZ;

-- Cursor for the email/calendar deadline sweep. Doubles as the `after:` bound
-- on the Gmail query, so repeat runs only read genuinely new mail.
ALTER TABLE user_profile
    ADD COLUMN IF NOT EXISTS last_deadline_scan TIMESTAMPTZ;

-- Gmail push state (spec section 6). `gmail_history_id` is the replay cursor
-- for history().list; `gmail_watch_expires_at` drives renewal, since a Gmail
-- watch silently stops delivering after 7 days.
ALTER TABLE user_profile
    ADD COLUMN IF NOT EXISTS gmail_history_id TEXT;
ALTER TABLE user_profile
    ADD COLUMN IF NOT EXISTS gmail_watch_expires_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS projects (
    id               SERIAL PRIMARY KEY,
    name             TEXT        NOT NULL UNIQUE,
    description      TEXT        NOT NULL DEFAULT '',
    status           TEXT        NOT NULL DEFAULT 'active',
    -- Drives the staleness nudge; bumped whenever the user mentions or works
    -- on the project.
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source           TEXT        NOT NULL DEFAULT 'conversation',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT projects_status_valid
        CHECK (status IN ('active', 'paused', 'done', 'abandoned'))
);

CREATE INDEX IF NOT EXISTS projects_stale_idx
    ON projects (last_activity_at) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS deadlines (
    id          SERIAL PRIMARY KEY,
    description TEXT        NOT NULL,
    due_at      TIMESTAMPTZ NOT NULL,
    source      TEXT        NOT NULL DEFAULT 'conversation',
    -- Stable identifier from the source system (Gmail message id, Calendar
    -- event id) so re-extraction updates rather than duplicates.
    source_ref  TEXT,
    status      TEXT        NOT NULL DEFAULT 'open',
    project_id  INTEGER     REFERENCES projects (id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT deadlines_status_valid
        CHECK (status IN ('open', 'done', 'dismissed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS deadlines_source_ref_idx
    ON deadlines (source, source_ref) WHERE source_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS deadlines_due_idx
    ON deadlines (due_at) WHERE status = 'open';

-- When a deadline was actually cleared. `status` says whether it's done;
-- this says when, which is what streaks and "kept this week" need.
ALTER TABLE deadlines
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS conversation_history (
    id         SERIAL PRIMARY KEY,
    role       TEXT        NOT NULL,
    -- Stored as JSONB rather than text because assistant turns carry
    -- structured tool_use / tool_result blocks that must replay verbatim.
    content    JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS conversation_history_created_idx
    ON conversation_history (created_at DESC);

-- Vector recall over emails, messages, and past conversation.
--
-- The spec calls for pgvector, but the extension is not available on this
-- Postgres install. Vectors are stored as REAL[] and cosine similarity is
-- computed in Python (numpy) instead -- at single-user data volumes that is
-- a few milliseconds. Swapping in pgvector later means changing the column
-- type and the one query in store.search_similar().
CREATE TABLE IF NOT EXISTS embeddings (
    id         SERIAL PRIMARY KEY,
    kind       TEXT        NOT NULL,
    ref_id     TEXT        NOT NULL,
    content    TEXT        NOT NULL,
    vector     REAL[],
    metadata   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS embeddings_ref_idx ON embeddings (kind, ref_id);

-- Queued proactive nudges produced by the scheduler, surfaced next time the
-- user interacts with the assistant.
CREATE TABLE IF NOT EXISTS nudges (
    id           SERIAL PRIMARY KEY,
    kind         TEXT        NOT NULL,
    message      TEXT        NOT NULL,
    -- The real tracked data behind the nudge, so the agent can cite evidence
    -- instead of sounding generic (spec section 3).
    evidence     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    status       TEXT        NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at TIMESTAMPTZ,
    CONSTRAINT nudges_status_valid
        CHECK (status IN ('pending', 'delivered', 'dismissed'))
);

CREATE INDEX IF NOT EXISTS nudges_pending_idx
    ON nudges (created_at) WHERE status = 'pending';

INSERT INTO user_profile (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
