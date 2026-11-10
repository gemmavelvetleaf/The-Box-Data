#!/usr/bin/env python3
"""Build validated, automatically versioned Box chunks from packs/*.txt.

Human-maintained source files are never rewritten. Filename determines PACK;
generated content determines VERSION. Legacy handwritten headers are ignored.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import tempfile
from pathlib import Path

MAX_CHUNK_BYTES = 3000
MAX_SCENES = 54
MAX_SCENE_LINES = 18  # six core + six outside/mirror + six optional GIF tags
MAX_PACKS = 24
CORE = {"F": 0, "FLOOR": 0, "C": 1, "CEILING": 1,
        "W1": 2, "WALL1": 2, "W2": 3, "WALL2": 3,
        "W3": 4, "WALL3": 4, "W4": 5, "WALL4": 5}
UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
GIF_URL = re.compile(
    r"https://(?:github\.com/[^/\s]+/[^/\s]+/blob/[^/\s]+/[^\s]+\.gif"
    r"|raw\.githubusercontent\.com/[^/\s]+/[^/\s]+/[^/\s]+/[^\s]+\.gif)\Z",
    re.IGNORECASE,
)


def error(message: str) -> None:
    raise ValueError(message)


def check_number(value: str, location: str) -> float:
    if not NUMBER.fullmatch(value.strip()):
        error(f"{location}: invalid numeric value {value!r}")
    number = float(value)
    if not math.isfinite(number):
        error(f"{location}: nonfinite numeric value {value!r}")
    return number


def check_vector(value: str, location: str) -> None:
    value = value.strip()
    if not value.startswith("<") or not value.endswith(">"):
        error(f"{location}: expected <x,y,z>")
    pieces = value[1:-1].split(",")
    if len(pieces) != 3:
        error(f"{location}: vector requires three components")
    for part in pieces:
        check_number(part, location)


def check_spec(value: str, location: str) -> None:
    parts = [p.strip() for p in value.split("|")]
    if len(parts) != 7:
        error(f"{location}: panel requires 7 pipe-separated fields; got {len(parts)}")
    if not UUID.fullmatch(parts[0]) or parts[0].lower() == "00000000-0000-0000-0000-000000000000":
        error(f"{location}: invalid texture UUID {parts[0]!r}")
    check_vector(parts[1], location)
    check_vector(parts[2], location)
    check_number(parts[3], location)
    check_vector(parts[4], location)
    if not 0 <= check_number(parts[5], location) <= 1:
        error(f"{location}: alpha must be between 0 and 1")
    if parts[6] not in ("0", "1"):
        error(f"{location}: fullbright must be 0 or 1")


def canonicalize_and_validate(path: Path, source: str) -> tuple[str, str, int]:
    name = path.stem
    if not name or name != name.strip() or "|" in name:
        error(f"{path}: invalid pack filename")
    source = source.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    body: list[str] = []
    scene_name = ""
    scene_started = False
    scenes = 0
    names: set[str] = set()
    core_seen: set[int] = set()
    optional_seen: set[int] = set()
    gif_seen: set[int] = set()
    scene_lines = 0

    def finish_scene(at_line: int) -> None:
        if not scene_name:
            return
        if len(core_seen) != 6:
            missing = [tag for tag, i in (("F", 0), ("C", 1), ("W1", 2),
                                          ("W2", 3), ("W3", 4), ("W4", 5))
                       if i not in core_seen]
            error(f"{path}:{at_line}: [{scene_name}] missing {', '.join(missing)}")

    for lineno, raw in enumerate(source.split("\n"), 1):
        line = raw.strip()
        place = f"{path}:{lineno}" + (f" [{scene_name}]" if scene_name else "")
        # Existing metadata may be incorrect; it is no longer authoritative.
        if scenes == 0 and line.upper().startswith(("VERSION=", "PACK=")):
            continue
        if line.startswith("[") and line.endswith("]"):
            finish_scene(lineno)
            scene_name = line[1:-1].strip()
            if not scene_name:
                error(f"{place}: blank scene name")
            short_name = scene_name[:24].casefold()
            if short_name in names:
                error(f"{place}: duplicate scene name after menu truncation: {scene_name}")
            names.add(short_name)
            scenes += 1
            if scenes > MAX_SCENES:
                error(f"{place}: exceeded {MAX_SCENES} scenes per pack")
            if len(scene_name) > 24:
                print(f"WARNING: {place}: menu truncates name {scene_name!r}")
            scene_started = True
            scene_lines = 0
            core_seen = set()
            optional_seen = set()
            gif_seen = set()
            body.append(line)
            continue
        if not scene_started:
            if line and not line.startswith(("#", "//")):
                error(f"{place}: expected [Scene Name] or a comment")
            body.append(raw)
            continue
        if not line:
            finish_scene(lineno)
            scene_started = False
            scene_name = ""
            body.append(raw)
            continue
        if "=" not in line:
            error(f"{place}: expected PANEL=values")
        tag, spec = (part.strip() for part in line.split("=", 1))
        tag = tag.upper()
        if tag in CORE:
            panel = CORE[tag]
            if panel in core_seen:
                error(f"{place}: duplicate panel {tag}")
            core_seen.add(panel)
            check_spec(spec, place)
        else:
            panel = -1
            if tag.startswith("G"):
                panel = CORE.get(tag[1:], -1)
                if panel < 0:
                    error(f"{place}: unknown GIF panel {tag!r}")
                if (len(spec) > 500 or not GIF_URL.fullmatch(spec)
                        or any(c in spec for c in ('<', '>', '"', "'"))):
                    error(f"{place}: invalid GitHub GIF URL {spec!r}")
                if panel in gif_seen:
                    error(f"{place}: duplicate GIF panel {tag}")
                gif_seen.add(panel)
            elif tag.endswith("_MIRROR"):
                panel = CORE.get(tag[:-7], -1)
                if spec.upper() != "ON":
                    error(f"{place}: mirror setting must be ON")
            elif tag.startswith("O"):
                panel = CORE.get(tag[1:], -1)
                if panel >= 0:
                    check_spec(spec, place)
            if panel < 0:
                error(f"{place}: unknown panel {tag!r}")
            if not tag.startswith("G"):
                if panel in optional_seen:
                    error(f"{place}: duplicate outside/mirror panel {tag}")
                optional_seen.add(panel)
        scene_lines += 1
        if scene_lines > MAX_SCENE_LINES:
            error(f"{place}: exceeded {MAX_SCENE_LINES} lines per scene")
        body.append(raw)
    finish_scene(len(source.split("\n")))
    if scenes == 0:
        error(f"{path}: no scenes")

    payload = "\n".join(body).strip("\n") + "\n"
    version = "h" + hashlib.sha256((name + "\n" + payload).encode("utf-8")).hexdigest()[:12]
    return version, f"VERSION={version}\nPACK={name}\n\n" + payload, scenes


def split_line_aligned(text: str, path: Path) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        n = len(line.encode("utf-8"))
        if n > MAX_CHUNK_BYTES:
            error(f"{path}: one generated line exceeds {MAX_CHUNK_BYTES} bytes")
        if current and size + n > MAX_CHUNK_BYTES:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += n
    if current:
        chunks.append("".join(current))
    return chunks


def read_order(path: Path, names: set[str]) -> dict[str, int]:
    if not path.exists():
        return {}
    result: dict[str, int] = {}
    used: set[int] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pieces = line.split("|", 1)
        if len(pieces) != 2 or not pieces[0].strip().isdigit():
            error(f"{path}:{lineno}: expected NUMBER|Pack Name")
        position, name = int(pieces[0]), pieces[1].strip()
        if name not in names or name in result or position in used:
            error(f"{path}:{lineno}: unknown/duplicate pack or position {name!r}")
        result[name] = position
        used.add(position)
    return result


def build(root: Path) -> None:
    packs, out = root / "packs", root / "chunks"
    if not packs.is_dir():
        error(f"missing {packs}")
    files = sorted(packs.glob("*.txt"), key=lambda p: p.name.casefold())
    if not files:
        error("no source packs/*.txt found")
    if len(files) > MAX_PACKS:
        error(f"{len(files)} packs exceeds the {MAX_PACKS}-pack menu limit")
    if len({p.stem.casefold() for p in files}) != len(files):
        error("pack filenames collide ignoring case")
    order = read_order(root / "pack-order.txt", {p.stem for p in files})
    files.sort(key=lambda p: (0, order[p.stem]) if p.stem in order
               else (1, p.name.casefold()))

    # Validate all packs before touching the last good generated output.
    records: list[tuple[str, str, list[str], int]] = []
    for path in files:
        version, text, scenes = canonicalize_and_validate(
            path, path.read_text(encoding="utf-8"))
        records.append((path.stem, version, split_line_aligned(text, path), scenes))

    with tempfile.TemporaryDirectory(prefix="box-build-", dir=root) as directory:
        stage = Path(directory) / "new"
        stage.mkdir()
        index = ["# AUTO-GENERATED - DO NOT EDIT\n"]
        for name, version, chunks, scenes in records:
            target = stage / name
            target.mkdir()
            for i, text in enumerate(chunks):
                (target / f"{i:03d}.txt").write_text(text, encoding="utf-8", newline="")
            index.append(f"{name}|{version}|{len(chunks)}\n")
            print(f"{name}: {scenes} scenes, version {version}, {len(chunks)} chunks")
        (stage / "index.txt").write_text("".join(index), encoding="utf-8", newline="")
        backup = Path(directory) / "previous"
        if out.exists():
            os.replace(out, backup)
        try:
            os.replace(stage, out)
        except OSError:
            if backup.exists():
                os.replace(backup, out)
            raise
    print("All packs validated; generated chunks updated.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path,
                        default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        build(args.root.resolve())
    except (ValueError, OSError, UnicodeError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    main()
