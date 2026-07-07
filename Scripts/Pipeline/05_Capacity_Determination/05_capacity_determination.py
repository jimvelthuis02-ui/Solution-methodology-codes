import csv
from collections import defaultdict
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


INPUT_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
SUMMARY_OUTPUT_FILE = common.STAGE5_OUTPUT_DIR / "Capacity_Determination_Summary.csv"
COUNT_OUTPUT_FILE = common.STAGE5_OUTPUT_DIR / "Constraint_Location_Counts_By_Slot_Size.csv"


def _read_candidate_configurations() -> list[dict[str, str]]:
    """Read Stage 4 candidates and keep only shortlisted configurations."""
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")
    with INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Candidate configuration CSV has no header row.")
        return [row for row in reader if str(row.get("Selection_Status", "")).strip() == "SHORTLISTED"]


def _parse_slot_sizes(value: str) -> list[float]:
    # Parse comma-separated representative slot sizes from Stage 4 output.
    sizes: list[float] = []
    for item in str(value).split(","):
        parsed = common._to_float(item)
        if parsed is not None:
            sizes.append(parsed)
    return sizes


def _parse_distribution(value: str) -> list[float]:
    # Parse distribution values in either percentage or fraction form to 0-1 scale.
    distribution: list[float] = []
    for item in str(value).split(","):
        text = str(item).strip()
        if text.endswith("%"):
            text = text[:-1].strip()
            parsed = common._to_float(text)
            distribution.append(0.0 if parsed is None else parsed / 100.0)
        else:
            parsed = common._to_float(text)
            distribution.append(0.0 if parsed is None else parsed if parsed <= 1.0 else parsed / 100.0)
    return distribution


def _capacity_rows_for_config(config: dict[str, str], sku_scenarios: dict[str, int]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build per-scenario capacity summary and slot-size constraint rows for one config."""
    slot_sizes = _parse_slot_sizes(config.get("Slot_Sizes", ""))
    distributions = _parse_distribution(config.get("Relative_Slot_Size_Distribution", ""))
    if not slot_sizes or not distributions:
        return [], []

    count_rows: list[dict[str, str]] = []
    summary_rows: list[dict[str, str]] = []

    for scenario_name, sku_count in sku_scenarios.items():
        # Allocate scenario SKU count across representative slot sizes.
        allocated_counts = common._allocate_counts_from_percentages(sku_count, distributions)
        cumulative_required_by_size: dict[float, int] = {}
        running_required = 0
        for index in range(len(slot_sizes) - 1, -1, -1):
            running_required += allocated_counts[index]
            cumulative_required_by_size[slot_sizes[index]] = running_required

        # Convert cumulative at-or-above constraints to exact counts per slot size.
        exact_required_by_size: dict[float, int] = {}
        ordered_sizes = sorted(slot_sizes)
        for index, slot_size in enumerate(ordered_sizes):
            next_size = ordered_sizes[index + 1] if index + 1 < len(ordered_sizes) else None
            next_required = cumulative_required_by_size.get(next_size, 0) if next_size is not None else 0
            exact_required_by_size[slot_size] = max(cumulative_required_by_size.get(slot_size, 0) - next_required, 0)

        exact_total = sum(exact_required_by_size.values())
        summary_rows.append(
            {
                "Config_ID": config.get("Config_ID", ""),
                "Method": config.get("Method", ""),
                "Scenario": config.get("Scenario", ""),
                "K": config.get("K", ""),
                "SKU_Scenario": scenario_name,
                "SKU_Count": str(sku_count),
                "Required_Locations_Total": str(exact_total),
                "Exact_Count_Distribution": "|".join(f"{int(size)}:{count}" for size, count in sorted(exact_required_by_size.items())),
                "Relative_Slot_Size_Distribution": config.get("Relative_Slot_Size_Distribution", ""),
            }
        )

        # Emit one constraint row per representative slot size.
        for slot_size in ordered_sizes:
            count_rows.append(
                {
                    "Config_ID": config.get("Config_ID", ""),
                    "Method": config.get("Method", ""),
                    "Scenario": config.get("Scenario", ""),
                    "K": config.get("K", ""),
                    "SKU_Scenario": scenario_name,
                    "SKU_Count": str(sku_count),
                    "Representative_Slot_Size": f"{slot_size:.0f}",
                    "Cluster_Count_Percentage": f"{(distributions[ordered_sizes.index(slot_size)] * 100):.2f}%",
                    "Assigned_SKUs_At_Representative_Size": str(allocated_counts[ordered_sizes.index(slot_size)]),
                    "Cumulative_Assigned_SKUs_At_Or_Above_Size": str(cumulative_required_by_size[slot_size]),
                    "Min_Required_Locations_At_Or_Above_Size": str(cumulative_required_by_size[slot_size]),
                    "Required_Locations_Total": str(exact_total),
                }
            )

    return summary_rows, count_rows


def build_capacity_determination() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Generate Stage 5 capacity outputs for all shortlisted configurations."""
    configs = _read_candidate_configurations()
    occupied_location_scenarios = common._build_occupied_location_count_scenarios([])

    summary_rows: list[dict[str, str]] = []
    count_rows: list[dict[str, str]] = []
    for config in configs:
        config_summary_rows, config_count_rows = _capacity_rows_for_config(config, occupied_location_scenarios)
        summary_rows.extend(config_summary_rows)
        count_rows.extend(config_count_rows)

    # Write compact summary used by downstream comparison and ranking.
    common._write_csv_clean(
        SUMMARY_OUTPUT_FILE,
        [
            "Config_ID",
            "Method",
            "Scenario",
            "K",
            "SKU_Scenario",
            "SKU_Count",
            "Required_Locations_Total",
            "Exact_Count_Distribution",
            "Relative_Slot_Size_Distribution",
        ],
        summary_rows,
    )

    # Write detailed per-slot-size constraints used by layout generation.
    common._write_csv_clean(
        COUNT_OUTPUT_FILE,
        [
            "Config_ID",
            "Method",
            "Scenario",
            "K",
            "SKU_Scenario",
            "SKU_Count",
            "Representative_Slot_Size",
            "Cluster_Count_Percentage",
            "Assigned_SKUs_At_Representative_Size",
            "Cumulative_Assigned_SKUs_At_Or_Above_Size",
            "Min_Required_Locations_At_Or_Above_Size",
            "Required_Locations_Total",
        ],
        count_rows,
    )

    return summary_rows, count_rows


if __name__ == "__main__":
    # Stage 5 entrypoint: derive capacity requirements from shortlisted candidates.
    summary_rows, count_rows = build_capacity_determination()
    print(
        "Capacity determination complete. "
        f"Summary rows: {len(summary_rows)}, slot-size rows: {len(count_rows)}."
    )
