"""Charts for the dashboard.

Only charts that answer a question you would otherwise have to work out in your
head. The board already shows *who* is best; these show *shape* - where value
falls off a cliff, and therefore when a position has to be addressed.

Colour follows the validated categorical palette in src/ui.py (slots assigned
per position, never cycled). Every chart here uses a single y axis; there are no
dual-axis charts anywhere in this project.
"""
from __future__ import annotations

from typing import Sequence

from src.ui import INK_FAINT, INK_MUTED, POSITION_HUES, position_hue

# Recessive chart furniture: the data should be the only assertive thing.
GRID_COLOR = "rgba(148,163,184,0.12)"
AXIS_COLOR = "rgba(148,163,184,0.28)"
SURFACE = "#0E1117"


def _theme(chart, height: int):
    return (
        chart.configure_view(strokeWidth=0, fill=SURFACE)
        .configure_axis(
            grid=True,
            gridColor=GRID_COLOR,
            domainColor=AXIS_COLOR,
            tickColor=AXIS_COLOR,
            labelColor=INK_MUTED,
            titleColor=INK_MUTED,
            labelFontSize=11,
            titleFontSize=11,
            titleFontWeight="normal",
        )
        .configure_legend(
            labelColor=INK_MUTED,
            titleColor=INK_MUTED,
            labelFontSize=11,
            titleFontSize=11,
            symbolStrokeWidth=3,
            orient="top",
            direction="horizontal",
            title=None,
        )
        .properties(height=height)
    )


def value_curve(players: Sequence, depth: int = 48, height: int = 340):
    """VORP against depth at each position - the shape of positional scarcity.

    Reading it: a steep line means waiting is expensive, because the next player
    at that position is much worse. A flat line means the position can wait. The
    zero rule is replacement level, so where a line crosses it is the last
    player at that position who is worth a starting slot.
    """
    import altair as alt
    import pandas as pd

    rows = [
        {
            "Position": p.position,
            "Rank": p.position_rank,
            "VORP": round(p.vorp, 1),
            "Player": p.name,
            "Proj": round(p.points, 1),
        }
        for p in players
        if p.position_rank and p.position_rank <= depth and p.position in POSITION_HUES
    ]
    if not rows:
        return None

    frame = pd.DataFrame(rows)
    present = [p for p in POSITION_HUES if p in set(frame["Position"])]
    scale = alt.Scale(domain=present, range=[position_hue(p) for p in present])

    hover = alt.selection_point(
        fields=["Rank"], nearest=True, on="pointermove", empty=False, clear="pointerout"
    )

    base = alt.Chart(frame).encode(
        x=alt.X("Rank:Q", title="Depth at position", scale=alt.Scale(nice=False)),
        y=alt.Y("VORP:Q", title="Value over replacement"),
        color=alt.Color("Position:N", scale=scale),
    )

    lines = base.mark_line(strokeWidth=2, interpolate="monotone")

    # Replacement level. Every line crossing this is the last startable player
    # at that position, which is the whole point of the chart.
    zero = (
        alt.Chart(pd.DataFrame({"y": [0]}))
        .mark_rule(strokeDash=[4, 4], color=AXIS_COLOR, strokeWidth=1)
        .encode(y="y:Q")
    )

    # Invisible wide targets so the hover hit area is bigger than the 2px mark.
    points = (
        base.mark_point(size=90, opacity=0)
        .add_params(hover)
        .encode(
            tooltip=[
                alt.Tooltip("Player:N"),
                alt.Tooltip("Position:N"),
                alt.Tooltip("Rank:Q", title="Positional rank"),
                alt.Tooltip("Proj:Q", title="Projected pts"),
                alt.Tooltip("VORP:Q"),
            ]
        )
    )
    marks = base.mark_point(size=45, filled=True).transform_filter(hover)

    return _theme(zero + lines + points + marks, height)


def tier_cliff(players: Sequence, position: str, depth: int = 24, height: int = 260):
    """Projected points by depth for one position, coloured by tier.

    Bars rather than a line: at a single position you are comparing individual
    players, and the gaps between tiers are the thing to see.
    """
    import altair as alt
    import pandas as pd

    from src.ui import tier_color

    pool = [
        p for p in players
        if p.position == position and p.position_rank and p.position_rank <= depth
    ]
    if not pool:
        return None

    frame = pd.DataFrame(
        [
            {
                "Player": p.name,
                "Rank": p.position_rank,
                "Points": round(p.points, 1),
                "Tier": int(p.tier or 1),
                "VORP": round(p.vorp, 1),
            }
            for p in pool
        ]
    )
    tiers = sorted(set(frame["Tier"]))
    scale = alt.Scale(domain=tiers, range=[tier_color(t) for t in tiers])

    chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=14)
        .encode(
            x=alt.X("Player:N", sort=alt.SortField("Rank"), title=None,
                    axis=alt.Axis(labelAngle=-40, labelLimit=90)),
            y=alt.Y("Points:Q", title="Projected points"),
            color=alt.Color("Tier:O", scale=scale,
                            legend=alt.Legend(title="Tier", orient="top")),
            tooltip=[
                alt.Tooltip("Player:N"),
                alt.Tooltip("Rank:Q", title="Positional rank"),
                alt.Tooltip("Points:Q", title="Projected pts"),
                alt.Tooltip("VORP:Q"),
                alt.Tooltip("Tier:O"),
            ],
        )
    )
    return _theme(chart, height)


def projection_drift(rows: Sequence[dict], height: int = 280):
    """How a player's projection has moved over the season.

    Reads from `projection_history`, so it only becomes interesting once several
    weeks of observations have accumulated.
    """
    import altair as alt
    import pandas as pd

    if not rows:
        return None
    frame = pd.DataFrame(rows)
    if frame.empty or frame["observed_at"].nunique() < 2:
        return None

    sources = sorted(set(frame["source"]))
    # Reuse the same fixed slot order so a source keeps its colour everywhere.
    slots = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]
    scale = alt.Scale(domain=sources, range=slots[: len(sources)])

    chart = (
        alt.Chart(frame)
        .mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=45, filled=True))
        .encode(
            x=alt.X("observed_at:T", title=None),
            y=alt.Y("points:Q", title="Projected points", scale=alt.Scale(zero=False)),
            color=alt.Color("source:N", scale=scale),
            tooltip=[
                alt.Tooltip("source:N", title="Source"),
                alt.Tooltip("points:Q", title="Projected"),
                alt.Tooltip("observed_at:T", title="Observed"),
            ],
        )
    )
    return _theme(chart, height)
