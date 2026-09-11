-- Remove the tables that held Yahoo league state.
--
-- API agreement signed 2026-09-10, obligation 1: Yahoo Fantasy data lives in
-- memory for the duration of a run and never reaches disk. Every reader was
-- converted to take a LeagueSnapshot instead, and tools/check_yahoo_persistence
-- fails the build if a write comes back.
--
-- Dropping rather than emptying, deliberately. An empty table is an invitation:
-- the next person to need a roster finds `rosters` sitting there and fills it,
-- and nothing about an empty table says why it should stay empty. A missing
-- table fails loudly at the moment somebody tries.
--
-- Nothing of ours is lost. Players, projections, blends, the draft board, the
-- draft picks typed in by hand and the learned FAAB coefficients are all in
-- other tables and are untouched.

DROP TABLE IF EXISTS rosters;
DROP TABLE IF EXISTS free_agents;
DROP TABLE IF EXISTS team_budgets;
DROP TABLE IF EXISTS transactions;
