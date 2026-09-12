-- Record whether a player's game was already over when the roster was entered.
--
-- Migration 0005 created my_roster without this, and 0005 had already run
-- against the live database - so editing it changed nothing, which is the
-- exact reason this project has numbered migrations rather than re-running a
-- template: CREATE TABLE IF NOT EXISTS never adds a column to a table that
-- exists.
--
-- Why the column is needed at all: nflverse publishes actual scores days
-- after the games, so deriving "already played" only from player_week_actuals
-- returned NOTHING for week 1 2026 while four of the manager's games were
-- already final. The optimiser was then free to recommend starting a
-- quarterback who had finished on 5.1 points.
--
-- Finality is monotonic - a game that was over when the roster was pasted is
-- still over - so recording it can only ever be conservative. It is unioned
-- with the actuals at read time.

ALTER TABLE my_roster ADD COLUMN played INTEGER NOT NULL DEFAULT 0;
