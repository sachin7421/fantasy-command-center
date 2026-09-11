-- Scoring rules move out of the database.
--
-- The table held a copy of src/league_bootstrap.py, which `fcc sync-settings`
-- then overwrote from the Yahoo API. That overwrite is the problem: the stored
-- row became Yahoo data, which obligation 1 forbids keeping.
--
-- The transcription in the repository is the source of truth now - it was read
-- off a screen by the league's own manager, so it is his configuration rather
-- than data obtained from Yahoo. `fcc verify-settings` diffs it against a live
-- fetch and reports the difference instead of storing it.

DROP TABLE IF EXISTS league_settings;
