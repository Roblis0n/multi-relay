#!/usr/bin/env python3
"""Generate deterministic README SVGs and optionally rasterize checked-in PNGs."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

try:
    from scripts.readme_visuals import validate_png, write_svg_assets
except ModuleNotFoundError:
    from readme_visuals import validate_png, write_svg_assets


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets" / "readme"


def _browser() -> Path:
    configured = os.environ.get("MULTI_RELAY_BROWSER")
    candidates = [
        configured,
        *(shutil.which(name) for name in ("msedge", "chrome", "chromium", "chromium-browser")),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for value in candidates:
        if value and Path(value).is_file():
            return Path(value)
    raise RuntimeError(
        "PNG rendering needs Chrome, Chromium, or Edge; set MULTI_RELAY_BROWSER."
    )


def _render_png(svg: Path, png: Path, size: tuple[int, int]) -> None:
    width, height = size
    with tempfile.TemporaryDirectory(prefix="multi-relay-readme-") as profile:
        completed = subprocess.run(
            [
                str(_browser()),
                "--headless=new",
                "--disable-background-networking",
                "--disable-breakpad",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-gpu",
                "--hide-scrollbars",
                "--no-first-run",
                "--force-device-scale-factor=1",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                f"--screenshot={png.resolve()}",
                svg.resolve().as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError("README PNG rendering failed.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--render-png",
        action="store_true",
        help="Rasterize hero and social preview from their generated SVG sources.",
    )
    args = parser.parse_args(argv)
    write_svg_assets(ASSETS)
    if args.render_png:
        _render_png(ASSETS / "hero.svg", ASSETS / "hero.png", (1800, 620))
        _render_png(
            ASSETS / "social-preview.svg",
            ASSETS / "social-preview.png",
            (1280, 640),
        )
    validate_png(ASSETS / "hero.png", (1800, 620))
    validate_png(ASSETS / "social-preview.png", (1280, 640))


if __name__ == "__main__":
    main()
