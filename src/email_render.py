"""HTML email rendering.

Email is the one channel that gets read on a phone at 7am without opening
anything, so the summaries need to be legible at a glance rather than a wall of
monospace.

Constraints that shape the markup:

* **Inline styles only.** Gmail strips `<style>` blocks, so every rule is on the
  element. No flexbox or grid either - tables are still the reliable layout
  primitive in mail clients.
* **Light background.** A dark email inverts unpredictably across clients; the
  dashboard is dark, mail is light, and that is fine.
* **A plain-text alternative always ships alongside.** Some clients refuse HTML,
  and a text part also keeps the message out of spam filters.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

# Kept deliberately close to the dashboard so the two feel like one product,
# stepped for a light background.
ACCENT = "#B8791A"
INK = "#111827"
INK_MUTED = "#6B7280"
INK_FAINT = "#9CA3AF"
RULE = "#E5E7EB"
SURFACE = "#FFFFFF"
CANVAS = "#F5F6F8"

POSITIVE = "#15803D"
WARNING = "#B45309"
DANGER = "#B91C1C"

URGENCY_ACCENT = {"high": DANGER, "normal": ACCENT, "low": INK_MUTED}

# NO QUOTES in these stacks. They are interpolated into style attributes that
# are sometimes single-quoted and sometimes double-quoted, so either quote
# character would terminate half of them early and silently drop the rest of the
# declaration - which is exactly how this rendered as serif the first time. CSS
# permits unquoted multi-word family names, so the Segoe family is spelled bare.
FONT = "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


@dataclass
class Section:
    title: str | None
    lines: list[str]


def split_sections(lines: list[str]) -> list[Section]:
    """Turn the flat notification lines into titled blocks.

    The jobs mark a heading as `__WAIVER CLAIMS__`; everything until the next
    heading belongs to it.
    """
    sections: list[Section] = []
    current = Section(None, [])
    for raw in lines:
        heading = re.fullmatch(r"__(.+)__", raw.strip())
        if heading:
            if current.lines or current.title:
                sections.append(current)
            current = Section(heading.group(1).title(), [])
        else:
            current.lines.append(raw)
    if current.lines or current.title:
        sections.append(current)
    return [s for s in sections if any(line.strip() for line in s.lines) or s.title]


def _line_html(line: str) -> str:
    """Render one report line, keeping its indentation and colouring markers."""
    stripped = line.strip()
    if not stripped:
        return "<div style='height:8px;line-height:8px;'>&nbsp;</div>"

    indent = len(line) - len(line.lstrip(" "))
    escaped = html.escape(stripped)

    color, weight = INK, "400"
    if stripped.startswith("+"):
        color = POSITIVE
    elif stripped.startswith("!"):
        color = WARNING
    elif stripped.startswith(("ADD ", "START ")) or re.match(r"^\d+\.", stripped):
        weight = "600"

    return (
        f"<div style='margin:0 0 4px {indent * 8}px;color:{color};font-weight:{weight};"
        f"font-size:14px;line-height:1.5;font-family:{FONT};'>{escaped}</div>"
    )


def render(
    title: str,
    lines: list[str],
    *,
    urgency: str = "normal",
    footer_note: str | None = None,
    dashboard_url: str | None = None,
    subtitle: str | None = None,
) -> str:
    accent = URGENCY_ACCENT.get(urgency, ACCENT)
    sections = split_sections(lines)

    body_blocks = []
    for section in sections:
        if section.title:
            body_blocks.append(
                f"<div style='margin:22px 0 8px;font-size:11px;font-weight:700;"
                f"letter-spacing:0.09em;text-transform:uppercase;color:{INK_MUTED};"
                f"font-family:{FONT};'>{html.escape(section.title)}</div>"
                f"<div style='height:1px;background:{RULE};margin-bottom:10px;'></div>"
            )
        body_blocks.extend(_line_html(line) for line in section.lines)

    button = ""
    if dashboard_url:
        button = (
            f"<tr><td style='padding:22px 28px 0;'>"
            f"<a href='{html.escape(dashboard_url)}' "
            f"style='display:inline-block;background:{accent};color:#FFFFFF;"
            f"text-decoration:none;padding:10px 18px;border-radius:6px;"
            f"font-weight:600;font-size:14px;font-family:{FONT};'>"
            f"Open the dashboard</a></td></tr>"
        )

    note = ""
    if footer_note:
        note = (
            f"<div style='margin-top:6px;color:{INK_FAINT};font-size:12px;"
            f"font-family:{FONT};'>{html.escape(footer_note)}</div>"
        )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light only">
</head>
<body style="margin:0;padding:24px 12px;background:{CANVAS};">
<table role="presentation" cellpadding="0" cellspacing="0" border="0"
       style="max-width:640px;margin:0 auto;background:{SURFACE};border-radius:12px;
              border:1px solid {RULE};overflow:hidden;">
  <tr><td style="height:4px;background:{accent};line-height:4px;">&nbsp;</td></tr>
  <tr><td style="padding:24px 28px 0;">
    <div style="font-size:11px;font-weight:700;letter-spacing:0.1em;
                text-transform:uppercase;color:{INK_FAINT};font-family:{FONT};">
      Fantasy Command Center
    </div>
    <div style="margin-top:6px;font-size:20px;font-weight:700;color:{INK};
                line-height:1.3;font-family:{FONT};">{html.escape(title)}</div>
    {f'<div style="margin-top:4px;color:{INK_MUTED};font-size:14px;font-family:{FONT};">{html.escape(subtitle)}</div>' if subtitle else ''}
  </td></tr>
  <tr><td style="padding:14px 28px 0;">{''.join(body_blocks)}</td></tr>
  {button}
  <tr><td style="padding:24px 28px 26px;">
    <div style="height:1px;background:{RULE};margin-bottom:12px;"></div>
    <div style="color:{INK_FAINT};font-size:12px;line-height:1.5;font-family:{FONT};">
      This is advice, not an action. Nothing has been changed in your Yahoo team —
      make any moves there yourself.
    </div>
    {note}
  </td></tr>
</table>
</body>
</html>"""


def render_text(title: str, lines: list[str], dashboard_url: str | None = None) -> str:
    """Plain-text alternative, sent as the fallback part of every message."""
    out = [title, "=" * min(len(title), 60), ""]
    for line in lines:
        out.append(re.sub(r"^__(.+)__$", lambda m: m.group(1).upper(), line.strip())
                   if line.strip().startswith("__") else line)
    out.append("")
    if dashboard_url:
        out.append(f"Dashboard: {dashboard_url}")
    out.append("")
    out.append(
        "This is advice, not an action. Nothing has been changed in your Yahoo team."
    )
    return "\n".join(out)
