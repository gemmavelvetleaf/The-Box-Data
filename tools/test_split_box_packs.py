"""Offline regression checks for The Box chunk generator."""
from __future__ import annotations

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

    def test_duplicate_name_rejected(self):
        path = self.put(content=SCENE + "\n" + SCENE)
        with self.assertRaisesRegex(ValueError, "duplicate scene name"):
            canonicalize_and_validate(path, path.read_text())

    def test_invalid_uuid_rejected(self):
        path = self.put(content=SCENE.replace(UUID, "no-texture"))
        with self.assertRaisesRegex(ValueError, "invalid texture UUID"):
            canonicalize_and_validate(path, path.read_text())

    def test_gif_and_outside_tags_can_share_panel(self):
        source = SCENE + "OW1=" + PANEL + "\nGW1=" + GIF + "\n"
        path = self.put(content=source)
        _, generated, count = canonicalize_and_validate(path, path.read_text())
        self.assertEqual(count, 1)
        self.assertIn("GW1=" + GIF, generated)

    def test_bad_gif_url_is_rejected(self):
        path = self.put(content=SCENE + "GW1=https://example.com/photo.gif\n")
        with self.assertRaisesRegex(ValueError, "invalid GitHub GIF URL"):
            canonicalize_and_validate(path, path.read_text())

    def test_duplicate_gif_panel_is_rejected(self):
        path = self.put(content=SCENE + "GW1=" + GIF + "\nGW1=" + GIF + "\n")
        with self.assertRaisesRegex(ValueError, "duplicate GIF panel"):
            canonicalize_and_validate(path, path.read_text())

    def test_all_six_short_gif_tags_are_valid(self):
        tags = ("GF", "GC", "GW1", "GW2", "GW3", "GW4")
        path = self.put(content=SCENE + "".join(f"{tag}={GIF}\n" for tag in tags))
        _, generated, count = canonicalize_and_validate(path, path.read_text())
        self.assertEqual(count, 1)
        for tag in tags:
            self.assertIn(f"{tag}={GIF}", generated)

    def test_old_long_gif_tag_is_rejected(self):
        path = self.put(content=SCENE + "GIFW1=" + GIF + "\n")
        with self.assertRaisesRegex(ValueError, "unknown GIF panel"):
            canonicalize_and_validate(path, path.read_text())

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


if __name__ == "__main__":
    unittest.main()
