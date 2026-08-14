#!/usr/bin/env python3
"""Generate deterministic README SVGs and validate checked-in PNG exports."""

from __future__ import annotations

from pathlib import Path

try:
    from scripts.readme_visuals import validate_png, write_svg_assets
except ModuleNotFoundError:
    from readme_visuals import validate_png, write_svg_assets


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "readme"


def main() -> None:
    write_svg_assets(ASSETS)
    validate_png(ASSETS / "hero.png", (1800, 620))
    validate_png(ASSETS / "social-preview.png", (1280, 640))


if __name__ == "__main__":
    main()
