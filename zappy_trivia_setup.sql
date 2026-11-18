-- ============================================
-- ZAPPY TRIVIA NIGHT — Supabase tables
-- Run in the Supabase SQL Editor (safe to re-run).
-- Optional: without these the game still runs, it just keeps no history
-- (no all-time leaderboard, no "don't repeat recent questions", and a redeploy
-- during the scheduled hour could start a second round).
-- ============================================

CREATE TABLE IF NOT EXISTS trivia_rounds (
    id               BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    host_zappy       TEXT,
    champion_user_id TEXT,
    question_count   INT,
    players          INT,
    questions        JSONB DEFAULT '[]'::jsonb   -- question texts, used to avoid repeats
);
CREATE INDEX IF NOT EXISTS idx_trivia_rounds_started ON trivia_rounds (started_at DESC);

CREATE TABLE IF NOT EXISTS trivia_scores (
    id           BIGSERIAL PRIMARY KEY,
    round_id     BIGINT REFERENCES trivia_rounds(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL,
    display_name TEXT,
    points       INT NOT NULL DEFAULT 0,
    correct      INT NOT NULL DEFAULT 0,
    answered     INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_trivia_scores_user ON trivia_scores (user_id);
