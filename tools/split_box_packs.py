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
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

MAX_CHUNK_BYTES = 3000
# Leave room for CHUNK=index/count|sha256\n on each response.
MAX_PAYLOAD_BYTES = MAX_CHUNK_BYTES - 90
MAX_SCENES = 54
# Button prefix is up to "54:" (3 characters) out of 24.
MAX_MENU_ALIAS = 21
MAX_SCENE_LINES = 24  # 6 core + 6 outside/mirror + 6 inside G + 6 outside G
MAX_PACKS = 24
CORE = {"F": 0, "FLOOR": 0, "C": 1, "CEILING": 1,
        "W1": 2, "WALL1": 2, "W2": 3, "WALL2": 3,
        "W3": 4, "WALL3": 4, "W4": 5, "WALL4": 5}
UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
MAX_TILE_REPEAT = 32
URL_PANEL = {"UF": (0, False), "UC": (1, False),
             "UOF": (0, True), "UOC": (1, True),
             **{f"UW{i}": (i + 1, False) for i in range(1, 5)},
             **{f"UO{i}": (i + 1, True) for i in range(1, 5)}}


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


def check_graphics_url(value: str, location: str) -> None:
    """Accept HTTP(S) media pages unchanged; only GIFs can specify tiling."""
    if len(value.encode("utf-8")) > 1024 or not value or any(
        ord(c) < 33 or c in '<>"\'' for c in value
    ):
        error(f"{location}: invalid graphics URL {value!r}")

    raw_url = value
    across, down = 1, 1
    if "|" in value:
        pieces = value.split("|")
        if len(pieces) != 3 or any(
            not p.isascii() or not p.isdecimal() or not 1 <= int(p) <= MAX_TILE_REPEAT
            for p in pieces[1:]
        ):
            error(f"{location}: invalid GIF tiling; expected URL|across|down (1-32)")
        raw_url = pieces[0]
        across, down = int(pieces[1]), int(pieces[2])
        if not urlsplit(raw_url).path.lower().endswith(".gif"):
            error(f"{location}: tiling is only supported for GIF images")

    try:
        parsed = urlsplit(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            error(f"{location}: graphics URL must be an HTTP(S) address with a hostname")
        # Force malformed ports to fail validation as well.
        _ = parsed.port
    except ValueError as exc:
        error(f"{location}: invalid graphics URL {value!r}: {exc}")

    if parsed.path.lower().endswith(".gif"):
        gif = raw_url
        if gif.startswith("https://github.com/") and "/blob/" in gif:
            gif = "https://raw.githubusercontent.com/" + gif[19:].replace("/blob/", "/", 1)
        style = (f"background-image:url('{gif}');background-repeat:repeat;"
                 f"background-size:{100.0 / across:.6f}% {100.0 / down:.6f}%")
        html = ("<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "</head><body style=\"margin:0;background-color:black;" + style
                + ";height:100vh;width:100vw;overflow:hidden\"></body></html>")
        if len(("data:text/html," + quote(html, safe="")).encode("utf-8")) > 1024:
            error(f"{location}: GIF media page exceeds Second Life's 1024-byte URL limit; use a shorter GIF URL")


def make_scene_aliases(source: str, path: Path) -> dict[str, str]:
    """Unique, stable menu labels; keep full canonical names in scene headings."""
    headers: list[tuple[str, int]] = []
    seen_full: set[str] = set()
    for lineno, raw in enumerate(source.split("\n"), 1):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if not name:
                error(f"{path}:{lineno}: blank scene name")
            if name.casefold() in seen_full:
                error(f"{path}:{lineno}: duplicate scene name: {name}")
            seen_full.add(name.casefold())
            headers.append((name, lineno))
    # Reserve all existing short labels first so hashed long labels can never
    # displace an unchanged short scene name.
    used = {name.casefold() for name, _ in headers if len(name) <= MAX_MENU_ALIAS}
    aliases: dict[str, str] = {}
    for name, lineno in headers:
        if len(name) <= MAX_MENU_ALIAS:
            aliases[name] = name
            continue
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
        for digits in range(5, len(digest) + 1):
            head = MAX_MENU_ALIAS - digits - 1
            if head < 0:
                break
            alias = name[:head] + "~" + digest[:digits]
            if alias.casefold() not in used:
                aliases[name] = alias
                used.add(alias.casefold())
                break
        if name not in aliases:
            error(f"{path}:{lineno}: cannot assign unique menu label: {name}")
    return aliases


def canonicalize_and_validate(path: Path, source: str) -> tuple[str, str, int]:
    name = path.stem
    if not name or name != name.strip() or "|" in name:
        error(f"{path}: invalid pack filename")
    source = source.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    aliases = make_scene_aliases(source, path)
    body: list[str] = []
    scene_name = ""
    scene_started = False
    scenes = 0
    core_seen: set[int] = set()
    optional_seen: set[int] = set()
    gif_seen: set[int] = set()
    outside_g_seen: set[int] = set()
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
            scenes += 1
            if scenes > MAX_SCENES:
                error(f"{place}: exceeded {MAX_SCENES} scenes per pack")
            scene_started = True
            scene_lines = 0
            core_seen = set()
            optional_seen = set()
            gif_seen = set()
            outside_g_seen = set()
            body.append(line)
            alias = aliases[scene_name]
            if alias != scene_name:
                # The LSL loader consumes ALIAS= as metadata, not a scene panel.
                # The original [canonical name] remains available in-world.
                body.append("ALIAS=" + alias)
                print(f"MENU LABEL: {path.name}: {scene_name!r} -> {alias!r}")
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
            if tag.startswith("U"):
                panel, outside_g = URL_PANEL.get(tag, (-1, False))
                if panel < 0:
                    error(f"{place}: unknown graphics panel {tag!r}")
                check_graphics_url(spec, place)
                target = outside_g_seen if outside_g else gif_seen
                if panel in target:
                    error(f"{place}: duplicate graphics panel {tag}")
                target.add(panel)
            elif tag.endswith("_MIRROR"):
                panel = CORE.get(tag[:-7], -1)
                if spec.upper() != "ON":
                    error(f"{place}: mirror setting must be ON")
            elif tag.startswith("O"):
                suffix = tag[1:]
                if suffix in ("1", "2", "3", "4"):
                    suffix = "W" + suffix
                panel = CORE.get(suffix, -1)
                if panel >= 0:
                    check_spec(spec, place)
            if panel < 0:
                error(f"{place}: unknown panel {tag!r}")
            if not tag.startswith("U"):
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
        if n > MAX_PAYLOAD_BYTES:
            error(f"{path}: one generated line exceeds {MAX_PAYLOAD_BYTES} bytes")
        if current and size + n > MAX_PAYLOAD_BYTES:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(line)
        size += n
    if current:
        chunks.append("".join(current))
    return chunks


def read_order(path: Path, names: set[str]) -> dict[str, int]:
    """Order existing packs; reserve future pack names without failing builds."""
    if not path.exists():
        return {}
    result: dict[str, int] = {}
    used_names: set[str] = set()
    used_positions: set[int] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pieces = line.split("|", 1)
        if len(pieces) != 2 or not pieces[0].strip().isdigit():
            error(f"{path}:{lineno}: expected NUMBER|Pack Name")
        position, name = int(pieces[0]), pieces[1].strip()
        if not name:
            error(f"{path}:{lineno}: pack name cannot be blank")
        # Treat duplicates as errors even when the pack is not created yet.
        if name in used_names or position in used_positions:
            error(f"{path}:{lineno}: duplicate pack name or position {name!r}")
        used_names.add(name)
        used_positions.add(position)
        if name not in names:
            print(f"PLANNED PACK — SKIPPED: {name}.txt not found in packs/ "
                  f"(pack-order.txt line {lineno}); will be included when created")
            continue
        result[name] = position
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
                checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
                envelope = f"CHUNK={i + 1}/{len(chunks)}|{checksum}\n{text}"
                if len(envelope.encode("utf-8")) > MAX_CHUNK_BYTES:
                    error(f"{name}: generated chunk {i} exceeds HTTP capacity")
                (target / f"{i:03d}.txt").write_text(envelope, encoding="utf-8", newline="")
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
        reason = str(exc)
        print("\n" + "=" * 72, file=sys.stderr)
        print("THE BOX SCENE PACK BUILD FAILED — NO NEW SCENES WERE PUBLISHED", file=sys.stderr)
        print("Fix this error and commit again:", file=sys.stderr)
        print(reason, file=sys.stderr)
        print("Previous generated scene data is preserved.\n" + "=" * 72,
              file=sys.stderr)
        match = re.match(r"(.+?\.txt):(\d+)(?: \[.*?\])?: (.+)", reason)
        annotation = reason
        if match:
            path, line, detail = match.groups()
            try:
                path = str(Path(path).relative_to(args.root.resolve()))
            except ValueError:
                pass
            annotation = f"file={path},line={line}::" + detail
            print("::error " + annotation, file=sys.stderr)
        else:
            print("::error::" + annotation, file=sys.stderr)
        summary = os.getenv("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8") as report:
                report.write("## ❌ The Box scene pack update failed\n\n")
                report.write("The new scene data was **not published**. ")
                report.write("The previous generated version is still available.\n\n")
                report.write("**Fix:** `" + reason.replace("`", "'") + "`\n")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
