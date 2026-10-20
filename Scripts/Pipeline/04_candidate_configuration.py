import csv
from collections import defaultdict
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


# Stage 4 output and legal slot-size bound.
OUTPUT_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
MAX_REPRESENTATIVE_SLOT_SIZE_CM = common.MAX_REPRESENTATIVE_SLOT_SIZE_CM


def _legal_slot_profile(slot_sizes: list[float]) -> bool:
    """Return True only when the configured family itself can support a legal physical stack.

    Lower rows must remain on the actual configured family; only the final top slot may legally
    complete the remaining physical height.
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

    lower_bounds = set(family_values)
    if not lower_bounds:
        return False

    for lower_count in range(1, 13):
        reachable: set[int] = {0}
        for _ in range(lower_count):
            next_reachable: set[int] = set()
            for partial_sum in reachable:
                for value in family_values:
                    next_reachable.add(partial_sum + value)
            reachable = next_reachable

        for lower_sum in reachable:
            support_below_top = lower_sum + 16 * max(lower_count - 1, 0)
            if support_below_top < 504.0:
                continue
            top_slot = 754 - lower_sum - 16 * lower_count
            if top_slot <= 0:
                continue
            if top_slot > int(round(MAX_REPRESENTATIVE_SLOT_SIZE_CM)):
                continue
            if top_slot % 10 not in (4, 9):
                continue
            if top_slot >= min(family_values):
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


_write_csv = common._write_csv_preserve


def _parse_percent(value: object | None) -> float:
    """Parse a percentage or fraction into a normalized value from zero to one."""
    text = str(value or "").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    parsed = common._to_float(text)
    if parsed is None:
        return 0.0
    return parsed / 100.0 if parsed > 1.0 else parsed


_to_int = common._to_int_default


def _unique_slot_families(rows: list[dict[str, str]]) -> tuple[list[float], list[float]]:
    """Collapse clusters mapped to the same legal slot size and recompute their shares."""
    cluster_counts: dict[int, int] = defaultdict(int)
    percentage_mass: dict[int, float] = defaultdict(float)
    complete_cluster_counts = True

    for row in rows:
        representative = common._to_float(row.get("Representative Slot Size"))
        if representative is None:
            continue
        slot_size = int(round(min(representative, MAX_REPRESENTATIVE_SLOT_SIZE_CM)))
        count = _to_int(row.get("Cluster Count"), -1)
        if count < 0:
            complete_cluster_counts = False
        else:
            cluster_counts[slot_size] += count
        percentage_mass[slot_size] += _parse_percent(row.get("Cluster Count Percentage"))

    slot_sizes = sorted(set(cluster_counts) | set(percentage_mass))
    if complete_cluster_counts and sum(cluster_counts.values()) > 0:
        total = sum(cluster_counts.values())
        distribution = [cluster_counts[size] / total for size in slot_sizes]
    else:
        total_mass = sum(percentage_mass.values())
        distribution = [percentage_mass[size] / total_mass for size in slot_sizes] if total_mass > 0 else []
    return [float(size) for size in slot_sizes], distribution


def build_candidate_configuration() -> Path:
    """Build Stage 4 candidate configurations from all Stage 3 summaries."""
    stage3_rows = _read_stage3_rows()
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)

    for row in stage3_rows:
        method = str(row.get("Clustering_Method", row.get("Clustering Method", row.get("Method", "")))).strip()
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
        slot_sizes, distribution = _unique_slot_families(rows)
        if not _legal_slot_profile(slot_sizes):
            continue

        output_rows.append(
            {
                "Config_ID": f"CFG_{index:03d}",
                "Clustering_Method": method,
                "Scenario": scenario,
                "K": k,
                "Unique_Slot_Size_Count": str(len(slot_sizes)),
                "Slot_Sizes": common._encode_excel_text(",".join(f"{size:.0f}" for size in slot_sizes)),
                "Relative_Slot_Size_Distribution": common._encode_excel_text(",".join(f"{value:.4f}" for value in distribution)),
                "Source_Sample": f"{method}|{scenario}|K={k}",
            }
        )

    _write_csv(
        OUTPUT_FILE,
        [
            "Config_ID",
            "Clustering_Method",
            "Scenario",
            "K",
            "Unique_Slot_Size_Count",
            "Slot_Sizes",
            "Relative_Slot_Size_Distribution",
            "Source_Sample",
        ],
        output_rows,
    )

    return OUTPUT_FILE


if __name__ == "__main__":
    output_path = build_candidate_configuration()
    print(f"Candidate configuration complete. Output written to: {output_path}")
