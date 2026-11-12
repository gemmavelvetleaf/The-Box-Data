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
