-- ============================================
-- ZAPPY CLASH — Supabase Database Setup
-- ============================================
-- Run this entire script in Supabase:
--   1. Go to your Supabase project
--   2. Click "SQL Editor" in the left sidebar
--   3. Paste this entire file
--   4. Click "Run"
-- ============================================


-- ── Wallet links ─────────────────────────────
-- One wallet per Discord user
CREATE TABLE IF NOT EXISTS wallets (
    discord_user_id  TEXT PRIMARY KEY,
    wallet_address   TEXT NOT NULL,
    linked_at        TIMESTAMPTZ DEFAULT NOW()
);


-- ── Bracket entries ───────────────────────────
-- Tracks who registered for each session bracket
CREATE TABLE IF NOT EXISTS bracket_entries (
    id               BIGSERIAL PRIMARY KEY,
    discord_user_id  TEXT NOT NULL,
    asset_id         BIGINT NOT NULL,
    bracket_id       TEXT NOT NULL,   -- e.g. "morning_2025-01-15"
    registered_at    TIMESTAMPTZ DEFAULT NOW(),
    status           TEXT DEFAULT 'registered',
    UNIQUE (discord_user_id, bracket_id)
);

CREATE INDEX IF NOT EXISTS idx_bracket_entries_bracket_id
    ON bracket_entries (bracket_id);


-- ── Battle results ────────────────────────────
-- Full history of every fight
CREATE TABLE IF NOT EXISTS battles (
    id                 BIGSERIAL PRIMARY KEY,
    bracket_id         TEXT NOT NULL,
    winner_discord_id  TEXT NOT NULL,
    loser_discord_id   TEXT NOT NULL,
    winner_asset_id    BIGINT NOT NULL,
    loser_asset_id     BIGINT NOT NULL,
    is_upset           BOOLEAN DEFAULT FALSE,
    bracket_round      INT DEFAULT 1,
    played_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_battles_winner ON battles (winner_discord_id);
CREATE INDEX IF NOT EXISTS idx_battles_loser  ON battles (loser_discord_id);


-- ── Leaderboard ───────────────────────────────
-- Running CP totals per player
CREATE TABLE IF NOT EXISTS leaderboard (
    discord_user_id  TEXT PRIMARY KEY,
    cp_total         BIGINT DEFAULT 0,
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);


-- ── CP transaction log ────────────────────────
-- Audit trail of every CP award
CREATE TABLE IF NOT EXISTS cp_log (
    id               BIGSERIAL PRIMARY KEY,
    discord_user_id  TEXT NOT NULL,
    amount           INT NOT NULL,
    reason           TEXT,
    logged_at        TIMESTAMPTZ DEFAULT NOW()
);


-- ── Streaks ───────────────────────────────────
-- Daily play streak tracking
CREATE TABLE IF NOT EXISTS streaks (
    discord_user_id   TEXT PRIMARY KEY,
    current_streak    INT DEFAULT 0,
    longest_streak    INT DEFAULT 0,
    last_played_date  DATE,
    total_wins        INT DEFAULT 0,
    total_played      INT DEFAULT 0
);


-- ============================================
-- Done! All tables created.
-- Next: go back to Railway and deploy your bot.
-- ============================================


-- ── Expedition runs ───────────────────────────
CREATE TABLE IF NOT EXISTS expedition_runs (
    id               BIGSERIAL PRIMARY KEY,
    discord_user_id  TEXT NOT NULL,
    zone_num         INT NOT NULL,
    cp_earned        INT DEFAULT 0,
    tokens_earned    INT DEFAULT 0,
    nft_dropped      BOOLEAN DEFAULT FALSE,
    run_date         DATE NOT NULL DEFAULT CURRENT_DATE,
    completed_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_expedition_one_per_day
    ON expedition_runs (discord_user_id, run_date);

-- ── Expedition leaderboard ────────────────────
CREATE TABLE IF NOT EXISTS expedition_leaderboard (
    discord_user_id  TEXT PRIMARY KEY,
    exp_cp_total     BIGINT DEFAULT 0,
    runs_completed   INT DEFAULT 0,
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);
