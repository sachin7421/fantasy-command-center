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
* **One accent colour (Jets green) means "action"** and is used nowhere else, so the
  Draft button is never ambiguous.
* **Numbers are tabular-figure aligned** so columns of points and VORP compare
  vertically at a glance.
"""
from __future__ import annotations

from string import Template

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
# Re-tuned 7 Oct 2026 for the light surface (#F6F7F9 / #FFFFFF): the same
# five hue families, deepened until each clears 4.5:1 for text on both
# surfaces (RB 4.8, WR 4.8, QB 6.6, TE 4.6, DEF 5.6). Order unchanged.
POSITION_HUES: dict[str, str] = {
    "RB":  "#2563EB",   # blue
    "WR":  "#C2410C",   # orange
    "QB":  "#6D28D9",   # violet
    "TE":  "#A16207",   # yellow
    "DEF": "#BE185D",   # magenta
    "K":   "#15803D",   # green (unused in this league)
}
POSITION_FALLBACK_HUE = "#5B6472"

#: Ink colours. Text always wears these, never a series hue - the coloured field
#: beside the text carries identity instead. On the light surface: 15.4:1,
#: 5.6:1, and a faint step for decoration only (2.9:1, never for text).
INK = "#1B1F24"
INK_MUTED = "#5B6472"
INK_FAINT = "#8A94A6"

# Tier ramp. Tier is ORDINAL, not categorical, so this is a single hue stepped
# light -> dark rather than a rainbow: a multi-hue tier scale implies the tiers
# are different kinds of thing rather than degrees of the same thing.
#
# Six evenly spaced steps of one blue, validated on the dark surface for monotone
# lightness, visible gaps between adjacent steps (>= 0.06 L), and a dark end that
# still clears 2:1 against the background. A seventh step fails the gap check, so
# tier 7 and beyond - all deep bench - share the darkest step.
# Reversed for the light surface: tier 1 is the deepest blue, tier 6+ the
# palest, so "more important" still reads as "more ink".
TIER_COLORS = [
    "#1E3A8A",  # tier 1 - elite
    "#1D4ED8",  # tier 2
    "#2563EB",  # tier 3
    "#60A5FA",  # tier 4
    "#93C5FD",  # tier 5
    "#BFDBFE",  # tier 6+
]

# Semantic colours, all >= 4.7:1 on the light surface.
POSITIVE = "#15803D"
WARNING = "#B45309"
DANGER = "#B91C1C"
MUTED = "#5B6472"

# Accents. ACCENT (bright blue, 4.8:1) means "action" and the selected state
# and is used for nothing else. ACCENT_FILL (orange) is a highlight fill for
# bars and rules; at 2.6:1 it never carries text. GOTHAM_GREEN is kept only
# as the brand mark in the sidebar.
GOTHAM_GREEN = "#125740"
ACCENT = "#2563EB"
ACCENT_BRIGHT = "#1D4ED8"
ACCENT_FILL = "#F97316"


def _rgb(hex_color: str) -> str:
    """"#2E9E6B" -> "46,158,107", so rgba() shares one source of truth with hex."""
    h = hex_color.lstrip("#")
    return ",".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))


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


def stat(label: str, value: str, color: str = INK) -> str:
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
        f"<div style='flex:1;height:5px;background:rgba(91,100,114,0.18);"
        f"border-radius:3px;overflow:hidden;'>"
        f"<div style='width:{pct * 100:.0f}%;height:100%;background:{color};'></div></div>"
        f"<span style='color:{color};font-size:0.75rem;font-weight:650;"
        f"font-variant-numeric:tabular-nums;'>{pct:.0%}</span></div>"
    )


_CSS = Template("""
<style>
  /* The default top padding clipped the first row of metrics, so the header
     had its labels cut off at the viewport edge. */
  .block-container {
    padding-top: 4.2rem;
    padding-bottom: 3rem;
    max-width: 1500px;
  }
  #MainMenu, footer { visibility: hidden; }
  /* Transparent, but NOT zero-height: that container also holds the sidebar
     expand control, and collapsing it left no way to reopen the sidebar - which
     is where the Draft/Season switch lives. The padding-top above is what
     actually fixed the clipped metric labels. */
  header[data-testid="stHeader"] { background: transparent; }

  /* Numbers should line up vertically for comparison. */
  [data-testid="stMetricValue"], .stDataFrame { font-variant-numeric: tabular-nums; }

  [data-testid="stMetricValue"] { font-size: 1.85rem; font-weight: 700; }
  [data-testid="stMetricLabel"] {
    text-transform: uppercase; letter-spacing: 0.07em;
    font-size: 0.68rem; color: #5B6472;
  }

  [data-testid="stSidebar"] { border-right: 1px solid rgba(27,31,36,0.08); }

  .fcc-card {
    border: 1px solid rgba(27,31,36,0.10);
    border-left-width: 3px;
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 10px;
    background: #FFFFFF;
    box-shadow: 0 1px 2px rgba(27,31,36,0.04);
    transition: border-color 120ms ease, box-shadow 120ms ease;
  }
  .fcc-card:hover {
    box-shadow: 0 2px 8px rgba(27,31,36,0.08);
    border-color: rgba($ACCENT_RGB,0.45);
  }
  .fcc-name { font-size: 1.02rem; font-weight: 700; letter-spacing: -0.01em; }
  .fcc-rank {
    color: #8A94A6; font-size: 0.8rem; font-weight: 700;
    font-variant-numeric: tabular-nums; margin-right: 6px;
  }
  .fcc-reason { color: #15803D; font-size: 0.78rem; margin-top: 3px; }
  .fcc-warn   { color: #B45309; font-size: 0.78rem; margin-top: 3px; }

  .fcc-section {
    text-transform: uppercase; letter-spacing: 0.08em;
    font-size: 0.7rem; color: #5B6472; font-weight: 700;
    margin: 14px 0 8px 0;
  }

  /* On the clock: the one moment the page should shout. */
  .fcc-clock {
    background: linear-gradient(90deg, rgba($ACCENT_RGB,0.14), rgba($ACCENT_RGB,0.02));
    border-left: 3px solid $ACCENT;
    padding: 10px 14px; border-radius: 8px; margin-bottom: 14px;
    font-weight: 650;
  }

  .fcc-slot {
    display:inline-block; padding:3px 9px; border-radius:6px; margin:2px 4px 2px 0;
    font-size:0.76rem; font-weight:650; font-variant-numeric: tabular-nums;
  }
  .fcc-slot-filled { background: rgba($ACCENT_RGB,0.12); color:#1D4ED8; }
  .fcc-slot-open   { background: rgba(180,83,9,0.12); color:#B45309; }

  /* Blue means action, and nothing else uses it. */
  .stButton button[kind="primary"] {
    background: $ACCENT; color: #FFFFFF; border: none; font-weight: 700;
  }
  .stButton button[kind="primary"]:hover { background: $ACCENT_BRIGHT; color: #FFFFFF; }

  .fcc-brand {
    font-weight: 800; letter-spacing: -0.02em; font-size: 1.05rem;
    color: #1B1F24; border-left: 4px solid $ACCENT; padding-left: 9px;
    margin-bottom: 2px;
  }
  .fcc-brand-sub {
    color: #5B6472; font-size: 0.68rem; letter-spacing: 0.12em;
    text-transform: uppercase; padding-left: 13px;
  }

  /* Stat tiles for the season pages: a label, a big number, a note. */
  .fcc-tile {
    background: #FFFFFF; border: 1px solid rgba(27,31,36,0.10); border-radius: 12px;
    padding: 12px 14px 10px 14px; min-height: 86px;
    box-shadow: 0 1px 2px rgba(27,31,36,0.04);
  }
  .fcc-tile-label {
    text-transform: uppercase; letter-spacing: 0.07em; font-size: 0.66rem;
    color: #5B6472; font-weight: 700;
  }
  .fcc-tile-value {
    font-size: 1.6rem; font-weight: 800; letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums; color: #1B1F24; line-height: 1.15; margin-top: 2px;
  }
  .fcc-tile-note { font-size: 0.74rem; color: #5B6472; margin-top: 2px; }
  .fcc-good { color: #15803D; } .fcc-bad { color: #B91C1C; } .fcc-warnc { color: #B45309; }
</style>
""")

CSS = _CSS.substitute(
    ACCENT=ACCENT,
    ACCENT_BRIGHT=ACCENT_BRIGHT,
    ACCENT_RGB=_rgb(ACCENT),
    GREEN_RGB=_rgb(GOTHAM_GREEN),
)


def inject_css() -> None:
    import streamlit as st

    st.markdown(CSS, unsafe_allow_html=True)
