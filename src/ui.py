"""Visual design system for the dashboard.

The draft board is read under time pressure - a minute-thirty per pick - so the
priorities are, in order: read the top recommendation instantly, see position
and tier without parsing text, and never mistake which control drafts a player.

Choices that follow from that:

* **Position is colour-coded**, because position is the single most common thing
  you scan for. The hues are spaced around the wheel and separated in lightness
  as well, so they stay distinguishable for red-green colour blindness and in
  grayscale.
* **Tier is a left border**, not a colour fill. It reads as a group boundary
  without competing with the position badge for attention.
* **One accent colour (amber) means "action"** and is used nowhere else, so the
  Draft button is never ambiguous.
* **Numbers are tabular-figure aligned** so columns of points and VORP compare
  vertically at a glance.
"""
from __future__ import annotations

# Position hues. These are NOT chosen by eye: they are slots 1-6 of a validated
# categorical theme, assigned in a fixed order and never cycled.
#
# The obvious palette (emerald RB / sky WR) was measured and rejected - those two
# sit only dE 12.8 apart to normal vision, below the 15 floor, and RB and WR are
# precisely the two positions scanned most often on a draft board. This set
# passes every gate on the dark surface: lightness band, chroma floor, CVD
# separation (worst adjacent dE 8.4), normal-vision separation (19.3), and 3:1
# contrast.
#
# Order is the colour-blindness safety mechanism, so do not reshuffle it.
POSITION_HUES: dict[str, str] = {
    "RB":  "#3987e5",   # blue
    "WR":  "#d95926",   # orange
    # QB moved off the theme's aqua slot when the interface went green: an
    # aqua-green position chip beside a green action button is exactly the
    # ambiguity the single-accent rule exists to prevent. Violet is a different
    # slot of the same validated theme, and the swap IMPROVED colour-blind
    # separation (worst adjacent dE 13.2, up from 8.4).
    "QB":  "#9085e9",   # violet
    "TE":  "#c98500",   # yellow
    "DEF": "#d55181",   # magenta
    "K":   "#008300",   # green (unused in this league)
}
POSITION_FALLBACK_HUE = "#94A3B8"

#: Ink colours. Text always wears these, never a series hue - the coloured field
#: beside the text carries identity instead.
INK = "#E8EAED"
INK_MUTED = "#94A3B8"
INK_FAINT = "#64748B"

# Tier ramp. Tier is ORDINAL, not categorical, so this is a single hue stepped
# light -> dark rather than a rainbow: a multi-hue tier scale implies the tiers
# are different kinds of thing rather than degrees of the same thing.
#
# Six evenly spaced steps of one blue, validated on the dark surface for monotone
# lightness, visible gaps between adjacent steps (>= 0.06 L), and a dark end that
# still clears 2:1 against the background. A seventh step fails the gap check, so
# tier 7 and beyond - all deep bench - share the darkest step.
TIER_COLORS = [
    "#cde2fb",  # tier 1 - elite
    "#9ec5f4",  # tier 2
    "#6da7ec",  # tier 3
    "#3987e5",  # tier 4
    "#256abf",  # tier 5
    "#184f95",  # tier 6+
]

# Semantic colours.
POSITIVE = "#4ADE80"
WARNING = "#FBBF24"
DANGER = "#F87171"
MUTED = "#94A3B8"

# Club colours. GOTHAM_GREEN is the official one and is used only behind text -
# at 2.2:1 on this background it cannot carry an interface element on its own.
# ACCENT is the brightened step that does (5.6:1), and it means "action", used
# for nothing else.
GOTHAM_GREEN = "#125740"
ACCENT = "#2E9E6B"
ACCENT_BRIGHT = "#3FBF85"

#: Injury status -> colour. Anything unlisted is treated as healthy.
STATUS_COLORS = {
    "IR": DANGER, "Out": DANGER, "PUP": DANGER, "Suspended": DANGER,
    "Doubtful": "#FB923C", "Questionable": WARNING, "NA": MUTED, "DNR": MUTED,
}


def position_hue(position: str) -> str:
    return POSITION_HUES.get((position or "").upper(), POSITION_FALLBACK_HUE)


def tint(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def position_color(position: str) -> tuple[str, str]:
    """(ink, tinted background) for a position chip."""
    return INK, tint(position_hue(position), 0.22)


def tier_color(tier: int) -> str:
    return TIER_COLORS[min(max(int(tier or 1), 1) - 1, len(TIER_COLORS) - 1)]


def status_color(status: str | None) -> str:
    return STATUS_COLORS.get(status or "", MUTED)


def position_badge(position: str, rank: int | None = None) -> str:
    """A position chip like `RB1`.

    The hue lives in the chip's field and its left rule; the label itself stays
    in primary ink. That keeps the text at full contrast and means identity is
    never carried by colour alone - the position is written on it.
    """
    hue = position_hue(position)
    label = f"{position}{rank}" if rank else position
    return (
        f"<span style='background:{tint(hue, 0.22)};color:{INK};"
        f"border-left:3px solid {hue};padding:2px 8px 2px 6px;border-radius:5px;"
        f"font-weight:650;font-size:0.78rem;letter-spacing:0.02em;"
        f"font-variant-numeric:tabular-nums;'>{label}</span>"
    )


def tier_pill(tier: int) -> str:
    """Tier label for a card.

    Deliberately uncoloured. The tier number already carries the ordering, and a
    coloured tier label sitting beside a coloured position chip invites reading
    one as the other. Colour is reserved for the tier CHART, where it is the only
    encoding available.
    """
    return (
        f"<span style='color:{INK_MUTED};font-weight:650;font-size:0.72rem;"
        f"letter-spacing:0.05em;'>TIER {tier}</span>"
    )


def stat(label: str, value: str, color: str = "#E8EAED") -> str:
    """A small label-over-value pair used inside cards."""
    return (
        f"<div style='display:inline-block;margin-right:16px;'>"
        f"<div style='color:{MUTED};font-size:0.66rem;letter-spacing:0.06em;"
        f"text-transform:uppercase;'>{label}</div>"
        f"<div style='color:{color};font-weight:650;font-size:0.95rem;"
        f"font-variant-numeric:tabular-nums;'>{value}</div></div>"
    )


def survival_bar(probability: float) -> str:
    """A compact bar for P(available at my next pick).

    Colour carries the decision: red means take him now, green means he will
    likely come back to you.
    """
    pct = max(0.0, min(1.0, probability))
    if pct < 0.35:
        color = DANGER
    elif pct < 0.7:
        color = WARNING
    else:
        color = POSITIVE
    return (
        f"<div style='display:flex;align-items:center;gap:8px;'>"
        f"<div style='flex:1;height:5px;background:rgba(148,163,184,0.2);"
        f"border-radius:3px;overflow:hidden;'>"
        f"<div style='width:{pct * 100:.0f}%;height:100%;background:{color};'></div></div>"
        f"<span style='color:{color};font-size:0.75rem;font-weight:650;"
        f"font-variant-numeric:tabular-nums;'>{pct:.0%}</span></div>"
    )


CSS = """
<style>
  /* The default top padding clipped the first row of metrics, so the header
     had its labels cut off at the viewport edge. */
  .block-container {
    padding-top: 4.2rem;
    padding-bottom: 3rem;
    max-width: 1500px;
  }
  #MainMenu, footer { visibility: hidden; }
  header[data-testid="stHeader"] { background: transparent; height: 0; }

  /* Numbers should line up vertically for comparison. */
  [data-testid="stMetricValue"], .stDataFrame { font-variant-numeric: tabular-nums; }

  [data-testid="stMetricValue"] { font-size: 1.85rem; font-weight: 700; }
  [data-testid="stMetricLabel"] {
    text-transform: uppercase; letter-spacing: 0.07em;
    font-size: 0.68rem; color: #94A3B8;
  }

  /* A thin club-green rule under the sidebar brand. */
  [data-testid="stSidebar"] { border-right: 1px solid rgba(46,158,107,0.22); }

  .fcc-card {
    border: 1px solid rgba(148,163,184,0.16);
    border-left-width: 3px;
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 10px;
    background: rgba(21,32,27,0.6);
    transition: border-color 120ms ease, background 120ms ease;
  }
  .fcc-card:hover {
    background: rgba(21,32,27,0.95);
    border-color: rgba(46,158,107,0.4);
  }
  .fcc-name { font-size: 1.02rem; font-weight: 700; letter-spacing: -0.01em; }
  .fcc-rank {
    color: #64748B; font-size: 0.8rem; font-weight: 700;
    font-variant-numeric: tabular-nums; margin-right: 6px;
  }
  .fcc-reason { color: #4ADE80; font-size: 0.78rem; margin-top: 3px; }
  .fcc-warn   { color: #FBBF24; font-size: 0.78rem; margin-top: 3px; }

  .fcc-section {
    text-transform: uppercase; letter-spacing: 0.08em;
    font-size: 0.7rem; color: #94A3B8; font-weight: 700;
    margin: 4px 0 10px 0;
  }

  /* On the clock: the one moment the page should shout, in club green. */
  .fcc-clock {
    background: linear-gradient(90deg, rgba(46,158,107,0.22), rgba(18,87,64,0.04));
    border-left: 3px solid #2E9E6B;
    padding: 10px 14px; border-radius: 8px; margin-bottom: 14px;
    font-weight: 650;
  }

  .fcc-slot {
    display:inline-block; padding:3px 9px; border-radius:6px; margin:2px 4px 2px 0;
    font-size:0.76rem; font-weight:650; font-variant-numeric: tabular-nums;
  }
  .fcc-slot-filled { background: rgba(46,158,107,0.18); color:#6EE7B7; }
  .fcc-slot-open   { background: rgba(251,191,36,0.14); color:#FBBF24; }

  /* Green means action, and nothing else uses it. */
  .stButton button[kind="primary"] {
    background: #2E9E6B; color: #06120C; border: none; font-weight: 700;
  }
  .stButton button[kind="primary"]:hover { background: #3FBF85; color: #06120C; }

  .fcc-brand {
    font-weight: 800; letter-spacing: -0.02em; font-size: 1.05rem;
    color: #E9EDEA; border-left: 4px solid #2E9E6B; padding-left: 9px;
    margin-bottom: 2px;
  }
  .fcc-brand-sub {
    color: #6B8578; font-size: 0.68rem; letter-spacing: 0.12em;
    text-transform: uppercase; padding-left: 13px;
  }
</style>
"""


def inject_css() -> None:
    import streamlit as st

    st.markdown(CSS, unsafe_allow_html=True)
