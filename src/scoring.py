"""League-specific scoring engine.

Converts a raw stat line into fantasy points using the league's *actual* Yahoo
scoring rules (spec 2.3, 4.2). Nothing about scoring is hardcoded: the stat
categories and their point modifiers are both pulled from Yahoo and stored.

The only static table here is `ALIASES`, which normalizes Yahoo's human-readable
stat *names* onto canonical keys so that projection sources (which speak in
their own field names) can be scored by the same engine.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Iterable

# --- canonical stat vocabulary ----------------------------------------------
# Every projection adapter emits stat lines keyed by these names.

# Yahoo reuses names across position types ("Int" and "Sack" mean different
# things for an offense and a defense), so aliases are keyed by
# (position_type, normalized_name) and fall back to name-only.
# Yahoo position_type: "O" offense, "K" kicker, "DT" defense/special teams.
ALIASES: dict[tuple[str, str], str] = {}


def _alias(names: str | Iterable[str], canonical: str, pos_type: str = "*") -> None:
    if isinstance(names, str):
        names = [names]
    for n in names:
        ALIASES[(pos_type, _norm(n))] = canonical


def _norm(name: str) -> str:
    """Normalize a stat name for matching: lowercase, collapse punctuation."""
    s = name.lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9+\- ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Passing
_alias(["pass att", "passing attempts", "attempts"], "pass_att", "O")
_alias(["pass comp", "completions", "passing completions"], "pass_cmp", "O")
_alias(["inc", "incomplete passes", "incompletions"], "pass_inc", "O")
_alias(["pass yds", "passing yards"], "pass_yds", "O")
_alias(["pass td", "passing touchdowns"], "pass_td", "O")
_alias(["int", "interceptions"], "pass_int", "O")
_alias(["sack", "sacked", "times sacked"], "pass_sacked", "O")
_alias(["pass 1st downs", "passing first downs"], "pass_fd", "O")
# Rushing
_alias(["rush att", "rushing attempts"], "rush_att", "O")
_alias(["rush yds", "rushing yards"], "rush_yds", "O")
_alias(["rush td", "rushing touchdowns"], "rush_td", "O")
_alias(["rush 1st downs", "rushing first downs"], "rush_fd", "O")
# Receiving
_alias(["targets", "tgt"], "rec_tgt", "O")
_alias(["rec", "receptions"], "rec", "O")
_alias(["rec yds", "receiving yards"], "rec_yds", "O")
_alias(["rec td", "receiving touchdowns"], "rec_td", "O")
_alias(["rec 1st downs", "receiving first downs"], "rec_fd", "O")
# Misc offense
_alias(["ret yds", "return yards"], "ret_yds", "O")
_alias(["ret td", "return touchdowns"], "ret_td", "O")
_alias(["2-pt", "2 pt", "2-point conversions", "two point conversions"], "two_pt", "O")
_alias(["fum", "fumbles"], "fum", "O")
_alias(["fum lost", "fumbles lost"], "fum_lost", "O")
_alias(["fum ret td", "fumbles return td", "fumble return td"], "fum_ret_td", "O")
_alias(["off fum ret td", "offensive fumble return td"], "off_fum_ret_td", "O")
# Kicking
_alias(["fg made", "field goals made"], "fg_made", "K")
_alias(["fg att", "field goals attempted"], "fg_att", "K")
_alias(["fg miss", "field goals missed"], "fg_miss", "K")
_alias(["pat made", "point after attempt made", "extra points made"], "pat_made", "K")
_alias(["pat miss", "point after attempt missed", "extra points missed"], "pat_miss", "K")
# Defense / special teams
_alias(["sack", "sacks"], "def_sack", "DT")
_alias(["int", "interceptions"], "def_int", "DT")
_alias(["fum rec", "fumble recovery", "fumbles recovered"], "def_fum_rec", "DT")
_alias(["td", "touchdowns", "def td", "defensive touchdowns"], "def_td", "DT")
_alias(["safe", "safety", "safeties"], "def_safety", "DT")
_alias(["blk kick", "blocked kick", "blocked kicks"], "def_blk_kick", "DT")
_alias(["ret td", "return td", "kick and punt return touchdowns"], "def_ret_td", "DT")
_alias(["xpr", "extra point returned"], "def_xpr", "DT")
_alias(["pts allow", "points allowed"], "def_pts_allowed", "DT")
_alias(["yds allow", "yards allowed"], "def_yds_allowed", "DT")
_alias(["4 dwn stops", "fourth down stops"], "def_4th_down_stops", "DT")
_alias(["3 and outs", "three and outs"], "def_3_and_outs", "DT")
_alias(["tfl", "tackles for loss"], "def_tfl", "DT")

# Range-bucket categories award a flat amount when a measured value falls in a
# range (e.g. "Pts Allow 7-13"). Maps the bucket family to the stat it reads.
BUCKET_BASES: dict[str, str] = {
    "def_pts_allowed": "def_pts_allowed",
    "def_yds_allowed": "def_yds_allowed",
}

# Field-goal distance buckets are *counts*, not ranges - each is its own stat.
FG_DISTANCE_KEYS = ["fg_0_19", "fg_20_29", "fg_30_39", "fg_40_49", "fg_50p"]

# When a source only gives total FGs made, split them across distances using a
# league-average distribution. Flagged in the projection detail so it is visible.
FG_DISTANCE_PRIOR = {
    "fg_0_19": 0.04, "fg_20_29": 0.26, "fg_30_39": 0.29,
    "fg_40_49": 0.26, "fg_50p": 0.15,
}

_RANGE_RE = re.compile(r"(\d+)\s*(?:-|to|–)\s*(\d+)")
_PLUS_RE = re.compile(r"(\d+)\s*\+")
_EXACT_RE = re.compile(r"(?:^|\s)(\d+)(?:\s|$)")


@dataclass
class StatCategory:
    """One Yahoo scoring category, plus how to evaluate it."""

    stat_id: int
    name: str
    display_name: str
    position_type: str
    canonical: str | None = None
    modifier: float = 0.0
    enabled: bool = True
    display_only: bool = False
    # Set for range-bucket categories such as "Pts Allow 14-20".
    bucket_base: str | None = None
    bucket_low: float | None = None
    bucket_high: float | None = None

    @property
    def is_bucket(self) -> bool:
        return self.bucket_base is not None

    def matches_bucket(self, value: float) -> bool:
        """Whether `value` falls in this bracket.

        Yahoo's brackets are integers and adjacent: 0, 1-6, 7-13, 14-20, 21-27,
        28-34, 35+. Projections are not integers - Sleeper's defensive lines
        carry values like 16.5 or 20.5 - so a literal `low <= value <= high`
        leaves a gap between every pair of brackets, and a value landing in one
        matched NOTHING and scored zero with no error. Points allowed is the
        largest single component of a defence's score, so every DST projection
        in the system was understated, and `sync_defense_season` summed
        eighteen of them.

        The upper edge is therefore treated as exclusive-to-the-next-bracket:
        anything below `high + 1` belongs here. 20.5 lands in 14-20, 34.6 in
        28-34, exactly as a human reading the table would expect.
        """
        low = self.bucket_low if self.bucket_low is not None else float("-inf")
        if self.bucket_high is None:
            return value >= low
        return low <= value < self.bucket_high + 1


@dataclass
class Bonus:
    """A threshold bonus, e.g. +3 when rushing yards reach 100."""

    canonical: str
    target: float
    points: float


@dataclass
class LeagueScoring:
    """Scoring rules for one league, derived entirely from Yahoo."""

    categories: list[StatCategory] = field(default_factory=list)
    bonuses: list[Bonus] = field(default_factory=list)

    # -- evaluation ----------------------------------------------------------

    def score(self, stat_line: dict[str, float], position_type: str | None = None) -> float:
        """Total fantasy points for a canonical stat line under these rules."""
        return round(sum(self.breakdown(stat_line, position_type).values()), 4)

    def breakdown(
        self, stat_line: dict[str, float], position_type: str | None = None
    ) -> dict[str, float]:
        """Per-category point contributions, for explaining a number to a human."""
        out: dict[str, float] = {}
        line = {k: float(v) for k, v in stat_line.items() if v is not None}

        for cat in self.categories:
            if not cat.enabled or cat.display_only or not cat.canonical:
                continue
            if position_type and cat.position_type not in ("*", position_type):
                continue

            if cat.is_bucket:
                base_value = line.get(cat.bucket_base)
                if base_value is not None and cat.matches_bucket(base_value):
                    out[cat.display_name] = cat.modifier
                continue

            value = line.get(cat.canonical)
            if value:
                out[cat.display_name] = round(value * cat.modifier, 4)

        for bonus in self.bonuses:
            value = line.get(bonus.canonical)
            if value is not None and value >= bonus.target:
                key = f"Bonus: {bonus.canonical} >= {bonus.target:g}"
                out[key] = out.get(key, 0.0) + bonus.points

        return {k: v for k, v in out.items() if v}

    # -- introspection used by the rest of the system ------------------------

    def modifier_for(self, canonical: str) -> float | None:
        for cat in self.categories:
            if cat.canonical == canonical and cat.enabled and not cat.display_only:
                return cat.modifier
        return None

    @property
    def is_ppr(self) -> bool:
        return bool(self.modifier_for("rec"))

    @property
    def ppr_value(self) -> float:
        return self.modifier_for("rec") or 0.0

    def active_canonicals(self) -> set[str]:
        return {
            c.canonical for c in self.categories
            if c.canonical and c.enabled and not c.display_only
        }

    # -- (de)serialization ---------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {
                "categories": [c.__dict__ for c in self.categories],
                "bonuses": [b.__dict__ for b in self.bonuses],
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> LeagueScoring:
        data = json.loads(raw)
        return cls(
            categories=[StatCategory(**c) for c in data.get("categories", [])],
            bonuses=[Bonus(**b) for b in data.get("bonuses", [])],
        )


# --- building from Yahoo payloads -------------------------------------------

def resolve_canonical(name: str, display_name: str, position_type: str) -> str | None:
    """Map a Yahoo stat name onto a canonical key, honoring position type."""
    for candidate in (display_name, name):
        if not candidate:
            continue
        key = _norm(candidate)
        # Strip any trailing range so "Pts Allow 7-13" resolves like "Pts Allow".
        base = _strip_range(key)
        for pt in (position_type, "*"):
            for probe in (key, base):
                hit = ALIASES.get((pt, probe))
                if hit:
                    return hit
    # Field-goal distance buckets are counts; derive their key from the range.
    fg_key = _fg_distance_key(display_name or name)
    if fg_key:
        return fg_key
    return None


def _strip_range(normalized: str) -> str:
    """Reduce a bucket label to its base stat name.

    "pts allow 7-13" -> "pts allow", and equally "points allowed 0 points" ->
    "points allowed", so the shutout bucket resolves like every other bucket.
    Only ever used as a *fallback* after the unmodified name misses, so stats
    that legitimately contain a number (e.g. "2-pt") match before reaching here.
    """
    s = _RANGE_RE.sub(" ", normalized)
    s = _PLUS_RE.sub(" ", s)
    s = re.sub(r"(?:^|\s)\d+(?=\s|$)", " ", s)          # bare counts: "pts allow 0"
    s = re.sub(r"\b(points|point|pts)\b\s*$", " ", s)   # trailing unit word
    return re.sub(r"\s+", " ", s).strip()


def _fg_distance_key(name: str) -> str | None:
    n = _norm(name)
    if not n.startswith(("fg", "field goal")):
        return None
    if _PLUS_RE.search(n):
        low = int(_PLUS_RE.search(n).group(1))
        return "fg_50p" if low >= 50 else f"fg_{low}p"
    m = _RANGE_RE.search(n)
    if m:
        return f"fg_{int(m.group(1))}_{int(m.group(2))}"
    return None


def parse_bucket(name: str, canonical_base: str | None) -> tuple[float | None, float | None] | None:
    """Extract (low, high) for a range-bucket category, else None."""
    if canonical_base not in BUCKET_BASES:
        return None
    n = _norm(name)
    m = _RANGE_RE.search(n)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = _PLUS_RE.search(n)
    if m:
        return float(m.group(1)), None
    m = _EXACT_RE.search(n)
    if m:
        # e.g. "Pts Allow 0" is the shutout bucket.
        v = float(m.group(1))
        return v, v
    return None


def build_from_yahoo(settings: dict[str, Any]) -> LeagueScoring:
    """Build a LeagueScoring from a serialized Yahoo league-settings payload.

    Expects the shape Yahoo returns:
      settings.stat_categories.stats[].stat  -> {stat_id, name, display_name, ...}
      settings.stat_modifiers.stats[].stat   -> {stat_id, value}
      settings.stat_modifiers.bonuses[].bonus-> {target, points, stat_id}
    """
    cats_raw = _dig(settings, ["stat_categories", "stats"]) or []
    mods_raw = _dig(settings, ["stat_modifiers", "stats"]) or []
    bonuses_raw = (
        _dig(settings, ["stat_modifiers", "bonuses"])
        or _dig(settings, ["stat_categories", "bonuses"])
        or []
    )

    modifiers: dict[int, float] = {}
    for entry in mods_raw:
        stat = entry.get("stat", entry) if isinstance(entry, dict) else {}
        sid = _as_int(stat.get("stat_id"))
        if sid is None:
            continue
        modifiers[sid] = _as_float(stat.get("value"), 0.0)

    categories: list[StatCategory] = []
    for entry in cats_raw:
        stat = entry.get("stat", entry) if isinstance(entry, dict) else {}
        sid = _as_int(stat.get("stat_id"))
        if sid is None:
            continue
        name = str(stat.get("name") or "")
        display = str(stat.get("display_name") or name)
        pos_type = str(stat.get("position_type") or "*") or "*"
        canonical = resolve_canonical(name, display, pos_type)
        base = _strip_canonical_base(canonical)
        bucket = parse_bucket(display or name, base)

        categories.append(
            StatCategory(
                stat_id=sid,
                name=name,
                display_name=display,
                position_type=pos_type,
                canonical=canonical,
                modifier=modifiers.get(sid, 0.0),
                enabled=sid in modifiers,
                display_only=_as_bool(stat.get("is_only_display_stat")),
                bucket_base=BUCKET_BASES.get(base) if bucket else None,
                bucket_low=bucket[0] if bucket else None,
                bucket_high=bucket[1] if bucket else None,
            )
        )

    bonuses: list[Bonus] = []
    by_id = {c.stat_id: c for c in categories}
    for entry in bonuses_raw:
        b = entry.get("bonus", entry) if isinstance(entry, dict) else {}
        sid = _as_int(b.get("stat_id"))
        cat = by_id.get(sid) if sid is not None else None
        if cat is None or not cat.canonical:
            continue
        bonuses.append(
            Bonus(
                canonical=cat.canonical,
                target=_as_float(b.get("target"), 0.0),
                points=_as_float(b.get("points"), 0.0),
            )
        )

    return LeagueScoring(categories=categories, bonuses=bonuses)


def _strip_canonical_base(canonical: str | None) -> str | None:
    """Bucket categories resolve to their base stat, e.g. def_pts_allowed."""
    return canonical


def expand_fg_distances(stat_line: dict[str, float]) -> dict[str, float]:
    """Split a bare `fg_made` total across distance buckets when the league
    scores kickers by distance but the source only gave a total."""
    if not stat_line.get("fg_made"):
        return stat_line
    if any(stat_line.get(k) for k in FG_DISTANCE_KEYS):
        return stat_line
    out = dict(stat_line)
    total = float(stat_line["fg_made"])
    for key, share in FG_DISTANCE_PRIOR.items():
        out[key] = total * share
    out["_fg_distance_estimated"] = 1
    return out


# --- small coercion helpers --------------------------------------------------

def _dig(data: Any, path: list[str]) -> Any:
    node = data
    for part in path:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    return str(value).strip() in ("1", "true", "True", "yes")
