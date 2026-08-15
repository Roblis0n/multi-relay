from __future__ import annotations

import re
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.readme_visuals import (
    ASSET_SPECS,
    PALETTE,
    read_png_size,
    validate_png,
    write_svg_assets,
)


EXPECTED_SPECS = {
    "hero.svg": (1800, 620),
    "architecture.svg": (1600, 780),
    "workflow.svg": (1600, 460),
    "social-preview.svg": (1280, 640),
    "relay-mark.svg": (512, 512),
}

EXPECTED_PALETTE = {
    "ink": "#08111f",
    "panel": "#0d1a2d",
    "text": "#edf3fb",
    "muted": "#8495ad",
    "route": "#4d6bfe",
    "verified": "#42d392",
    "warning": "#f2b84b",
    "failure": "#ef6a72",
}

def elements_with(root: ET.Element, key: str, value: str) -> list[ET.Element]:
    return [node for node in root.iter() if node.attrib.get(key) == value]


def visible_text(root: ET.Element) -> str:
    return " ".join(part.strip() for part in root.itertext() if part.strip())


def png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", checksum)
    )


def png_file(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(b""))
        + png_chunk(b"IEND", b"")
    )


class ReadmeVisualTests(unittest.TestCase):
    def test_asset_contract_is_exact(self) -> None:
        self.assertEqual(ASSET_SPECS, EXPECTED_SPECS)
        self.assertEqual(PALETTE, EXPECTED_PALETTE)

    def test_generated_svgs_are_valid_deterministic_and_correctly_sized(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = write_svg_assets(Path(first_dir))
            second = write_svg_assets(Path(second_dir))

            self.assertEqual(set(first), set(EXPECTED_SPECS))
            self.assertEqual(set(second), set(EXPECTED_SPECS))
            for name, (width, height) in EXPECTED_SPECS.items():
                first_bytes = first[name].read_bytes()
                self.assertEqual(first_bytes, second[name].read_bytes())
                for line in first_bytes.splitlines():
                    self.assertEqual(line, line.rstrip(), name)
                root = ET.fromstring(first_bytes)
                self.assertEqual(root.attrib["width"], str(width))
                self.assertEqual(root.attrib["height"], str(height))
                self.assertEqual(
                    root.attrib["viewBox"], f"0 0 {width} {height}"
                )

    def test_committed_svgs_exactly_match_the_generator(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            generated = write_svg_assets(Path(output_dir))
            for name, generated_path in generated.items():
                committed_path = ROOT / "assets" / "readme" / name
                self.assertEqual(
                    committed_path.read_bytes(), generated_path.read_bytes(), name
                )

    def test_asset_specs_are_enforced_by_the_generator(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            with mock.patch.dict(ASSET_SPECS, {"hero.svg": (1, 1)}):
                with self.assertRaisesRegex(ValueError, "hero.svg"):
                    write_svg_assets(Path(output_dir))

    def test_warning_and_failure_colors_keep_their_semantic_scope(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            paths = write_svg_assets(Path(output_dir))
            for name, path in paths.items():
                svg = path.read_text(encoding="utf-8")
                if name == "workflow.svg":
                    self.assertIn(PALETTE["warning"], svg)
                else:
                    self.assertNotIn(PALETTE["warning"], svg, name)
                self.assertNotIn(PALETTE["failure"], svg, name)

    def test_hero_and_architecture_show_exactly_eight_role_slots(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            paths = write_svg_assets(Path(output_dir))
            for name in ("hero.svg", "architecture.svg"):
                root = ET.parse(paths[name]).getroot()
                children = elements_with(root, "data-node", "child")
                self.assertEqual(len(children), 8, name)
                roles = {node.attrib["data-role"] for node in children}
                self.assertEqual(roles, {"default", "worker", "explorer", "reviewer"})

    def test_generated_assets_explain_the_relay_contract(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            paths = write_svg_assets(Path(output_dir))
            hero_text = visible_text(ET.parse(paths["hero.svg"]).getroot())
            architecture_text = visible_text(
                ET.parse(paths["architecture.svg"]).getroot()
            )
            workflow_text = visible_text(
                ET.parse(paths["workflow.svg"]).getroot()
            )

            for label in (
                "AUDITED HANDOFF",
                "CAPABILITY ROUTING",
                "UP TO 8 CHILDREN",
                "default",
                "worker",
                "explorer",
                "reviewer",
            ):
                self.assertIn(label, hero_text)
            for label in (
                "CODEX PARENT",
                "VISIBLE HANDOFF",
                "LOCAL RELAY",
                "MODEL PROVIDERS",
                "NATIVE CODEX",
                "RESPONSES API",
                "CHAT COMPLETIONS",
                "TOOL CALLS",
                "SAFE PROGRESS",
            ):
                self.assertIn(label, architecture_text)
            for label in ("VERIFY", "ROLLBACK", "MODEL PROBE", "TRANSACTION"):
                self.assertIn(label, workflow_text)
            self.assertIn("system credential store", workflow_text)
            self.assertNotIn("system keychain", workflow_text)
            for boundary in ("VISION", "AUDIO", "WEB", "HIGH-RISK"):
                self.assertIn(boundary, workflow_text)

    def test_generated_svgs_never_embed_vendor_images(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            paths = write_svg_assets(Path(output_dir))
            for name, path in paths.items():
                root = ET.parse(path).getroot()
                local_names = {
                    node.tag.rsplit("}", 1)[-1].lower() for node in root.iter()
                }
                self.assertNotIn("image", local_names, name)
                self.assertNotIn("data:image", path.read_text(encoding="utf-8"))

    def test_rollback_route_stops_before_the_rollback_card(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            workflow = write_svg_assets(Path(output_dir))["workflow.svg"]
            root = ET.parse(workflow).getroot()
            routes = elements_with(root, "data-route", "rollback")
            cards = elements_with(root, "data-component", "rollback-card")
            self.assertEqual(len(routes), 1)
            self.assertEqual(len(cards), 1)

            route_match = re.fullmatch(
                r"M\d+ (?P<start>\d+)V(?P<end>\d+)", routes[0].attrib["d"]
            )
            card_match = re.fullmatch(
                r"translate\(\d+ (?P<top>\d+)\)", cards[0].attrib["transform"]
            )
            self.assertIsNotNone(route_match)
            self.assertIsNotNone(card_match)
            assert route_match is not None
            assert card_match is not None
            self.assertLess(
                int(route_match.group("end")), int(card_match.group("top"))
            )

    def test_png_dimension_reader_and_validator(self) -> None:
        with tempfile.TemporaryDirectory() as output_dir:
            path = Path(output_dir) / "asset.png"
            valid_png = png_file(1800, 620)
            path.write_bytes(valid_png)

            self.assertEqual(read_png_size(path), (1800, 620))
            validate_png(path, (1800, 620))
            with self.assertRaisesRegex(ValueError, "expected 1280x640"):
                validate_png(path, (1280, 640))

            path.write_bytes(b"not a png")
            with self.assertRaisesRegex(ValueError, "valid PNG"):
                read_png_size(path)

            corrupted_cases = {
                "truncated": valid_png[:24],
                "missing-iend": valid_png[:-12],
                "bad-crc": valid_png[:-1] + bytes((valid_png[-1] ^ 1,)),
                "wrong-first-chunk": (
                    b"\x89PNG\r\n\x1a\n"
                    + b"\x00\x00\x00\rXXXX"
                    + struct.pack(">II", 1800, 620)
                ),
            }
            for case, payload in corrupted_cases.items():
                with self.subTest(case=case):
                    path.write_bytes(payload)
                    with self.assertRaisesRegex(ValueError, "valid PNG"):
                        read_png_size(path)


if __name__ == "__main__":
    unittest.main()
