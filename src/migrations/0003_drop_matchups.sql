-- Matchups and standings are Yahoo data too.
--
-- Playoff odds needed a schedule and never had a writer for one. The obvious
-- fix was to sync `matchups` into a table - which would have reintroduced
-- exactly what obligation 1 forbids, in a pair of tables nobody had thought to
-- name when the first four were dropped. They live on the LeagueSnapshot
-- instead, like every other piece of Yahoo league state.

DROP TABLE IF EXISTS matchups;
DROP TABLE IF EXISTS standings_history;
