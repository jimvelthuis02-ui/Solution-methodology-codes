"""Naming helpers for isolated heuristic output trees."""

import csv
import re
from pathlib import Path

_CONFIG_ID_PATTERN = re.compile(r"\bCFG_(\d{3})(?:_(SU|PE|BP))?\b")


def suffixed_path(path: Path, suffix: str) -> Path:
    marker = f"_{suffix}"
    if path.stem.endswith(marker):
        return path
    return path.with_name(f"{path.stem}{marker}{path.suffix}")


def _suffix_cell(value: str, suffix: str, renamed_files: dict[str, str]) -> str:
    result = _CONFIG_ID_PATTERN.sub(lambda match: f"CFG_{match.group(1)}_{suffix}", str(value))
    for old_name, new_name in sorted(renamed_files.items(), key=lambda item: len(item[0]), reverse=True):
        result = result.replace(old_name, new_name)
    return result


def suffix_output_tree(output_dir: Path, suffix: str) -> None:
    """Append a heuristic suffix to every output filename and CFG identifier in CSVs."""
    if suffix not in {"SU", "PE", "BP"}:
        raise ValueError(f"Unsupported heuristic suffix: {suffix}")
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return

    renamed_files: dict[str, str] = {}
    files = sorted((path for path in output_dir.rglob("*") if path.is_file()), key=lambda path: len(path.parts), reverse=True)
    for source in files:
        target = suffixed_path(source, suffix)
        if source == target:
            continue
        source.replace(target)
        renamed_files[source.name] = target.name

    for csv_path in output_dir.rglob("*.csv"):
        with csv_path.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                continue
            fieldnames = reader.fieldnames
            rows = list(reader)
        updated_rows = [
            {
                field: _suffix_cell(row.get(field, ""), suffix, renamed_files)
                for field in fieldnames
            }
            for row in rows
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as target:
            writer = csv.DictWriter(target, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(updated_rows)
