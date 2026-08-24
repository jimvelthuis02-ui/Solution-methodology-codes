import csv
from collections import defaultdict
import sys
from functools import lru_cache
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


OUTPUT_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
MAX_REPRESENTATIVE_SLOT_SIZE_CM = 234.0


def _legal_slot_profile(slot_sizes: list[float]) -> bool:
    """Reject any slot-size profile that cannot be represented with legal operating sizes."""
    if not slot_sizes:
        return False
    min_size = min(slot_sizes)
    max_size = max(slot_sizes)
    if max_size > MAX_REPRESENTATIVE_SLOT_SIZE_CM:
        return False

    legal_pool: list[int] = []
    seen: set[int] = set()
    for slot_size in slot_sizes:
        if slot_size < min_size - 1e-9:
            return False
        rounded = int(round(slot_size))
        if rounded % 10 not in (4, 9):
            return False
        if rounded not in seen:
            seen.add(rounded)
            legal_pool.append(rounded)

    if not legal_pool:
        return False

    # Keep only capacity profiles that can actually form a legal rack-column fill
    # under the physical limit 754 cm. This prevents impossible families such as
    # single-size 44 cm profiles or other combinations that never match the exact
    # fill target at any valid row count.
    for row_count in range(1, 20):
        target_total = int(round(754.0 - (row_count - 1) * 16.0))
        if target_total <= 0:
            continue

        @lru_cache(maxsize=None)
        def can_make(remaining: int, slots_left: int) -> bool:
            if slots_left == 0:
                return remaining == 0
            if remaining < 0:
                return False
            for size in sorted(legal_pool):
                if size > remaining:
                    continue
                if can_make(remaining - size, slots_left - 1):
                    return True
            return False

        if can_make(target_total, row_count):
            return True
    return False


def _read_stage3_rows() -> list[dict[str, str]]:
    """Read slot-size summaries from all Stage 3 clustering methods."""
    merged_summary = common.SLOT_SIZE_ROOT / "Stage3_Slot_Size_Configuration_Summary_All.csv"
    if merged_summary.exists():
        with merged_summary.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                return []
            return list(reader)

    rows: list[dict[str, str]] = []
    method_summary_files = {
        "quantile_binning": "Quantile_Slot_Size_Configuration_Summary.csv",
        "hierarchical_clustering": "Hierarchical_Slot_Size_Configuration_Summary.csv",
        "kmeans_clustering": "KMeans_Slot_Size_Configuration_Summary.csv",
    }
    for method in common.METHODS:
        preferred = common.SLOT_SIZE_ROOT / method_summary_files.get(method, "Slot_Size_Configuration_Summary.csv")
        legacy = common.SLOT_SIZE_ROOT / method / "Slot_Size_Configuration_Summary.csv"
        path = preferred if preferred.exists() else legacy
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            rows.extend(list(reader))
    return rows


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _parse_percent(value: object | None) -> float:
    text = str(value or "").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    parsed = common._to_float(text)
    if parsed is None:
        return 0.0
    return parsed / 100.0 if parsed > 1.0 else parsed


def _to_int(value: object | None, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def build_candidate_configuration() -> Path:
    """Build Stage 4 candidate configurations from all Stage 3 summaries."""
    stage3_rows = _read_stage3_rows()
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)

    for row in stage3_rows:
        method = str(row.get("Method", "")).strip()
        scenario = str(row.get("Scenario", "")).strip()
        k = str(row.get("K", "")).strip()
        if method and scenario and k:
            grouped[(method, scenario, k)].append(row)

    grouped_items = sorted(
        grouped.items(),
        key=lambda item: (
            item[0][0],
            item[0][1],
            _to_int(item[0][2]),
        ),
    )

    output_rows: list[dict[str, str]] = []
    for index, ((method, scenario, k), rows) in enumerate(grouped_items, start=1):
        ordered_rows = sorted(rows, key=lambda row: common._to_float(row.get("Representative Slot Size")) or 0.0)
        slot_sizes = [
            min(common._to_float(row.get("Representative Slot Size")) or 0.0, MAX_REPRESENTATIVE_SLOT_SIZE_CM)
            for row in ordered_rows
        ]
        if not _legal_slot_profile(slot_sizes):
            continue
        distribution = [_parse_percent(row.get("Cluster Count Percentage")) for row in ordered_rows]

        output_rows.append(
            {
                "Config_ID": f"CFG_{index:03d}",
                "Method": method,
                "Scenario": scenario,
                "K": k,
                "Slot_Sizes": common._encode_excel_text(",".join(f"{size:.0f}" for size in slot_sizes)),
                "Relative Slot Size Distribution": common._encode_excel_text(",".join(f"{value:.4f}" for value in distribution)),
                "Source Sample": f"{method}|{scenario}|K={k}",
            }
        )

    _write_csv(
        OUTPUT_FILE,
        [
            "Config_ID",
            "Method",
            "Scenario",
            "K",
            "Slot_Sizes",
            "Relative Slot Size Distribution",
            "Source Sample",
        ],
        output_rows,
    )

    return OUTPUT_FILE


if __name__ == "__main__":
    output_path = build_candidate_configuration()
    print(f"Candidate configuration complete. Output written to: {output_path}")
