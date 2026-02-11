from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _is_aux_toml(p: Path) -> bool:
    return p.name.lower() in {"teachers_reviews.toml"}


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def sync(*, src_root: Path, dst_root: Path, clean_dst: bool) -> tuple[int, int, list[str]]:
    src_root = src_root.resolve()
    dst_root = dst_root.resolve()

    if not src_root.exists() or not src_root.is_dir():
        raise SystemExit(f"source not found or not a dir: {src_root}")

    if clean_dst and dst_root.exists():
        shutil.rmtree(dst_root)

    dst_root.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = 0
    warnings: list[str] = []

    for entry in sorted(src_root.iterdir()):
        if not entry.is_dir():
            continue

        repo_name = entry.name
        readme = entry / "readme.toml"
        if readme.exists() and readme.is_file():
            _copy_file(readme, dst_root / repo_name / "readme.toml")
            copied += 1
            continue

        # Fallback: try to salvage one TOML as readme.toml
        tomls = [p for p in entry.glob("*.toml") if p.is_file() and not _is_aux_toml(p)]
        if len(tomls) == 1:
            _copy_file(tomls[0], dst_root / repo_name / "readme.toml")
            copied += 1
            warnings.append(f"{repo_name}: no readme.toml; used {tomls[0].name} as readme.toml")
            continue

        if len(tomls) > 1:
            skipped += 1
            warnings.append(f"{repo_name}: multiple toml but no readme.toml; skipped ({', '.join(p.name for p in tomls[:5])}{'...' if len(tomls) > 5 else ''})")
            continue

        skipped += 1

    return copied, skipped, warnings


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync readme.toml from final/ backup into a bot-readable courses directory")
    parser.add_argument(
        "--src",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "final",
        help="Source directory (default: <repo_root>/final)",
    )
    parser.add_argument(
        "--dst",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "courses_seed",
        help="Destination directory (default: hitsz_manager/data/courses_seed)",
    )
    parser.add_argument(
        "--clean-dst",
        action="store_true",
        help="Delete destination directory before syncing",
    )

    args = parser.parse_args()

    copied, skipped, warnings = sync(src_root=args.src, dst_root=args.dst, clean_dst=args.clean_dst)

    print(f"✅ synced: {copied}, skipped: {skipped}")
    if warnings:
        print("\n⚠️ warnings:")
        for w in warnings[:20]:
            print(f"- {w}")
        if len(warnings) > 20:
            print(f"... ({len(warnings) - 20} more)")


if __name__ == "__main__":
    main()
