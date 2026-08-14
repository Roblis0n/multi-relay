#!/usr/bin/env python3
"""使用官方品牌图片生成 DeepSeek fan-out README SVG。"""

from __future__ import annotations

import base64
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "readme"


def png_data(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


codex = png_data(ASSETS / "brand" / "codex-official.png")
deepseek = png_data(ASSETS / "brand" / "deepseek-official.png")

hero = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="380" viewBox="0 0 1200 380" role="img" aria-labelledby="title desc">
  <title id="title">codex-deepseek-subagent</title>
  <desc id="desc">将 Codex 内置角色路由到 DeepSeek，并验证 8-way fan-out。</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#090C16"/>
      <stop offset="1" stop-color="#111B32"/>
    </linearGradient>
    <linearGradient id="route" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#6C63FF"/>
      <stop offset="1" stop-color="#4D6BFE"/>
    </linearGradient>
    <filter id="soft" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="12"/>
    </filter>
  </defs>
  <rect width="1200" height="420" rx="30" fill="url(#bg)"/>
  <circle cx="1070" cy="80" r="120" fill="#4D6BFE" opacity=".10" filter="url(#soft)"/>
  <path d="M62 71H1138" stroke="#8FA3C8" stroke-opacity=".18"/>

  <g transform="translate(62 47)">
    <text x="0" y="0" fill="#9AAACA" font-size="18" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" letter-spacing="2">NATIVE DEEPSEEK FAN-OUT FOR CODEX</text>
    <text x="0" y="88" fill="#FFFFFF" font-size="58" font-weight="760" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, sans-serif">codex-deepseek</text>
    <text x="0" y="148" fill="#B8C6E4" font-size="31" font-weight="620" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, sans-serif">8-way fan-out</text>
    <text x="0" y="205" fill="#C7D1E8" font-size="22" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, sans-serif">default · worker · explorer</text>
  </g>

  <g transform="translate(648 76)">
    <rect x="0" y="0" width="488" height="272" rx="26" fill="#0B1020" stroke="#2B3B60"/>
    <image href="{codex}" x="26" y="34" width="128" height="128"/>
    <text x="90" y="183" text-anchor="middle" fill="#FFFFFF" font-size="22" font-weight="700" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif">Codex</text>
    <path d="M164 98H310" stroke="url(#route)" stroke-width="5" stroke-linecap="round"/>
    <path d="M298 86L312 98L298 110" fill="none" stroke="#4D6BFE" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
    <rect x="172" y="118" width="130" height="36" rx="18" fill="#151E36" stroke="#34486F"/>
    <text x="237" y="142" text-anchor="middle" fill="#AFC0E0" font-size="16" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">fan-out × 8</text>
    <rect x="340" y="46" width="104" height="104" rx="23" fill="#EEF3FF"/>
    <image href="{deepseek}" x="350" y="56" width="84" height="84"/>
    <text x="392" y="183" text-anchor="middle" fill="#FFFFFF" font-size="22" font-weight="700" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif">DeepSeek</text>
    <path d="M38 221H450" stroke="#26395D"/>
    <circle cx="58" cy="243" r="6" fill="#42D392"/>
    <text x="76" y="249" fill="#C7D1E8" font-size="17" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">deepseek-v4-pro · highest verified effort</text>
  </g>
</svg>
'''

workflow = '''<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="330" viewBox="0 0 1200 330" role="img" aria-labelledby="title desc">
  <title id="title">DeepSeek fan-out 配置和验证流程</title>
  <desc id="desc">管理程序依次完成凭据、模型门禁、事务安装、三路 fan-out 和元数据验收。</desc>
  <rect width="1200" height="330" rx="28" fill="#F5F7FB"/>
  <text x="58" y="68" fill="#0D1730" font-size="36" font-weight="760" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, sans-serif">DeepSeek fan-out 配置与验收</text>
  <text x="58" y="104" fill="#53627C" font-size="19" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">CREDENTIAL → MODEL GATE → INSTALL → 8-WAY FAN-OUT → VERIFY</text>
  <g transform="translate(58 148)" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, sans-serif">
    <g><rect width="178" height="112" rx="20" fill="#FFFFFF" stroke="#D5DCE8"/><text x="24" y="40" fill="#4D6BFE" font-size="18" font-weight="700">01</text><text x="24" y="75" fill="#14203A" font-size="22" font-weight="700">本机凭据</text></g>
    <path d="M190 56H224" stroke="#4D6BFE" stroke-width="3"/><path d="M216 48L226 56L216 64" fill="none" stroke="#4D6BFE" stroke-width="3"/>
    <g transform="translate(238)"><rect width="178" height="112" rx="20" fill="#FFFFFF" stroke="#D5DCE8"/><text x="24" y="40" fill="#4D6BFE" font-size="18" font-weight="700">02</text><text x="24" y="75" fill="#14203A" font-size="22" font-weight="700">模型门禁</text></g>
    <path d="M428 56H462" stroke="#4D6BFE" stroke-width="3"/><path d="M454 48L464 56L454 64" fill="none" stroke="#4D6BFE" stroke-width="3"/>
    <g transform="translate(476)"><rect width="178" height="112" rx="20" fill="#FFFFFF" stroke="#D5DCE8"/><text x="24" y="40" fill="#4D6BFE" font-size="18" font-weight="700">03</text><text x="24" y="75" fill="#14203A" font-size="22" font-weight="700">事务安装</text></g>
    <path d="M666 56H700" stroke="#4D6BFE" stroke-width="3"/><path d="M692 48L702 56L692 64" fill="none" stroke="#4D6BFE" stroke-width="3"/>
    <g transform="translate(714)"><rect width="178" height="112" rx="20" fill="#FFFFFF" stroke="#D5DCE8"/><text x="24" y="40" fill="#4D6BFE" font-size="18" font-weight="700">04</text><text x="24" y="75" fill="#14203A" font-size="22" font-weight="700">三路 fan-out</text></g>
    <path d="M904 56H938" stroke="#4D6BFE" stroke-width="3"/><path d="M930 48L940 56L930 64" fill="none" stroke="#4D6BFE" stroke-width="3"/>
    <g transform="translate(952)"><rect width="178" height="112" rx="20" fill="#101A31" stroke="#31476F"/><text x="24" y="40" fill="#42D392" font-size="18" font-weight="700">05</text><text x="24" y="75" fill="#FFFFFF" font-size="22" font-weight="700">原生验收</text></g>
  </g>
</svg>
'''

(ASSETS / "hero.svg").write_text(hero, encoding="utf-8")
(ASSETS / "workflow.svg").write_text(workflow, encoding="utf-8")
