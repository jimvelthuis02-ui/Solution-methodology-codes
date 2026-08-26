import csv
from collections import defaultdict
import sys
from functools import lru_cache
from itertools import combinations_with_replacement, permutations
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


OUTPUT_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
MAX_REPRESENTATIVE_SLOT_SIZE_CM = 234.0


def _legal_slot_profile(slot_sizes: list[float]) -> bool:
    """Return True if some legal column can be built from the family.

    The candidate family may be reused in any order, and the final top slot may be a
    nearby legal value within +/-30 cm of an existing family value. The exact 754 cm
    total must still be reached, and the beam directly beneath the top slot must land in
    the required 504-520 cm support band.
    """
    if not slot_sizes:
        return False

    family_values: list[int] = []
    seen: set[int] = set()
    for slot_size in sorted(float(size) for size in slot_sizes):
        rounded = int(round(slot_size))
        if rounded <= 0:
            return False
        if rounded > int(round(MAX_REPRESENTATIVE_SLOT_SIZE_CM)):
            return False
        if rounded % 10 not in (4, 9):
            return False
        if rounded not in seen:
            seen.add(rounded)
            family_values.append(rounded)

    if not family_values:
        return False

    legal_family: set[int] = set()
    for base_value in family_values:
        for delta in range(-30, 31):
            candidate = base_value + delta
            if candidate < 4:
                continue
            if candidate > int(round(MAX_REPRESENTATIVE_SLOT_SIZE_CM)):
                continue
            if candidate % 10 in (4, 9):
                legal_family.add(candidate)

    if not legal_family:
        return False

    reachable: dict[int, set[int]] = {0: {0}}
    max_count = 60
    for lower_count in range(1, max_count + 1):
        sums = set()
        for partial_sum in reachable.get(lower_count - 1, set()):
            for value in sorted(legal_family):
                sums.add(partial_sum + value)
        if not sums:
            continue
        reachable[lower_count] = sums
        for lower_sum in sums:
            support_below_top = lower_sum + 16 * max(lower_count - 1, 0)
            if not (504.0 <= support_below_top <= 520.0 + 1e-9):
                continue
            top_slot = 754 - lower_sum - 16 * lower_count
            if top_slot <= 0:
                continue
            if top_slot > int(round(MAX_REPRESENTATIVE_SLOT_SIZE_CM)):
                continue
            if top_slot % 10 not in (4, 9):
                continue
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
