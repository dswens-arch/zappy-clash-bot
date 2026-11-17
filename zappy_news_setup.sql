-- ============================================
-- ZAPPY AGENT WIRE — Supabase tables
-- Run in the Supabase SQL Editor (safe to re-run).
-- ============================================

-- Shuffle bag: every Zappy takes a shift as agent before anyone repeats.
-- When every Zappy in the pool is in here, the cog clears it and starts a new cycle.
CREATE TABLE IF NOT EXISTS news_anchor_bag (
    asset_id   BIGINT PRIMARY KEY,
    used_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Stories already reported (headline links, battle ids, prize ids ...) so nothing airs twice.
CREATE TABLE IF NOT EXISTS news_seen (
    key        TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_news_seen_created ON news_seen (created_at);

-- Every broadcast (also drives daily caps and per-segment spacing across restarts).
CREATE TABLE IF NOT EXISTS news_log (
    id               BIGSERIAL PRIMARY KEY,
    segment          TEXT NOT NULL,
    anchor_asset_id  BIGINT,
    channel_id       TEXT,
    message_id       TEXT,
    breaking         BOOLEAN DEFAULT FALSE,
    posted_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_news_log_posted  ON news_log (posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_log_segment ON news_log (segment, posted_at DESC);
