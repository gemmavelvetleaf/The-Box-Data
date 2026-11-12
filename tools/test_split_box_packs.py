"""Offline regression checks for The Box chunk generator."""
from __future__ import annotations

import os
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from split_box_packs import build, canonicalize_and_validate  # noqa: E402


UUID = "c7a4e9ff-3a9c-aef0-0481-5f14d6ac2fd4"
PANEL = f"{UUID}|<1,1,0>|<0,0,0>|0|<1,1,1>|1|1"
SCENE = "[One]\n" + "\n".join(f"{p}={PANEL}" for p in
                                     ("F", "C", "W1", "W2", "W3", "W4")) + "\n"
GIF = "https://github.com/gemmavelvetleaf/The-Box-Data/blob/main/GIF/People%20Machine.gif"
STREAM = "http://85.215.155.9:86/hls/stream/?controls=false&muted=false&autoplay=true&playsinline=true"


class ChunkGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "packs").mkdir()

    def put(self, name="Pasture", content=SCENE):
        path = self.root / "packs" / (name + ".txt")
        path.write_text(content, encoding="utf-8")
        return path

    def test_old_headers_are_ignored_and_version_is_content_derived(self):
        path = self.put(content="VERSION=forgotten\nPACK=Wrong Name\n\n" + SCENE)
        first, data, count = canonicalize_and_validate(path, path.read_text())
        self.assertEqual(count, 1)
        self.assertIn("PACK=Pasture\n", data)
        self.assertTrue(data.startswith("VERSION=" + first))
        path.write_text("VERSION=another\nPACK=Also Wrong\n\n" + SCENE)
        second, _, _ = canonicalize_and_validate(path, path.read_text())
        self.assertEqual(first, second)

    def test_scene_change_changes_version(self):
        path = self.put()
        first, _, _ = canonicalize_and_validate(path, path.read_text())
        path.write_text(SCENE.replace("|1|1", "|0.9|1"))
        second, _, _ = canonicalize_and_validate(path, path.read_text())
        self.assertNotEqual(first, second)

    def test_invalid_scene_preserves_last_generated_output(self):
        path = self.put()
        build(self.root)
        old = (self.root / "chunks" / "index.txt").read_bytes()
        path.write_text(SCENE.replace("W4=", "BROKEN="))
        with self.assertRaisesRegex(ValueError, "unknown panel"):
            build(self.root)
        self.assertEqual(old, (self.root / "chunks" / "index.txt").read_bytes())

    def test_cli_failure_has_visible_github_annotation_and_summary(self):
        self.put(content=SCENE + "NOT_A_PANEL=" + PANEL + "\n")
        summary = self.root / "step-summary.md"
        env = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary))
        command = [sys.executable,
                   str(Path(__file__).with_name("split_box_packs.py")),
                   "--root", str(self.root)]
        run = subprocess.run(command, text=True, capture_output=True, env=env,
                             check=False)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("NO NEW SCENES WERE PUBLISHED", run.stderr)
        self.assertIn("::error file=packs/Pasture.txt,line=", run.stderr)
        self.assertIn("The Box scene pack update failed", summary.read_text())

    def test_long_scene_name_preserves_canonical_and_unique_aliases(self):
        one = "White Room Wide Window TR"
        two = "White Room Wide Window Transparent"
        path = self.put(content=SCENE.replace("[One]", f"[{one}]") + "\n"
                        + SCENE.replace("[One]", f"[{two}]") + "\n")
        _, generated, count = canonicalize_and_validate(path, path.read_text())
        self.assertEqual(count, 2)
        self.assertIn(f"[{one}]\nALIAS=", generated)
        self.assertIn(f"[{two}]\nALIAS=", generated)
        aliases = [line[6:] for line in generated.splitlines() if line.startswith("ALIAS=")]
        self.assertEqual(len(aliases), 2)
        self.assertNotEqual(aliases[0].casefold(), aliases[1].casefold())
        self.assertTrue(all(len(alias) <= 21 for alias in aliases))

    def test_short_names_stay_unchanged(self):
        path = self.put()
        _, generated, _ = canonicalize_and_validate(path, path.read_text())
        self.assertNotIn("ALIAS=", generated)

    def test_duplicate_name_rejected(self):
        path = self.put(content=SCENE + "\n" + SCENE)
        with self.assertRaisesRegex(ValueError, "duplicate scene name"):
            canonicalize_and_validate(path, path.read_text())

    def test_invalid_uuid_rejected(self):
        path = self.put(content=SCENE.replace(UUID, "no-texture"))
        with self.assertRaisesRegex(ValueError, "invalid texture UUID"):
            canonicalize_and_validate(path, path.read_text())

    def test_graphics_and_outside_tags_can_share_panel(self):
        source = SCENE + "O1=" + PANEL + "\nUW1=" + GIF + "\nUO1=" + STREAM + "\n"
        path = self.put(content=source)
        _, generated, count = canonicalize_and_validate(path, path.read_text())
        self.assertEqual(count, 1)
        self.assertIn("UW1=" + GIF, generated)
        self.assertIn("UO1=" + STREAM, generated)

    def test_non_github_gif_is_accepted(self):
        path = self.put(content=SCENE + "UW1=https://example.com/photo.gif\n")
        canonicalize_and_validate(path, path.read_text())

    def test_bad_graphics_scheme_is_rejected(self):
        path = self.put(content=SCENE + "UW1=javascript:alert(1)\n")
        with self.assertRaisesRegex(ValueError, r"HTTP\(S\)"):
            canonicalize_and_validate(path, path.read_text())

    def test_duplicate_graphics_panel_is_rejected(self):
        path = self.put(content=SCENE + "UW1=" + GIF + "\nUW1=" + GIF + "\n")
        with self.assertRaisesRegex(ValueError, "duplicate graphics panel"):
            canonicalize_and_validate(path, path.read_text())

    def test_all_six_short_gif_tags_are_valid(self):
        tags = ("UF", "UC", "UW1", "UW2", "UW3", "UW4")
        path = self.put(content=SCENE + "".join(f"{tag}={GIF}\n" for tag in tags))
        _, generated, count = canonicalize_and_validate(path, path.read_text())
        self.assertEqual(count, 1)
        for tag in tags:
            self.assertIn(f"{tag}={GIF}", generated)

    def test_old_long_gif_tag_is_rejected(self):
        path = self.put(content=SCENE + "GW1=" + GIF + "\n")
        with self.assertRaisesRegex(ValueError, "unknown panel"):
            canonicalize_and_validate(path, path.read_text())

    def test_bare_live_stream_url_with_player_options(self):
        path = self.put(content=SCENE + "UW1=" + STREAM + "\n")
        _, generated, _ = canonicalize_and_validate(path, path.read_text())
        self.assertIn("UW1=" + STREAM, generated)

    def test_tiled_gif_and_query_string(self):
        path = self.put(content=SCENE + "UW1=" + GIF + "|4|4\n"
                        + "UO2=https://example.net/tiles/animated.gif?cache=7|2|3\n")
        _, generated, _ = canonicalize_and_validate(path, path.read_text())
        self.assertIn("UW1=" + GIF + "|4|4", generated)

    def test_tiling_must_be_positive_bounded_and_gif(self):
        for suffix in ("|0|4", "|33|4", "|4|no", "|4", "|4|4|4"):
            with self.subTest(suffix=suffix):
                path = self.put(content=SCENE + "UW1=" + GIF + suffix + "\n")
                with self.assertRaisesRegex(ValueError, "tiling"):
                    canonicalize_and_validate(path, path.read_text())
        path = self.put(content=SCENE + "UW1=" + STREAM + "|2|2\n")
        with self.assertRaisesRegex(ValueError, "only supported for GIF"):
            canonicalize_and_validate(path, path.read_text())

    def test_long_gif_address_rejected_before_publishing(self):
        long_gif = "https://example.com/" + "a" * 780 + ".gif"
        path = self.put(content=SCENE + "UW1=" + long_gif + "\n")
        with self.assertRaisesRegex(ValueError, "1024-byte URL limit"):
            canonicalize_and_validate(path, path.read_text())

    def test_short_and_legacy_outside_tags_are_equivalent(self):
        for outside in ("O1", "OW1"):
            with self.subTest(outside=outside):
                path = self.put(content=SCENE + outside + "=" + PANEL + "\n")
                canonicalize_and_validate(path, path.read_text())
        path = self.put(content=SCENE + "O1=" + PANEL + "\nOW1=" + PANEL + "\n")
        with self.assertRaisesRegex(ValueError, "duplicate outside"):
            canonicalize_and_validate(path, path.read_text())

    def test_twenty_four_scene_records_are_allowed(self):
        inside = ("UF", "UC", "UW1", "UW2", "UW3", "UW4")
        outside = ("UOF", "UOC", "UO1", "UO2", "UO3", "UO4")
        exterior = ("OF", "OC", "O1", "O2", "O3", "O4")
        content = SCENE + "".join(t + "=" + PANEL + "\n" for t in exterior)
        content += "".join(t + "=" + GIF + "\n" for t in inside + outside)
        path = self.put(content=content)
        _, generated, _ = canonicalize_and_validate(path, path.read_text())
        self.assertIn("UO4=" + GIF, generated)

    def test_pack_order_and_complete_regeneration(self):
        self.put("A")
        self.put("B")
        (self.root / "pack-order.txt").write_text("10|B\n20|A\n")
        build(self.root)
        index = (self.root / "chunks" / "index.txt").read_text().splitlines()
        self.assertEqual([row.split("|")[0] for row in index[1:]], ["B", "A"])
        (self.root / "packs" / "B.txt").unlink()
        (self.root / "pack-order.txt").unlink()
        build(self.root)
        self.assertFalse((self.root / "chunks" / "B").exists())
        self.assertEqual(len((self.root / "chunks" / "index.txt").read_text().splitlines()), 2)

    def test_future_pack_in_order_is_ignored_until_file_is_created(self):
        self.put("Existing")
        (self.root / "pack-order.txt").write_text("10|Existing\n20|Future Pack\n")
        build(self.root)
        index = (self.root / "chunks" / "index.txt").read_text()
        self.assertIn("Existing|", index)
        self.assertNotIn("Future Pack|", index)
        self.assertFalse((self.root / "chunks" / "Future Pack").exists())
        self.put("Future Pack")
        build(self.root)
        index = (self.root / "chunks" / "index.txt").read_text().splitlines()
        self.assertEqual([row.split("|")[0] for row in index[1:]],
                         ["Existing", "Future Pack"])

    def test_duplicate_planned_pack_name_is_rejected(self):
        self.put("Existing")
        (self.root / "pack-order.txt").write_text("10|Future Pack\n20|Future Pack\n")
        with self.assertRaisesRegex(ValueError, "duplicate pack"):
            build(self.root)

    def test_duplicate_order_position_is_rejected_even_for_planned_packs(self):
        self.put("Existing")
        (self.root / "pack-order.txt").write_text("10|Future Pack\n10|Existing\n")
        with self.assertRaisesRegex(ValueError, "duplicate pack"):
            build(self.root)

    def test_every_generated_chunk_has_a_verifiable_sha256_envelope(self):
        many = "\n".join(SCENE.replace("[One]", f"[Scene {i:02}]")
                         for i in range(1, 30))
        self.put(content=many)
        build(self.root)
        index = (self.root / "chunks" / "index.txt").read_text().splitlines()
        number = int(index[1].split("|")[2])
        self.assertGreater(number, 1)
        combined = ""
        for i in range(number):
            raw = (self.root / "chunks" / "Pasture" / f"{i:03d}.txt").read_bytes()
            self.assertLessEqual(len(raw), 3000)
            header, text = raw.split(b"\n", 1)
            self.assertEqual(header.decode(),
                             f"CHUNK={i + 1}/{number}|" + hashlib.sha256(text).hexdigest())
            combined += text.decode("utf-8")
        self.assertEqual(combined.count("[Scene"), 29)
        self.assertTrue(combined.startswith("VERSION="))

    def test_rustic_window_github_gif_is_accepted_by_python(self):
        raw_gif = ("https://raw.githubusercontent.com/"
                   "gemmavelvetleaf/The-Box-Data/main/GIF/mind.gif")
        self.put(content=SCENE + "UW4=" + raw_gif + "\n")
        build(self.root)
        chunk = (self.root / "chunks" / "Pasture" / "000.txt").read_text()
        self.assertIn("UW4=" + raw_gif, chunk)

    def test_invalid_graphics_url_does_not_replace_published_chunks(self):
        path = self.put(content=SCENE + "UW4=https://example.com/mind.gif\n")
        build(self.root)
        old = {p.relative_to(self.root / "chunks"): p.read_bytes()
               for p in (self.root / "chunks").rglob("*.txt")}
        path.write_text(SCENE + "UW4=not-a-url\n")
        with self.assertRaisesRegex(ValueError, "HTTP"):
            build(self.root)
        new = {p.relative_to(self.root / "chunks"): p.read_bytes()
               for p in (self.root / "chunks").rglob("*.txt")}
        self.assertEqual(old, new)


if __name__ == "__main__":
    unittest.main()
