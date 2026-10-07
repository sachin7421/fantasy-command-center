"""The season in dollars: what each outcome pays, and what this week is worth.

`season_sim.simulate` answers "how likely are the playoffs". This league pays
for more than the playoffs: $750/$400/$250 for the top three, $300 for the
most regular-season points, $50 for each week's high score (13 of 14 weeks -
which one is unpaid is an open question for the commissioner), $50 to the
consolation winner, and a $50 fine for last place. A lineup or waiver
decision moves several of those at once, so this plays the rest of the
regular season out week by week, seeds BOTH brackets, prices every outcome,
and - by conditioning each trial on this week's result - says what a win
this week is worth in dollars.

Scores are sampled from the same gamma model as season_sim, one draw per
team per week, shared between the matchup and the week's high score so the
two stay consistent.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Sequence

from src.analytics.season_sim import Matchup, TeamSeason


@dataclass
class NextGame:
    """This week's matchup, priced."""

    opponent: str
    p_win: float
    ev_if_win: float
    ev_if_loss: float
    p_high_score: float

    @property
    def win_value(self) -> float:
        return round(self.ev_if_win - self.ev_if_loss, 2)


@dataclass
class PayoutOdds:
    team_key: str
    name: str
    p_finish: dict[int, float] = field(default_factory=dict)   # 1, 2, 3
    p_playoffs: float = 0.0
    p_most_points: float = 0.0
    p_last: float = 0.0
    p_consolation_win: float = 0.0
    expected_high_scores: float = 0.0
    expected_dollars: float = 0.0
    next_game: NextGame | None = None

    def describe(self) -> str:
        return (
            f"{self.name:<22} ${self.expected_dollars:7.0f}  "
            f"1st {self.p_finish.get(1, 0):5.1%}  2nd {self.p_finish.get(2, 0):5.1%}  "
            f"3rd {self.p_finish.get(3, 0):5.1%}  most pts {self.p_most_points:5.1%}  "
            f"high scores {self.expected_high_scores:4.1f}  last {self.p_last:5.1%}"
        )


def _bracket_placings(seeds: Sequence[TeamSeason], rng: random.Random, reseed: bool) -> list[TeamSeason]:
    """[champion, runner-up, third] for a seeded bracket with byes.

    Same structure as season_sim._play_bracket, keeping the losers: the two
    semifinal losers play for third, as Yahoo's six-team bracket does.
    """
    field_ = list(seeds)
    if not field_:
        return []
    if len(field_) == 1:
        return field_
    semifinal_losers: list[TeamSeason] = []
    final_loser: TeamSeason | None = None
    while len(field_) > 1:
        target = 1 << (len(field_) - 1).bit_length() >> 1
        playing = 2 * (len(field_) - target)
        byes = field_[: len(field_) - playing]
        contest = field_[len(field_) - playing:]
        winners, losers = [], []
        for i in range(len(contest) // 2):
            high, low = contest[i], contest[len(contest) - 1 - i]
            if high.sample(rng) >= low.sample(rng):
                winners.append(high)
                losers.append(low)
            else:
                winners.append(low)
                losers.append(high)
        if len(field_) == 4:
            semifinal_losers = losers
        if len(field_) == 2:
            final_loser = losers[0]
        if reseed:
            order = {t.team_key: i for i, t in enumerate(seeds)}
            field_ = sorted(byes + winners, key=lambda t: order[t.team_key])
        else:
            field_ = byes + winners
    out = [field_[0]]
    if final_loser is not None:
        out.append(final_loser)
    if len(semifinal_losers) == 2:
        a, b = semifinal_losers
        out.append(a if a.sample(rng) >= b.sample(rng) else b)
    return out


def simulate_payouts(
    teams: Sequence[TeamSeason],
    remaining: Sequence[Matchup],
    payouts: dict[str, Any],
    finish_payouts: dict[int, int],
    playoff_spots: int = 6,
    trials: int = 3_000,
    final_week: int = 14,
    my_team: str | None = None,
    seed: int = 41,
    reseed: bool = False,
) -> list[PayoutOdds]:
    """Expected dollars per team over the rest of the season, and the value
    of `my_team`'s next game. `remaining` covers regular-season weeks only."""
    rng = random.Random(seed)
    index = {t.team_key: t for t in teams}
    keys = [t.team_key for t in teams]
    weeks = sorted({m.week for m in remaining})
    by_week = {w: [m for m in remaining if m.week == w] for w in weeks}
    unpaid_week = payouts.get("unpaid_high_score_week")
    high_pay = float(payouts.get("weekly_high_score", 0))

    totals = {k: {"d": 0.0, "f1": 0, "f2": 0, "f3": 0, "po": 0, "mp": 0, "last": 0,
                  "cons": 0, "hs": 0.0} for k in keys}
    # This week, for my_team: trial dollars split by the result.
    my_first: Matchup | None = None
    if my_team and weeks:
        my_first = next((m for m in by_week[weeks[0]] if my_team in (m.home, m.away)), None)
    cond = {"win": [], "loss": [], "high": 0}

    for _ in range(trials):
        wins = {k: float(index[k].wins) for k in keys}
        points = {k: index[k].points_for for k in keys}
        dollars = dict.fromkeys(keys, 0.0)
        first_result: str | None = None

        for w in weeks:
            scores = {k: index[k].sample(rng) for k in keys}
            for m in by_week[w]:
                if m.home not in scores or m.away not in scores:
                    continue
                hs, as_ = scores[m.home], scores[m.away]
                points[m.home] += hs
                points[m.away] += as_
                if hs > as_:
                    wins[m.home] += 1
                elif as_ > hs:
                    wins[m.away] += 1
                else:
                    wins[m.home] += 0.5
                    wins[m.away] += 0.5
                if m is my_first and my_team:
                    mine, theirs = (hs, as_) if m.home == my_team else (as_, hs)
                    first_result = "win" if mine > theirs else "loss"
            if high_pay and w != unpaid_week:
                best = max(scores, key=lambda k: scores[k])
                dollars[best] += high_pay
                totals[best]["hs"] += 1
                if w == weeks[0] and my_team and best == my_team:
                    cond["high"] += 1

        standings = sorted(keys, key=lambda k: (wins[k], points[k]), reverse=True)
        most = max(keys, key=lambda k: points[k])
        dollars[most] += float(payouts.get("most_points_regular_season", 0))
        totals[most]["mp"] += 1
        last = standings[-1]
        dollars[last] -= float(payouts.get("last_place_penalty", 0))
        totals[last]["last"] += 1

        field_ = [index[k] for k in standings[:playoff_spots]]
        for k in standings[:playoff_spots]:
            totals[k]["po"] += 1
        placings = _bracket_placings(field_, rng, reseed)
        for place, team in enumerate(placings, 1):
            dollars[team.team_key] += float(finish_payouts.get(place, 0))
            totals[team.team_key][f"f{place}"] += 1
        consolation = [index[k] for k in standings[playoff_spots:]]
        if consolation:
            winner = _bracket_placings(consolation, rng, reseed)[0]
            dollars[winner.team_key] += float(payouts.get("consolation_winner", 0))
            totals[winner.team_key]["cons"] += 1

        for k in keys:
            totals[k]["d"] += dollars[k]
        if my_team and first_result:
            cond[first_result].append(dollars[my_team])

    out = []
    for k in keys:
        t = totals[k]
        odds = PayoutOdds(
            team_key=k, name=index[k].name,
            p_finish={1: t["f1"] / trials, 2: t["f2"] / trials, 3: t["f3"] / trials},
            p_playoffs=t["po"] / trials, p_most_points=t["mp"] / trials,
            p_last=t["last"] / trials, p_consolation_win=t["cons"] / trials,
            expected_high_scores=round(t["hs"] / trials, 2),
            expected_dollars=round(t["d"] / trials, 2),
        )
        if k == my_team and my_first is not None:
            n_win, n_loss = len(cond["win"]), len(cond["loss"])
            decided = n_win + n_loss
            odds.next_game = NextGame(
                opponent=my_first.away if my_first.home == my_team else my_first.home,
                p_win=n_win / decided if decided else 0.0,
                ev_if_win=round(sum(cond["win"]) / n_win, 2) if n_win else 0.0,
                ev_if_loss=round(sum(cond["loss"]) / n_loss, 2) if n_loss else 0.0,
                p_high_score=cond["high"] / trials,
            )
        out.append(odds)
    return sorted(out, key=lambda o: -o.expected_dollars)
