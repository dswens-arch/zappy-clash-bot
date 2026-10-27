-- ============================================
-- CHAIN LIGHTNING — Supabase tables
-- Run in the Supabase SQL Editor (safe to re-run).
-- ============================================

-- One row per chain (live or ended)
CREATE TABLE IF NOT EXISTS chain_runs (
    id                BIGSERIAL PRIMARY KEY,
    guild_id          TEXT,
    channel_id        TEXT,
    status            TEXT DEFAULT 'live',          -- live | ended
    grounds_left      INT  DEFAULT 3,
    chain_count       INT  DEFAULT 1,
    started_by        TEXT,
    started_at        TIMESTAMPTZ DEFAULT NOW(),
    ended_at          TIMESTAMPTZ,
    used_zappy_ids    BIGINT[] DEFAULT '{}',        -- a Zappy can be used once per chain
    catches           JSONB DEFAULT '{}'::jsonb,    -- { discord_user_id: catch_count }
    last_catcher      TEXT,
    paid_milestones   INT[] DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_chain_runs_status ON chain_runs (status);
-- Database-level guarantee: only one live chain per server
CREATE UNIQUE INDEX IF NOT EXISTS one_live_chain_per_guild ON chain_runs (guild_id) WHERE status = 'live';

-- One row per bolt (each time the chain arcs to new targets)
CREATE TABLE IF NOT EXISTS chain_bolts (
    id                BIGSERIAL PRIMARY KEY,
    run_id            BIGINT REFERENCES chain_runs(id) ON DELETE CASCADE,
    bolt_no           INT,
    targets           TEXT[] DEFAULT '{}',
    lurkers           TEXT[] DEFAULT '{}',          -- subset of targets who were inactive (easier puzzle, bigger bonus)
    channel_id        TEXT,
    message_id        TEXT,
    puzzle_type       TEXT,                         -- circuit | odd | trivia
    nudged            TEXT[] DEFAULT '{}',          -- user ids who already nudged this bolt
    deadline          TIMESTAMPTZ,
    status            TEXT DEFAULT 'open',          -- open | caught | passed | expired | fizzled
    caught_by         TEXT,
    caught_at         TIMESTAMPTZ,
    pass_deadline     TIMESTAMPTZ,                  -- catcher must pick a Zappy by this time
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chain_bolts_msg    ON chain_bolts (message_id);
CREATE INDEX IF NOT EXISTS idx_chain_bolts_status ON chain_bolts (status, deadline);

-- Each tagged player gets their own puzzle variant (easier for lurkers) and 2 tries
CREATE TABLE IF NOT EXISTS chain_attempts (
    bolt_id           BIGINT REFERENCES chain_bolts(id) ON DELETE CASCADE,
    discord_user_id   TEXT,
    puzzle            JSONB NOT NULL,               -- includes the correct answer index
    wrong_count       INT DEFAULT 0,
    PRIMARY KEY (bolt_id, discord_user_id)
);

-- Who caught with which Zappy
CREATE TABLE IF NOT EXISTS chain_links (
    id                BIGSERIAL PRIMARY KEY,
    run_id            BIGINT REFERENCES chain_runs(id) ON DELETE CASCADE,
    link_no           INT,
    caught_by         TEXT,
    zappy_id          BIGINT,
    hero              TEXT,
    puzzle_type       TEXT,
    caught_at         TIMESTAMPTZ DEFAULT NOW()
);

-- Activity + opt-out. last_active drives the active/lurker split.
CREATE TABLE IF NOT EXISTS chain_prefs (
    discord_user_id   TEXT PRIMARY KEY,
    last_active       TIMESTAMPTZ DEFAULT NOW(),
    opted_out         BOOLEAN DEFAULT FALSE
);

-- Cached trivia questions (Zappy trivia is generated live; this holds OpenTDB)
CREATE TABLE IF NOT EXISTS trivia_pool (
    id                BIGSERIAL PRIMARY KEY,
    source            TEXT DEFAULT 'opentdb',
    category          TEXT,
    difficulty        TEXT,                         -- easy | medium | hard
    qtype             TEXT,                         -- multiple | boolean
    question          TEXT NOT NULL UNIQUE,
    correct           TEXT NOT NULL,
    wrong             JSONB NOT NULL,
    last_used         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_trivia_pool_pick ON trivia_pool (difficulty, last_used);
