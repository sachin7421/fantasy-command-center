-- The manager's own roster, so the hosted app can use it too.
--
-- Yahoo API approval is an external gate with no timeline. The roster he types
-- in already makes lineup, byes, recap and startsit work locally, but it lived
-- in a file - so the deployed dashboard could not see it, and the file could
-- not be committed because the paste it comes from carries Yahoo's projections.
--
-- This holds slots and our own player keys. Nothing Yahoo generated.

CREATE TABLE IF NOT EXISTS my_roster (
    league_key TEXT NOT NULL,
    season     INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    team_key   TEXT NOT NULL,
    player_key TEXT NOT NULL,
    slot       TEXT,
    played     INTEGER NOT NULL DEFAULT 0,
    entered_at TEXT NOT NULL,
    PRIMARY KEY (league_key, season, week, team_key, player_key)
);
