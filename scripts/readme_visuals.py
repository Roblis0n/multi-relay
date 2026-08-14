#!/usr/bin/env python3
"""Build the deterministic Signal Relay Console artwork used by the README."""

from __future__ import annotations

import struct
from pathlib import Path
from xml.sax.saxutils import escape


PALETTE = {
    "ink": "#08111f",
    "panel": "#0d1a2d",
    "text": "#edf3fb",
    "muted": "#8495ad",
    "route": "#4d6bfe",
    "verified": "#42d392",
    "warning": "#f2b84b",
    "failure": "#ef6a72",
}

ASSET_SPECS = {
    "hero.svg": (1800, 620),
    "architecture.svg": (1600, 780),
    "workflow.svg": (1600, 460),
    "social-preview.svg": (1280, 640),
    "relay-mark.svg": (512, 512),
}

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif"
MONO = "'Cascadia Mono', 'SFMono-Regular', Consolas, Menlo, monospace"


def _document(
    width: int,
    height: int,
    title: str,
    description: str,
    body: str,
    *,
    defs: str = "",
) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
  <title id="title">{escape(title)}</title>
  <desc id="desc">{escape(description)}</desc>
  <defs>
    <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
      <path d="M32 0H0V32" fill="none" stroke="{PALETTE["muted"]}" stroke-width="1" opacity=".05"/>
    </pattern>
    <radialGradient id="relayGlow">
      <stop offset="0" stop-color="{PALETTE["route"]}" stop-opacity=".10"/>
      <stop offset="1" stop-color="{PALETTE["route"]}" stop-opacity="0"/>
    </radialGradient>
    <marker id="arrowRoute" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="strokeWidth">
      <path d="M0 0L10 5L0 10Z" fill="{PALETTE["route"]}"/>
    </marker>
    <marker id="arrowMuted" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="strokeWidth">
      <path d="M0 0L10 5L0 10Z" fill="{PALETTE["muted"]}"/>
    </marker>
    <marker id="arrowWarning" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="strokeWidth">
      <path d="M0 0L10 5L0 10Z" fill="{PALETTE["warning"]}"/>
    </marker>
{defs}
  </defs>
  {body}
</svg>
"""


def _text(
    x: int | float,
    y: int | float,
    value: str,
    *,
    size: int = 18,
    color: str | None = None,
    weight: int = 500,
    family: str = SANS,
    anchor: str = "start",
    opacity: float | None = None,
    spacing: float | None = None,
) -> str:
    attributes = [
        f'x="{x}"',
        f'y="{y}"',
        f'fill="{color or PALETTE["text"]}"',
        f'font-size="{size}"',
        f'font-weight="{weight}"',
        f'font-family="{family}"',
        f'text-anchor="{anchor}"',
    ]
    if opacity is not None:
        attributes.append(f'opacity="{opacity:g}"')
    if spacing is not None:
        attributes.append(f'letter-spacing="{spacing:g}"')
    return f'<text {" ".join(attributes)}>{escape(value)}</text>'


def _chip(
    x: int,
    y: int,
    width: int,
    label: str,
    *,
    color: str | None = None,
) -> str:
    ink = color or PALETTE["route"]
    return "\n".join(
        (
            f'<g transform="translate({x} {y})">',
            f'  <rect width="{width}" height="34" rx="17" fill="{ink}" opacity=".12" stroke="{ink}" stroke-opacity=".45"/>',
            f'  <circle cx="18" cy="17" r="4" fill="{ink}"/>',
            f'  {_text(31, 22, label, size=13, color=ink, weight=700, family=MONO, spacing=1.1)}',
            "</g>",
        )
    )


def _relay_gate(
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    id_label: str,
    frame: bool = True,
) -> str:
    center_y = height / 2
    rail_x = width * 0.58
    output_x = width - 10
    top = 24
    bottom = height - 24
    step = (bottom - top) / 7
    parts = [
        f'<g transform="translate({x} {y})" data-component="relay-mark" aria-label="{escape(id_label)}">'
    ]
    if frame:
        parts.append(
            f'  <rect x="18" y="10" width="{width - 36}" height="{height - 20}" rx="18" fill="{PALETTE["ink"]}" stroke="{PALETTE["route"]}" stroke-width="2"/>'
        )
    parts.extend(
        (
            f'  <path d="M0 {center_y:g}H{rail_x:g}" fill="none" stroke="{PALETTE["route"]}" stroke-width="6" stroke-linecap="round"/>',
            f'  <path d="M{rail_x:g} {top:g}V{bottom:g}" fill="none" stroke="{PALETTE["route"]}" stroke-width="6" stroke-linecap="round"/>',
        )
    )
    for index in range(8):
        output_y = top + index * step
        parts.append(
            f'  <path d="M{rail_x:g} {output_y:g}H{output_x:g}" fill="none" stroke="{PALETTE["route"]}" stroke-width="4" stroke-linecap="round"/>'
        )
        parts.append(
            f'  <circle cx="{output_x:g}" cy="{output_y:g}" r="5" fill="{PALETTE["verified"]}"/>'
        )
    parts.append(
        f'  <circle cx="0" cy="{center_y:g}" r="6" fill="{PALETTE["route"]}"/>'
    )
    parts.append("</g>")
    return "\n".join(parts)


def _child_terminal(
    x: int,
    y: int,
    index: int,
    role: str,
    *,
    width: int = 186,
    compact: bool = False,
) -> str:
    height = 34 if compact else 42
    text_y = 22 if compact else 27
    return "\n".join(
        (
            f'<g transform="translate({x} {y})" data-node="child" data-role="{role}">',
            f'  <rect width="{width}" height="{height}" rx="{height // 2}" fill="{PALETTE["panel"]}" stroke="{PALETTE["muted"]}" stroke-opacity=".28"/>',
            f'  <circle cx="18" cy="{height / 2:g}" r="5" fill="{PALETTE["verified"]}"/>',
            f'  {_text(33, text_y, f"{index:02d}  {role}", size=13 if compact else 15, family=MONO, color=PALETTE["text"], weight=650, spacing=.4)}',
            "</g>",
        )
    )


def _panel(
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    radius: int = 22,
    dashed: bool = False,
) -> str:
    dash = ' stroke-dasharray="8 9"' if dashed else ""
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="{radius}" '
        f'fill="{PALETTE["panel"]}" stroke="{PALETTE["muted"]}" '
        f'stroke-opacity=".26"{dash}/>'
    )


def hero_svg() -> str:
    roles = ("default", "default", "worker", "worker", "worker", "explorer", "explorer", "explorer")
    child_y = (124, 174, 224, 274, 324, 374, 424, 474)
    children: list[str] = []
    branches: list[str] = []
    for index, (role, y) in enumerate(zip(roles, child_y), start=1):
        center = y + 17
        branches.append(
            f'<path d="M1490 {center}H1520" fill="none" stroke="{PALETTE["route"]}" stroke-width="2.5" opacity=".72"/>'
        )
        children.append(_child_terminal(1520, y, index, role, compact=True))

    body = "\n".join(
        (
            f'<rect width="1800" height="620" rx="30" fill="{PALETTE["ink"]}"/>',
            f'<rect x="1" y="1" width="1798" height="618" rx="29" fill="none" stroke="{PALETTE["muted"]}" stroke-opacity=".22"/>',
            _chip(72, 58, 232, "LOCAL • AUDITED • NATIVE", color=PALETTE["verified"]),
            _text(72, 158, "Codex DeepSeek", size=56, weight=740, spacing=-1.2),
            _text(72, 222, "Relay", size=72, color=PALETTE["route"], weight=780, spacing=-1.5),
            _text(74, 275, "Native DeepSeek fan-out for Codex.", size=23, color=PALETTE["text"], weight=580),
            _text(74, 309, "One protected handoff. Eight verified child slots.", size=19, color=PALETTE["muted"], family=MONO),
            f'<path d="M74 360H674" stroke="{PALETTE["muted"]}" stroke-opacity=".22"/>',
            f'<circle cx="82" cy="400" r="5" fill="{PALETTE["route"]}"/>',
            _text(104, 407, "Parent model stays unchanged", size=16, color=PALETTE["text"], family=MONO),
            f'<circle cx="82" cy="444" r="5" fill="{PALETTE["route"]}"/>',
            _text(104, 451, "Exact task text is handed off visibly", size=16, color=PALETTE["text"], family=MONO),
            f'<circle cx="82" cy="488" r="5" fill="{PALETTE["verified"]}"/>',
            _text(104, 495, "Any failed gate rolls back cleanly", size=16, color=PALETTE["text"], family=MONO),
            _text(74, 558, "codex-deepseek-relay", size=14, color=PALETTE["muted"], family=MONO, spacing=1.4),
            _panel(900, 44, 828, 532, radius=26),
            '<rect x="900" y="44" width="828" height="532" rx="26" fill="url(#grid)"/>',
            _text(940, 82, "SIGNAL MAP // READY", size=14, color=PALETTE["verified"], weight=700, family=MONO, spacing=1.3),
            _text(1688, 82, "NATIVE FAN-OUT ×8", size=14, color=PALETTE["route"], weight=700, family=MONO, anchor="end", spacing=1.0),
            '<circle cx="1306" cy="309" r="158" fill="url(#relayGlow)"/>',
            '<g transform="translate(944 257)">',
            f'  <rect width="178" height="104" rx="18" fill="{PALETTE["ink"]}" stroke="{PALETTE["muted"]}" stroke-opacity=".35"/>',
            f'  {_text(22, 36, "CODEX HOST", size=17, weight=720, family=MONO, spacing=.8)}',
            f'  {_text(22, 65, "PARENT MODEL", size=13, color=PALETTE["muted"], family=MONO, spacing=.8)}',
            f'  <circle cx="150" cy="52" r="10" fill="{PALETTE["route"]}" opacity=".18"/>',
            f'  <circle cx="150" cy="52" r="4" fill="{PALETTE["route"]}"/>',
            "</g>",
            f'<path d="M1122 309H1213" fill="none" stroke="{PALETTE["route"]}" stroke-width="4" marker-end="url(#arrowRoute)"/>',
            _text(1168, 286, "AUDITED HANDOFF", size=12, color=PALETTE["route"], weight=700, family=MONO, anchor="middle", spacing=.8),
            _relay_gate(1230, 220, 150, 178, id_label="Relay core with one input and eight outputs"),
            _text(1305, 427, "LOCAL LOOPBACK", size=13, color=PALETTE["text"], weight=700, family=MONO, anchor="middle", spacing=.9),
            _text(1305, 451, "127.0.0.1:42137", size=12, color=PALETTE["muted"], family=MONO, anchor="middle", spacing=.4),
            f'<path d="M1380 309H1490M1490 141V491" fill="none" stroke="{PALETTE["route"]}" stroke-width="3" stroke-linecap="round"/>',
            *branches,
            *children,
            _text(940, 548, "RESPONSES → RELAY → CHAT COMPLETIONS", size=12, color=PALETTE["muted"], family=MONO, spacing=.7),
            _text(1688, 548, "VERIFIED", size=12, color=PALETTE["verified"], weight=700, family=MONO, anchor="end", spacing=1.2),
        )
    )
    return _document(
        1800,
        620,
        "Codex DeepSeek Relay",
        "A Codex parent task crosses an audited local relay and fans out to eight verified DeepSeek child slots.",
        body,
    )


def architecture_svg() -> str:
    roles = ("default", "worker", "explorer", "worker", "explorer", "default", "worker", "explorer")
    positions = (
        (1190, 198),
        (1385, 198),
        (1190, 250),
        (1385, 250),
        (1190, 302),
        (1385, 302),
        (1190, 354),
        (1385, 354),
    )
    children = [
        _child_terminal(x, y, index, role, width=174, compact=True)
        for index, ((x, y), role) in enumerate(zip(positions, roles), start=1)
    ]
    body = "\n".join(
        (
            f'<rect width="1600" height="780" rx="30" fill="{PALETTE["ink"]}"/>',
            '<rect x="0" y="0" width="1600" height="780" rx="30" fill="url(#grid)"/>',
            _text(58, 68, "How the relay actually works", size=38, weight=760, spacing=-.6),
            _text(58, 103, "Protected dispatch in. Native tools and safe progress out.", size=17, color=PALETTE["muted"], family=MONO),
            _chip(1287, 48, 255, "LOCAL BOUNDARY • FAIL CLOSED", color=PALETTE["verified"]),
            f'<rect x="356" y="136" width="770" height="366" rx="28" fill="{PALETTE["panel"]}" fill-opacity=".42" stroke="{PALETTE["verified"]}" stroke-opacity=".32" stroke-dasharray="9 10"/>',
            _text(382, 166, "LOCAL-ONLY SECURITY BOUNDARY", size=12, color=PALETTE["verified"], weight=700, family=MONO, spacing=1),
            _panel(54, 186, 260, 250),
            _text(78, 220, "01", size=15, color=PALETTE["route"], weight=760, family=MONO),
            _text(78, 258, "CODEX PARENT", size=22, weight=740, family=MONO, spacing=.4),
            _text(78, 293, "Main task", size=17, color=PALETTE["text"], weight=620),
            _text(78, 322, "OpenAI model unchanged", size=14, color=PALETTE["muted"], family=MONO),
            f'<path d="M78 352H288" stroke="{PALETTE["muted"]}" stroke-opacity=".22"/>',
            _text(78, 384, "spawn_agent", size=14, color=PALETTE["route"], family=MONO),
            _text(78, 410, "agent_type is explicit", size=13, color=PALETTE["muted"], family=MONO),
            _panel(390, 186, 286, 250),
            _text(414, 220, "02", size=15, color=PALETTE["route"], weight=760, family=MONO),
            _text(414, 258, "VISIBLE HANDOFF", size=22, weight=740, family=MONO, spacing=.2),
            f'<rect x="414" y="282" width="238" height="86" rx="12" fill="{PALETTE["ink"]}" stroke="{PALETTE["route"]}" stroke-opacity=".55"/>',
            _text(430, 308, "[DeepSeek task: target]", size=12, color=PALETTE["route"], weight=700, family=MONO),
            _text(430, 334, "exact complete task text", size=12, color=PALETTE["text"], family=MONO),
            _text(430, 354, "[/DeepSeek task: target]", size=12, color=PALETTE["route"], weight=700, family=MONO),
            _text(414, 402, "Missing or mismatched → reject", size=13, color=PALETTE["muted"], family=MONO),
            _panel(724, 186, 354, 250),
            _text(748, 220, "03", size=15, color=PALETTE["route"], weight=760, family=MONO),
            _text(748, 258, "LOCAL RELAY", size=22, weight=740, family=MONO, spacing=.4),
            _relay_gate(758, 281, 122, 126, id_label="Local relay protocol bridge"),
            _text(906, 304, "127.0.0.1:42137", size=13, color=PALETTE["verified"], weight=700, family=MONO),
            _text(906, 334, "Responses", size=13, color=PALETTE["text"], family=MONO),
            _text(906, 356, "↓ protocol bridge", size=12, color=PALETTE["muted"], family=MONO),
            _text(906, 380, "Chat Completions", size=13, color=PALETTE["text"], family=MONO),
            _text(906, 410, "loopback only", size=12, color=PALETTE["muted"], family=MONO),
            _panel(1148, 136, 410, 366),
            _text(1176, 174, "04  DEEPSEEK CHILDREN ×8", size=19, weight=740, family=MONO, spacing=.3),
            *children,
            _text(1190, 430, "default / worker / explorer", size=13, color=PALETTE["muted"], family=MONO),
            _text(1190, 458, "deepseek-v4-pro • verified effort", size=12, color=PALETTE["verified"], family=MONO),
            f'<path d="M314 310H374" fill="none" stroke="{PALETTE["route"]}" stroke-width="3" marker-end="url(#arrowRoute)"/>',
            f'<path d="M676 310H708" fill="none" stroke="{PALETTE["route"]}" stroke-width="3" marker-end="url(#arrowRoute)"/>',
            f'<path d="M1078 310H1132" fill="none" stroke="{PALETTE["route"]}" stroke-width="3" marker-end="url(#arrowRoute)"/>',
            _text(330, 294, "PROTECTED", size=10, color=PALETTE["route"], family=MONO),
            _text(1085, 294, "FAN-OUT", size=10, color=PALETTE["route"], family=MONO),
            f'<path d="M1510 540V608H110V454" fill="none" stroke="{PALETTE["muted"]}" stroke-width="2.5" marker-end="url(#arrowMuted)"/>',
            f'<circle cx="1320" cy="608" r="5" fill="{PALETTE["route"]}"/>',
            f'<circle cx="922" cy="608" r="5" fill="{PALETTE["route"]}"/>',
            f'<circle cx="512" cy="608" r="5" fill="{PALETTE["verified"]}"/>',
            _text(1320, 588, "TOOL CALLS", size=12, color=PALETTE["route"], weight=700, family=MONO, anchor="middle", spacing=.8),
            _text(922, 588, "TOOL RESULTS", size=12, color=PALETTE["route"], weight=700, family=MONO, anchor="middle", spacing=.8),
            _text(512, 588, "SAFE PROGRESS", size=12, color=PALETTE["verified"], weight=700, family=MONO, anchor="middle", spacing=.8),
            _text(58, 686, "PRIVATE REASONING", size=12, color=PALETTE["muted"], weight=700, family=MONO, spacing=1),
            _text(58, 716, "Sealed only for continuation; never shown as a fabricated transcript.", size=16, color=PALETTE["text"], family=MONO),
            _text(1542, 724, "codex-deepseek-relay", size=13, color=PALETTE["muted"], family=MONO, anchor="end", spacing=1.1),
        )
    )
    return _document(
        1600,
        780,
        "Codex DeepSeek Relay architecture",
        "Four stages show the Codex parent, visible task handoff, local loopback relay, and eight DeepSeek child slots with tool results returning safely.",
        body,
    )


def workflow_svg() -> str:
    steps = (
        ("01", "CREDENTIAL", "system keychain", "NO KEY IN LOGS"),
        ("02", "MODEL PROBE", "deepseek-v4-pro", "HIGHEST EFFORT"),
        ("03", "TRANSACTION", "atomic install", "BACKUP FIRST"),
        ("04", "NATIVE FAN-OUT", "default • worker • explorer", "8-WAY FAN-OUT"),
        ("05", "VERIFY", "real Codex acceptance", "READY"),
    )
    cards: list[str] = []
    arrows: list[str] = []
    x_positions = (54, 360, 666, 972, 1278)
    for index, ((number, title, subtitle, footer), x) in enumerate(
        zip(steps, x_positions)
    ):
        final = index == 4
        border = PALETTE["verified"] if final else PALETTE["muted"]
        title_color = PALETTE["verified"] if final else PALETTE["text"]
        cards.extend(
            (
                f'<g transform="translate({x} 164)">',
                f'  <rect width="260" height="164" rx="20" fill="{PALETTE["panel"]}" stroke="{border}" stroke-opacity="{".72" if final else ".28"}"/>',
                f'  {_text(22, 34, number, size=14, color=PALETTE["route"], weight=760, family=MONO)}',
                f'  {_text(22, 72, title, size=20, color=title_color, weight=740, family=MONO, spacing=.3)}',
                f'  {_text(22, 104, subtitle, size=13, color=PALETTE["muted"], family=MONO)}',
                f'  <path d="M22 122H238" stroke="{PALETTE["muted"]}" stroke-opacity=".20"/>',
                f'  {_text(22, 148, footer, size=11, color=PALETTE["verified"] if final else PALETTE["route"], weight=700, family=MONO, spacing=.8)}',
                "</g>",
            )
        )
        if index < 4:
            arrows.append(
                f'<path d="M{x + 260} 246H{x + 292}" fill="none" stroke="{PALETTE["route"]}" stroke-width="3" marker-end="url(#arrowRoute)"/>'
            )
    body = "\n".join(
        (
            f'<rect width="1600" height="460" rx="30" fill="{PALETTE["ink"]}"/>',
            '<rect width="1600" height="460" rx="30" fill="url(#grid)"/>',
            _text(54, 62, "Configure once. Prove every boundary.", size=36, weight=760, spacing=-.5),
            _text(54, 98, "A guarded install path with an automatic way back.", size=16, color=PALETTE["muted"], family=MONO),
            _chip(1260, 48, 284, "TRANSACTIONAL • VERIFIED", color=PALETTE["verified"]),
            *cards,
            *arrows,
            f'<path data-route="rollback" d="M1408 328V348" fill="none" stroke="{PALETTE["warning"]}" stroke-width="2.5" stroke-dasharray="7 7" marker-end="url(#arrowWarning)"/>',
            '<g transform="translate(1278 354)" data-component="rollback-card">',
            f'  <rect width="260" height="58" rx="16" fill="{PALETTE["warning"]}" fill-opacity=".08" stroke="{PALETTE["warning"]}" stroke-opacity=".50"/>',
            f'  {_text(20, 23, "ANY FAILURE", size=10, color=PALETTE["warning"], weight=700, family=MONO, spacing=.8)}',
            f'  {_text(20, 46, "ROLLBACK", size=15, color=PALETTE["warning"], weight=760, family=MONO, spacing=1)}',
            f'  {_text(238, 37, "AUTO RESTORE", size=10, color=PALETTE["muted"], weight=700, family=MONO, anchor="end", spacing=.6)}',
            "</g>",
            _text(54, 404, "CREDENTIAL → MODEL PROBE → TRANSACTION → 8-WAY FAN-OUT → VERIFY", size=13, color=PALETTE["muted"], family=MONO, spacing=.5),
            _text(1546, 438, "codex-deepseek-relay", size=12, color=PALETTE["muted"], family=MONO, anchor="end", spacing=1),
        )
    )
    return _document(
        1600,
        460,
        "Relay setup and verification workflow",
        "Credential storage, DeepSeek model probing, transactional installation, native eight-way fan-out, verification, and automatic rollback.",
        body,
    )


def social_preview_svg() -> str:
    dot_y = (154, 202, 250, 298, 346, 394, 442, 490)
    branches = [
        f'<path d="M1008 {y}H1082" stroke="{PALETTE["route"]}" stroke-width="3" opacity=".72"/><circle cx="1096" cy="{y}" r="8" fill="{PALETTE["verified"]}"/>'
        for y in dot_y
    ]
    body = "\n".join(
        (
            f'<rect width="1280" height="640" fill="{PALETTE["ink"]}"/>',
            '<rect width="1280" height="640" fill="url(#grid)"/>',
            '<circle cx="900" cy="320" r="270" fill="url(#relayGlow)"/>',
            _chip(66, 66, 205, "LOCAL • AUDITED • NATIVE", color=PALETTE["verified"]),
            _text(66, 190, "Codex", size=72, weight=780, spacing=-1.7),
            _text(66, 268, "DeepSeek Relay", size=72, color=PALETTE["route"], weight=780, spacing=-1.8),
            _text(68, 326, "Native DeepSeek fan-out for Codex.", size=24, weight=600),
            _text(68, 365, "One protected handoff. Eight verified child slots.", size=17, color=PALETTE["muted"], family=MONO),
            _chip(66, 430, 112, "8-WAY", color=PALETTE["route"]),
            _chip(192, 430, 118, "LOCAL", color=PALETTE["route"]),
            _chip(324, 430, 136, "VERIFIED", color=PALETTE["verified"]),
            _text(68, 566, "codex-deepseek-relay", size=14, color=PALETTE["muted"], family=MONO, spacing=1.3),
            f'<rect x="694" y="82" width="520" height="476" rx="28" fill="{PALETTE["panel"]}" stroke="{PALETTE["muted"]}" stroke-opacity=".28"/>',
            '<rect x="694" y="82" width="520" height="476" rx="28" fill="url(#grid)"/>',
            _text(724, 120, "SIGNAL MAP // ×8", size=13, color=PALETTE["route"], weight=700, family=MONO, spacing=1),
            f'<rect x="724" y="276" width="128" height="88" rx="16" fill="{PALETTE["ink"]}" stroke="{PALETTE["muted"]}" stroke-opacity=".35"/>',
            _text(788, 311, "CODEX", size=15, weight=740, family=MONO, anchor="middle", spacing=.8),
            _text(788, 337, "PARENT", size=12, color=PALETTE["muted"], family=MONO, anchor="middle", spacing=.8),
            f'<path d="M852 320H886" stroke="{PALETTE["route"]}" stroke-width="4" marker-end="url(#arrowRoute)"/>',
            _relay_gate(900, 182, 108, 276, id_label="Relay mark with eight verified outputs"),
            f'<path d="M1008 320H1042M1042 154V490" stroke="{PALETTE["route"]}" stroke-width="3" fill="none"/>',
            *branches,
            _text(1168, 524, "VERIFIED", size=12, color=PALETTE["verified"], weight=700, family=MONO, anchor="end", spacing=1),
        )
    )
    return _document(
        1280,
        640,
        "Codex DeepSeek Relay social preview",
        "Repository social card showing one Codex parent routed through a local relay to eight DeepSeek child slots.",
        body,
    )


def relay_mark_svg() -> str:
    body = "\n".join(
        (
            f'<rect width="512" height="512" rx="104" fill="{PALETTE["ink"]}"/>',
            f'<rect x="18" y="18" width="476" height="476" rx="88" fill="{PALETTE["panel"]}" stroke="{PALETTE["muted"]}" stroke-opacity=".24"/>',
            '<circle cx="264" cy="256" r="212" fill="url(#relayGlow)"/>',
            _relay_gate(76, 92, 356, 328, id_label="Codex DeepSeek Relay project mark"),
        )
    )
    return _document(
        512,
        512,
        "Codex DeepSeek Relay mark",
        "A project-owned relay gate with one incoming route and eight verified outputs.",
        body,
    )


def write_svg_assets(output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    builders = {
        "hero.svg": hero_svg,
        "architecture.svg": architecture_svg,
        "workflow.svg": workflow_svg,
        "social-preview.svg": social_preview_svg,
        "relay-mark.svg": relay_mark_svg,
    }
    written: dict[str, Path] = {}
    for name, builder in builders.items():
        path = output_dir / name
        path.write_text(builder(), encoding="utf-8", newline="\n")
        written[name] = path
    return written


def read_png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != PNG_SIGNATURE:
        raise ValueError(f"README asset is not a valid PNG: {path}")
    return struct.unpack(">II", header[16:24])


def validate_png(path: Path, expected: tuple[int, int]) -> None:
    width, height = read_png_size(path)
    if (width, height) != expected:
        expected_width, expected_height = expected
        raise ValueError(
            f"README asset expected {expected_width}x{expected_height}, "
            f"got {width}x{height}: {path}"
        )
