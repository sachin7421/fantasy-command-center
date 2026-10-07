"""Opportunity, usage and availability - the inputs a forecast should lean on.

Fantasy points are a poor predictor of future fantasy points, because they mix
two things with very different persistence:

    points = opportunity x efficiency

Opportunity (targets, carries, snaps, share of the team's work) is sticky week
to week. Efficiency (yards per touch, and above all touchdown rate) is close to
noise at the individual level over a 17-game season. So this module collects the
sticky half, plus the *expected* points that usage implies, and leaves the
modelling to src/analytics.

Sources, all from nflverse and free:

  load_ff_opportunity  expected fantasy points from usage
  load_snap_counts     snap share, the earliest signal of a changing role
  load_injuries        the OFFICIAL injury report, including practice status
  load_depth_charts    real depth order, so handcuffs are looked up not guessed
"""
from __future__ import annotations

import logging
from typing import Any

from src import db
from src.idmap import IdMapper, normalize_position, normalize_team
from src.sources.base import Source

log = logging.getLogger(__name__)

#: Practice designations, worst first. DNP on Wednesday AND Thursday is the
#: strongest public signal that a player will not play on Sunday.
PRACTICE_SEVERITY = {
    "Did Not Participate In Practice": 3,
    "Limited Participation in Practice": 2,
    "Full Participation in Practice": 1,
}


def _rows(frame) -> list[dict[str, Any]]:
    """polars/pandas -> list of dicts, whichever nflreadpy returns."""
    if frame is None:
        return []
    if hasattr(frame, "iter_rows"):
        return list(frame.iter_rows(named=True))
    return frame.to_dict("records")


#: Categories this league scores that the ground-truth ingest cannot supply.
#: Empty since 7 Oct 2026: the actual line now comes from the box score
#: (`load_player_stats`), which carries every scored offensive category. It
#: used to hold the two return touchdowns, genuinely absent from
#: ff_opportunity. Kept as the declaration the warning below checks against,
#: so a category the league adds, or a column nflverse drops, is a failure.
KNOWN_MISSING_STATS: frozenset[str] = frozenset()


def box_score_line(row: dict[str, Any]) -> dict[str, float]:
    """One player-week of nflverse `player_stats` as a scoreable stat line.

    This is the ground truth every projection is graded against, so it is
    read from the box score and nothing else. The first live comparison with
    Yahoo (7 Oct 2026, 45 player-weeks) matched to the cent everywhere except
    fumbles: the opportunity dataset used before this has no sack fumbles and
    no self-recovered ones. `fumbles_total` and `fumbles_lost_total` here
    reproduced all four differences exactly.

    A missing column reads as zero. That is the right default for a box
    score, where absence means it did not happen - and `_warn_scoring_gaps`
    is what catches a scored category going missing wholesale.
    """
    def num(column: str) -> float:
        return _f(row.get(column)) or 0.0

    return {
        "pass_yds": num("passing_yards"),
        "pass_td": num("passing_tds"),
        "pass_int": num("passing_interceptions"),
        "rush_yds": num("rushing_yards"),
        "rush_td": num("rushing_tds"),
        "rec": num("receptions"),
        "rec_yds": num("receiving_yards"),
        "rec_td": num("receiving_tds"),
        # Yahoo charges -1 for any fumble and a further -1 when it is lost.
        "fum": num("fumbles_total"),
        "fum_lost": num("fumbles_lost_total"),
        "two_pt": (
            num("passing_2pt_conversions")
            + num("rushing_2pt_conversions")
            + num("receiving_2pt_conversions")
        ),
        "ret_td": num("special_teams_tds"),
        "off_fum_ret_td": num("fumble_recovery_tds"),
    }


def _warn_scoring_gaps(scoring, stat_line: dict[str, float]) -> None:
    """Say out loud which scored categories this ingest cannot supply.

    `points_actual` is the ground truth every projection is graded against, and
    a category missing from the stat line scores zero exactly as if it never
    happened. Five were missing for months without any number looking wrong.
    """
    gaps = set(scoring.scoring_gaps(stat_line.keys())) - KNOWN_MISSING_STATS
    if gaps:
        log.warning(
            "points_actual is missing %d scored categor%s: %s - "
            "every actual is wrong whenever one of these happens",
            len(gaps), "y" if len(gaps) == 1 else "ies", ", ".join(sorted(gaps)),
        )


def _f(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


#: The positions this league rosters as players. Defenses are scored as a
#: unit from team stats, not from these rows.
BOX_SCORE_POSITIONS = frozenset({"QB", "RB", "FB", "WR", "TE"})


def _opportunity_line(row: dict[str, Any]) -> dict[str, float]:
    """The stat line ff_opportunity can supply, used only when the box score
    cannot be reached. It has no sack fumbles and no self-recovered ones, so
    `fum` is filled from the lost count: right for every lost fumble, low by
    one for every other, and no return touchdowns at all."""
    fumbles_lost = (
        (_f(row.get("rec_fumble_lost")) or 0.0)
        + (_f(row.get("rush_fumble_lost")) or 0.0)
    )
    return {
        "pass_yds": _f(row.get("pass_yards_gained")) or 0.0,
        "pass_td": _f(row.get("pass_touchdown")) or 0.0,
        "pass_int": _f(row.get("pass_interception")) or 0.0,
        "rush_yds": _f(row.get("rush_yards_gained")) or 0.0,
        "rush_td": _f(row.get("rush_touchdown")) or 0.0,
        "rec": _f(row.get("receptions")) or 0.0,
        "rec_yds": _f(row.get("rec_yards_gained")) or 0.0,
        "rec_td": _f(row.get("rec_touchdown")) or 0.0,
        "fum": fumbles_lost,
        "fum_lost": fumbles_lost,
        "two_pt": (
            (_f(row.get("pass_two_point_conv")) or 0.0)
            + (_f(row.get("rec_two_point_conv")) or 0.0)
            + (_f(row.get("rush_two_point_conv")) or 0.0)
        ),
    }


class UsageSource(Source):
    name = "nflverse_usage"

    # -- resolution ----------------------------------------------------------

    def _key_by_gsis(self) -> dict[str, str]:
        """gsis_id -> player_key, using the ids Sleeper already bridged for us."""
        return {
            r["gsis_id"]: r["player_key"]
            for r in self.conn.fetchall(
                "SELECT gsis_id, player_key FROM players WHERE gsis_id IS NOT NULL"
            )
        }

    # -- expected points & usage --------------------------------------------

    def sync_usage(
        self, idmap: IdMapper, scoring, season: int, force: bool = False
    ) -> dict[str, int]:
        """Store per-week opportunity and expected points.

        `points_expected` is scored with OUR league rules from the expected stat
        line, not taken from nflverse's own scoring - the same rule that applies
        to every projection source here.
        """
        try:
            import nflreadpy as nfl
        except ImportError:
            log.warning("nflreadpy not installed; skipping usage sync")
            return {"rows": 0, "stored": 0}

        stats = {"rows": 0, "stored": 0, "unmatched": 0, "actuals": 0}
        by_gsis = self._key_by_gsis()

        try:
            opportunity = _rows(nfl.load_ff_opportunity(seasons=[season]))
        except Exception as exc:
            log.warning("ff_opportunity unavailable for %s: %s", season, exc)
            return stats

        # The box score is the ground truth; the opportunity model is the
        # usage. Keyed the same way (gsis id, week) so they join without a
        # name match. When the box score is unreachable the opportunity line
        # stands in, with the warning that it undercounts fumbles.
        box_scores = self._box_score_lines(season)

        snaps = self._snap_share(season)
        recorded_at = db.utcnow()

        usage_rows: list[tuple] = []
        actual_rows: list[tuple] = []
        actual_seen: set[tuple[str, int]] = set()
        for row in opportunity:
            stats["rows"] += 1
            gsis = row.get("player_id")
            key = by_gsis.get(gsis) if gsis else None
            if not key:
                match = idmap.resolve(
                    source="nflverse", source_id=gsis, name=row.get("full_name") or "",
                    position=row.get("position"), team=row.get("posteam"),
                )
                key = match.player_key
            if not key:
                stats["unmatched"] += 1
                continue

            week = row.get("week")
            if week is None:
                continue

            # Fumbles and two-point conversions are scored by this league and
            # were absent from both lines, so `points_actual` - described below
            # as the ground truth every projection is graded against - was
            # simply wrong whenever either happened. Measured over 2025: 4.0% of
            # player-weeks affected, up to 4.0 points on a single week (Jared
            # Goff wk17 -4.0, Justin Fields wk5 +4.0). The aggregate bias is
            # only -0.016 pts/week because fumbles and conversions nearly
            # cancel, which is exactly why it survived: it is invisible in the
            # average and wrong in the individual week a lineup is decided on.
            two_pt_exp = (
                (_f(row.get("pass_two_point_conv_exp")) or 0.0)
                + (_f(row.get("rec_two_point_conv_exp")) or 0.0)
                + (_f(row.get("rush_two_point_conv_exp")) or 0.0)
            )
            expected_line = {
                "pass_yds": _f(row.get("pass_yards_gained_exp")) or 0.0,
                "pass_td": _f(row.get("pass_touchdown_exp")) or 0.0,
                "pass_int": _f(row.get("pass_interception_exp")) or 0.0,
                "rush_yds": _f(row.get("rush_yards_gained_exp")) or 0.0,
                "rush_td": _f(row.get("rush_touchdown_exp")) or 0.0,
                "rec": _f(row.get("receptions_exp")) or 0.0,
                "rec_yds": _f(row.get("rec_yards_gained_exp")) or 0.0,
                "rec_td": _f(row.get("rec_touchdown_exp")) or 0.0,
                "two_pt": two_pt_exp,
                # No fumble_lost_exp exists in this dataset. Left at zero rather
                # than invented, which biases EXPECTED slightly high - stated
                # here because an unstated zero reads as "does not happen".
            }
            boxed = box_scores.get((str(gsis), int(week))) if box_scores else None
            actual_line = boxed if boxed is not None else _opportunity_line(row)

            team = normalize_team(row.get("posteam"))
            snap_pct = snaps.get((key, int(week)))

            usage_rows.append((
                key, season, int(week), team,
                _f(row.get("pass_attempt")), _f(row.get("rush_attempt")),
                _f(row.get("rec_attempt")), _f(row.get("receptions")),
                None, None, snap_pct, _f(row.get("rec_air_yards")),
                scoring.score(actual_line), scoring.score(expected_line),
                recorded_at,
            ))
            if stats["stored"] == 0:
                # Once per sync, on the first real row, so the declaration is
                # checked against the line actually built rather than a list
                # someone kept in their head.
                _warn_scoring_gaps(scoring, actual_line)
            # The same number is the ground truth every projection is graded
            # against, so record it where the accuracy model looks for it.
            actual_rows.append((
                key, season, int(week), scoring.score(actual_line), None,
                "nflverse", recorded_at,
            ))
            actual_seen.add((key, int(week)))
            stats["stored"] += 1

        # Player-weeks the opportunity model has no row for - a return
        # touchdown and nothing else, or a week it has not published yet -
        # still happened, and Yahoo scored them. The box score is complete on
        # its own, so every offensive line it has is recorded.
        for (gsis, week), line in box_scores.items():
            key = by_gsis.get(gsis)
            if not key or (key, week) in actual_seen:
                continue
            actual_rows.append((
                key, season, week, scoring.score(line), None, "nflverse", recorded_at,
            ))
            actual_seen.add((key, week))
        stats["actuals"] = len(actual_rows)

        # Both in one statement each: a row at a time is a round trip at a
        # time, and this loop runs over every player-week of the season.
        self.conn.executemany(
            "INSERT INTO player_week_usage(player_key, season, week, team, "
            "pass_attempts, rush_attempts, targets, receptions, target_share, "
            "rush_share, snap_pct, air_yards, points_actual, points_expected, "
            "recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(player_key, season, week) DO UPDATE SET "
            "points_actual=excluded.points_actual, "
            "points_expected=excluded.points_expected, "
            "targets=excluded.targets, receptions=excluded.receptions, "
            "rush_attempts=excluded.rush_attempts, snap_pct=excluded.snap_pct, "
            "air_yards=excluded.air_yards, recorded_at=excluded.recorded_at",
            usage_rows,
        )
        db.record_actuals_many(self.conn, actual_rows)
        self.conn.commit()
        self._fill_shares(season)
        return stats

    def _box_score_lines(self, season: int) -> dict[tuple[str, int], dict[str, float]]:
        """(gsis id, week) -> scoreable stat line, for every offensive skill
        player-week in nflverse's weekly box score. Empty when unreachable,
        with a warning: the caller then falls back to the opportunity line."""
        try:
            import nflreadpy as nfl

            rows = _rows(nfl.load_player_stats(seasons=[season]))
        except Exception as exc:
            log.warning(
                "player_stats unavailable for %s (%s): actuals fall back to "
                "ff_opportunity, which undercounts fumbles", season, exc,
            )
            return {}

        out: dict[tuple[str, int], dict[str, float]] = {}
        for row in rows:
            if row.get("position") not in BOX_SCORE_POSITIONS:
                continue
            gsis, week = row.get("player_id"), row.get("week")
            if not gsis or week is None or row.get("season_type") not in (None, "REG"):
                continue
            out[(str(gsis), int(week))] = box_score_line(row)
        return out

    def _snap_share(self, season: int) -> dict[tuple[str, int], float]:
        """(player_key, week) -> offensive snap share."""
        try:
            import nflreadpy as nfl

            rows = _rows(nfl.load_snap_counts(seasons=[season]))
        except Exception as exc:
            log.info("snap counts unavailable: %s", exc)
            return {}

        from src.idmap import normalize_name

        lookup = {
            (normalize_name(r["full_name"]), normalize_team(r["team"])): r["player_key"]
            for r in self.conn.fetchall(
                "SELECT full_name, team, player_key FROM players WHERE team IS NOT NULL"
            )
        }
        out: dict[tuple[str, int], float] = {}
        for row in rows:
            key = lookup.get(
                (normalize_name(row.get("player") or ""), normalize_team(row.get("team")))
            )
            pct = _f(row.get("offense_pct"))
            if key and pct is not None and row.get("week") is not None:
                out[(key, int(row["week"]))] = pct
        return out

    def _fill_shares(self, season: int) -> None:
        """Compute each player's share of his own team's opportunity.

        Share is what persists; raw counts move with game script, so a player
        whose team simply ran more plays should not look like he gained a role.
        """
        self.conn.execute(
            """
            UPDATE player_week_usage AS u
               SET target_share = (
                     SELECT CASE WHEN SUM(t.targets) > 0
                                 THEN u.targets / SUM(t.targets) END
                     FROM player_week_usage t
                     WHERE t.season=u.season AND t.week=u.week AND t.team=u.team
                   ),
                   rush_share = (
                     SELECT CASE WHEN SUM(t.rush_attempts) > 0
                                 THEN u.rush_attempts / SUM(t.rush_attempts) END
                     FROM player_week_usage t
                     WHERE t.season=u.season AND t.week=u.week AND t.team=u.team
                   )
             WHERE u.season = ?
            """,
            (season,),
        )
        self.conn.commit()

    # -- official injury report ---------------------------------------------

    def sync_practice_reports(
        self, idmap: IdMapper, season: int, force: bool = False
    ) -> int:
        """The official report, including practice participation.

        This is the piece the Sleeper feed does not carry. A player limited or
        out of practice midweek is far likelier to sit than his game-status tag
        alone suggests, and it is knowable on Thursday rather than at kickoff.
        """
        try:
            import nflreadpy as nfl

            rows = _rows(nfl.load_injuries(seasons=[season]))
        except Exception as exc:
            log.warning("injury report unavailable: %s", exc)
            return 0

        by_gsis = self._key_by_gsis()
        recorded_at = db.utcnow()
        stored = 0

        for row in rows:
            gsis_id = row.get("gsis_id")
            key = by_gsis.get(gsis_id) if gsis_id else None
            if not key:
                match = idmap.resolve(
                    source="nflverse", source_id=row.get("gsis_id"),
                    name=row.get("full_name") or "", position=row.get("position"),
                    team=row.get("team"),
                )
                key = match.player_key
            if not key or row.get("week") is None:
                continue

            self.conn.execute(
                "INSERT INTO practice_reports(player_key, season, week, report_status, "
                "practice_status, primary_injury, recorded_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(player_key, season, week) DO UPDATE SET "
                "report_status=excluded.report_status, "
                "practice_status=excluded.practice_status, "
                "primary_injury=excluded.primary_injury, "
                "recorded_at=excluded.recorded_at",
                (
                    key, season, int(row["week"]), row.get("report_status"),
                    row.get("practice_status"), row.get("report_primary_injury"),
                    recorded_at,
                ),
            )
            stored += 1
        self.conn.commit()
        return stored

    # -- depth charts --------------------------------------------------------

    def sync_depth_charts(
        self, idmap: IdMapper, season: int, week: int | None = None
    ) -> int:
        """Real depth order, so a handcuff is looked up rather than inferred."""
        try:
            import nflreadpy as nfl

            rows = _rows(nfl.load_depth_charts(seasons=[season]))
        except Exception as exc:
            log.warning("depth charts unavailable: %s", exc)
            return 0

        by_gsis = self._key_by_gsis()
        recorded_at = db.utcnow()

        # The feed is timestamped rather than weekly; keep only the newest entry
        # per (team, position, rank) so the table reflects the current chart.
        latest: dict[tuple, dict] = {}
        for row in rows:
            position = normalize_position(row.get("pos_abb") or row.get("pos_name"))
            if position not in ("QB", "RB", "WR", "TE", "K"):
                continue
            rank = row.get("pos_rank")
            team = normalize_team(row.get("team"))
            if rank is None or not team:
                continue
            slot = (team, position, int(rank))
            stamp = str(row.get("dt") or "")
            if slot not in latest or stamp > latest[slot]["_stamp"]:
                latest[slot] = {**row, "_stamp": stamp, "_pos": position}

        stored = 0
        target_week = week or 0
        for (team, position, rank), row in latest.items():
            gsis_id = row.get("gsis_id")
            key = by_gsis.get(gsis_id) if gsis_id else None
            name = row.get("player_name") or ""
            if not key and name:
                key = idmap.resolve(
                    source="nflverse", source_id=row.get("gsis_id"), name=name,
                    position=position, team=team,
                ).player_key
            self.conn.execute(
                "INSERT INTO depth_charts(season, week, team, position, depth_rank, "
                "player_key, player_name, recorded_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(season, week, team, position, depth_rank) DO UPDATE SET "
                "player_key=excluded.player_key, player_name=excluded.player_name, "
                "recorded_at=excluded.recorded_at",
                (season, target_week, team, position, rank, key, name, recorded_at),
            )
            stored += 1
        self.conn.commit()
        return stored

    def handcuff_for(self, player_key: str, season: int) -> dict[str, Any] | None:
        """The next man up at the same position on the same team."""
        row = self.conn.fetchone(
            "SELECT team, position FROM players WHERE player_key=?", (player_key,)
        )
        if not row or not row["team"]:
            return None
        backup = self.conn.fetchone(
            "SELECT player_key, player_name, depth_rank FROM depth_charts "
            "WHERE season=? AND team=? AND position=? AND depth_rank>1 "
            "ORDER BY depth_rank LIMIT 1",
            (season, row["team"], row["position"]),
        )
        return dict(backup) if backup else None

    def health(self) -> dict[str, Any]:
        try:
            import nflreadpy  # noqa: F401
        except Exception as exc:
            return {"source": self.name, "ok": False, "error": str(exc)}
        n = self.conn.scalar("SELECT COUNT(*) FROM player_week_usage") or 0
        return {"source": self.name, "ok": True, "usage_rows": n}
