#!/usr/bin/env python3
"""验证 ImageGen 主图并生成 DeepSeek fan-out 工作流 SVG。"""

from __future__ import annotations

import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "readme"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def validate_generated_hero(path: Path) -> tuple[int, int]:
    """确保不可重建的 ImageGen 主图存在且适合作为宽幅 README 头图。"""
    header = path.read_bytes()[:24]
    if len(header) != 24 or header[:8] != PNG_SIGNATURE:
        raise ValueError(f"README hero is not a valid PNG: {path}")

    width, height = struct.unpack(">II", header[16:24])
    if width < 1200:
        raise ValueError(f"README hero must be at least 1200 px wide: {width}")
    if width / height < 2.5:
        raise ValueError(f"README hero must be panoramic: {width}x{height}")
    return width, height


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


def main() -> None:
    validate_generated_hero(ASSETS / "hero.png")
    (ASSETS / "workflow.svg").write_text(workflow, encoding="utf-8")


if __name__ == "__main__":
    main()
