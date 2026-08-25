"""Playoff odds, and what a decision is actually worth.

Every commercial tool that has this presents it as a headline number. The number
alone is entertainment; what makes it useful is asking it twice - once as things
stand, once with a change applied - because the difference is the only honest
measure of what a trade or a waiver claim is worth.

    "Adds 4.2 points of projected value" is unfalsifiable puffery.
    "Raises your playoff odds from 41% to 52%" is a decision.

Method: simulate the remaining schedule many times. Each week both teams draw a
score from their own distribution (the same per-player gamma machinery as the
start/sit model, summed to a team level), the wins are tallied, and the final
standings are sorted. Doing it this way rather than with closed-form
approximations picks up the things that actually decide seasons - a strong team
with a brutal remaining schedule, a weak one that only has to beat weak
opponents.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from collections.abc import Sequence


@dataclass
class TeamSeason:
    """One team as the simulator sees it."""

    team_key: str
    name: str
    wins: int
    losses: int
    ties: int = 0
    points_for: float = 0.0
    #: Expected weekly score and its spread.
    mean: float = 100.0
    sd: float = 25.0

    @property
    def games_played(self) -> int:
        return self.wins + self.losses + self.ties

    def sample(self, rng: random.Random) -> float:
        if self.mean <= 0 or self.sd <= 0:
            return max(0.0, self.mean)
        shape = (self.mean / self.sd) ** 2
        scale = self.sd**2 / self.mean
        return rng.gammavariate(shape, scale)


@dataclass
class Matchup:
    week: int
    home: str
    away: str


@dataclass
class Odds:
    team_key: str
    name: str
    playoff_odds: float
    title_odds: float
    mean_wins: float
    mean_seed: float

    def describe(self) -> str:
        return (
            f"{self.name:<22} {self.playoff_odds:5.1%} playoffs  "
            f"{self.title_odds:5.1%} title  "
            f"{self.mean_wins:4.1f} wins  seed {self.mean_seed:.1f}"
        )


def _play_bracket(
    seeds: Sequence[TeamSeason],
    rng: random.Random,
    reseed: bool = False,
) -> TeamSeason | None:
    """A standard seeded bracket with byes for the top seeds.

    The previous version was structurally incoherent. With six spots it paired
    (1,6), (3,4), (5,2) in the first round and then gave the winner of 3-v-4 a
    bye in the second - so the 3 seed needed TWO wins for the title and the 2
    seed needed THREE, and neither of the top two got the bye. `title_odds` was
    not an estimate of anything.

    The real format: teams beyond the nearest power of two play a wild-card
    round and the top seeds sit it out.

    `reseed` decides what happens next, and it matters. With reseeding the best
    surviving team faces the worst each round. Without it - which is what THIS
    league is set to, verified on the settings page - the bracket is fixed once
    the field is known: the 1 seed plays the winner of 4-v-5 however the other
    game goes. The difference is worth real probability to the top seeds, so it
    is read from the league settings rather than assumed.
    """
    field = list(seeds)
    if not field:
        return None

    while len(field) > 1:
        # How many play this round: enough to bring the field to a power of two.
        target = 1 << (len(field) - 1).bit_length() >> 1
        playing = 2 * (len(field) - target)
        byes = field[: len(field) - playing]
        contest = field[len(field) - playing:]

        winners = []
        for i in range(len(contest) // 2):
            high, low = contest[i], contest[len(contest) - 1 - i]
            winners.append(high if high.sample(rng) >= low.sample(rng) else low)

        if reseed:
            # Best surviving team faces the worst next round.
            order = {team.team_key: i for i, team in enumerate(seeds)}
            field = sorted(byes + winners, key=lambda team: order[team.team_key])
        else:
            # Fixed bracket: a winner inherits the slot it played in, so the
            # top seed meets whoever came out of the bottom half.
            field = byes + winners

    return field[0]


def simulate(
    teams: Sequence[TeamSeason],
    remaining: Sequence[Matchup],
    playoff_spots: int = 6,
    trials: int = 5_000,
    seed: int = 41,
    reseed: bool = False,
) -> list[Odds]:
    """Play out the rest of the season repeatedly and count outcomes.

    Ties in the standings break on points for, which is the common rule and the
    one this league uses.
    """
    rng = random.Random(seed)
    index = {t.team_key: t for t in teams}
    made_playoffs = {t.team_key: 0 for t in teams}
    won_title = {t.team_key: 0 for t in teams}
    total_wins = {t.team_key: 0.0 for t in teams}
    total_seed = {t.team_key: 0.0 for t in teams}

    for _ in range(trials):
        wins = {t.team_key: float(t.wins) for t in teams}
        points = {t.team_key: t.points_for for t in teams}

        for matchup in remaining:
            home, away = index.get(matchup.home), index.get(matchup.away)
            if not home or not away:
                continue
            home_score, away_score = home.sample(rng), away.sample(rng)
            points[home.team_key] += home_score
            points[away.team_key] += away_score
            if home_score > away_score:
                wins[home.team_key] += 1
            elif away_score > home_score:
                wins[away.team_key] += 1
            else:
                wins[home.team_key] += 0.5
                wins[away.team_key] += 0.5

        standings = sorted(
            teams, key=lambda t: (wins[t.team_key], points[t.team_key]), reverse=True
        )
        for position, team in enumerate(standings, 1):
            total_wins[team.team_key] += wins[team.team_key]
            total_seed[team.team_key] += position
            if position <= playoff_spots:
                made_playoffs[team.team_key] += 1

        champion = _play_bracket(standings[:playoff_spots], rng, reseed=reseed)
        if champion is not None:
            won_title[champion.team_key] += 1

    return sorted(
        [
            Odds(
                team_key=t.team_key,
                name=t.name,
                playoff_odds=made_playoffs[t.team_key] / trials,
                title_odds=won_title[t.team_key] / trials,
                mean_wins=round(total_wins[t.team_key] / trials, 2),
                mean_seed=round(total_seed[t.team_key] / trials, 2),
            )
            for t in teams
        ],
        key=lambda o: -o.playoff_odds,
    )


def odds_for(results: Sequence[Odds], team_key: str) -> Odds | None:
    return next((o for o in results if o.team_key == team_key), None)


@dataclass
class DecisionImpact:
    """What a change is worth, in the only currency that matters."""

    before: float
    after: float
    label: str = ""
    detail: list[str] = field(default_factory=list)

    @property
    def delta(self) -> float:
        return self.after - self.before

    @property
    def verdict(self) -> str:
        if self.delta >= 0.03:
            return "clearly worth doing"
        if self.delta >= 0.01:
            return "marginally positive"
        if self.delta <= -0.03:
            return "clearly not worth doing"
        if self.delta <= -0.01:
            return "marginally negative"
        return "no meaningful difference"

    def describe(self) -> str:
        return (
            f"{self.label}: playoff odds {self.before:.1%} -> {self.after:.1%} "
            f"({self.delta:+.1%}) - {self.verdict}"
        )


def evaluate_change(
    teams: Sequence[TeamSeason],
    remaining: Sequence[Matchup],
    team_key: str,
    new_mean: float,
    new_sd: float | None = None,
    label: str = "change",
    playoff_spots: int = 6,
    trials: int = 4_000,
) -> DecisionImpact:
    """Playoff odds with and without a change to one team's weekly scoring.

    Both runs use the same seed, so the difference reflects the change rather
    than simulation noise - which at these trial counts would otherwise be the
    same size as the effect being measured.
    """
    baseline = simulate(teams, remaining, playoff_spots, trials, seed=41)

    adjusted = []
    for t in teams:
        if t.team_key == team_key:
            adjusted.append(
                TeamSeason(
                    t.team_key, t.name, t.wins, t.losses, t.ties, t.points_for,
                    new_mean, new_sd if new_sd is not None else t.sd,
                )
            )
        else:
            adjusted.append(t)

    changed = simulate(adjusted, remaining, playoff_spots, trials, seed=41)
    before = odds_for(baseline, team_key)
    after = odds_for(changed, team_key)
    return DecisionImpact(
        before=before.playoff_odds if before else 0.0,
        after=after.playoff_odds if after else 0.0,
        label=label,
        detail=[
            f"projected weekly score {new_mean - (before.mean_wins * 0):.1f}"
        ] if before else [],
    )
