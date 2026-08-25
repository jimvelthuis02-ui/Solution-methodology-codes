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
SCENARIO_HEIGHT_INPUT_FILE = common.STAGE2_OUTPUT_DIR / "02_Item_Height_Scenarios_Delta_Weighted.csv"
SCENARIO_HEIGHT_COLUMNS = [
    "Scenario_1_Item_Height",
    "Scenario_2_Item_Height",
    "Scenario_3_Item_Height",
    "Scenario_4_Item_Height",
    "Scenario_5_Item_Height",
    "Scenario_6_Item_Height",
]
SCENARIO_HEIGHT_LABELS = {
    "Scenario_1_Item_Height": "Scenario 1",
    "Scenario_2_Item_Height": "Scenario 2",
    "Scenario_3_Item_Height": "Scenario 3",
    "Scenario_4_Item_Height": "Scenario 4",
    "Scenario_5_Item_Height": "Scenario 5",
    "Scenario_6_Item_Height": "Scenario 6",
}


def _read_candidate_configurations() -> list[dict[str, str]]:
    """Read all Stage 4 candidate configurations."""
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")
    with INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Candidate configuration CSV has no header row.")
        return list(reader)


def _parse_slot_sizes(value: str) -> list[float]:
    # Parse comma-separated representative slot sizes from Stage 4 output.
    sizes: list[float] = []
    raw = common._decode_excel_text(value)
    for item in raw.split(","):
        parsed = common._to_float(item)
        if parsed is not None:
            sizes.append(parsed)
    return sizes


def _parse_distribution(value: str) -> list[float]:
    # Parse distribution values in either percentage or fraction form to 0-1 scale.
    distribution: list[float] = []
    raw = common._decode_excel_text(value)
    for item in raw.split(","):
        text = str(item).strip()
        if text.endswith("%"):
            text = text[:-1].strip()
            parsed = common._to_float(text)
            distribution.append(0.0 if parsed is None else parsed / 100.0)
        else:
            parsed = common._to_float(text)
            distribution.append(0.0 if parsed is None else parsed if parsed <= 1.0 else parsed / 100.0)
    return distribution


def _distribution_value(config: dict[str, str]) -> str:
    value = str(config.get("Relative Slot Size Distribution", "")).strip()
    if value:
        return value
    return str(config.get("Relative_Slot_Size_Distribution", ""))


def _scenario_aware_slot_size_counts(config: dict[str, str]) -> dict[str, dict[float, int]]:
    """Map each scenario's measured item heights to the configuration's representative slot sizes."""
    rows = []
    if SCENARIO_HEIGHT_INPUT_FILE.exists():
        with SCENARIO_HEIGHT_INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            rows = list(reader)

    if not rows:
        return {}

    slot_sizes = sorted({float(value) for value in _parse_slot_sizes(config.get("Slot_Sizes", ""))})
    if not slot_sizes:
        return {}

    by_scenario: dict[str, dict[float, int]] = {
        label: defaultdict(int) for label in SCENARIO_HEIGHT_LABELS.values()
    }

    for row in rows:
        location = str(row.get("Location", "")).strip()
        if not location:
            continue
        for scenario_column, scenario_label in SCENARIO_HEIGHT_LABELS.items():
            item_height = common._to_float(row.get(scenario_column))
            if item_height is None:
                continue
            slot_size = min(
                (size for size in slot_sizes if size >= item_height),
                default=max(slot_sizes),
            )
            by_scenario[scenario_label][slot_size] += 1

    return {scenario_name: dict(counts) for scenario_name, counts in by_scenario.items() if counts}


def _capacity_rows_for_config(config: dict[str, str], sku_scenarios: dict[str, int]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build exact capacity rows using only the scenario already assigned to the config.

    Sensitivity analysis should be handled separately in a later stage; Stage 5 is
    intended to reflect the scenario used to generate the configuration itself.
    """
    slot_sizes = _parse_slot_sizes(config.get("Slot_Sizes", ""))
    distributions = _parse_distribution(_distribution_value(config))
    if not slot_sizes or not distributions:
        return [], []

    scenario_aware_counts = _scenario_aware_slot_size_counts(config)
    config_scenario = str(config.get("Scenario", "")).strip()
    if config_scenario:
        scenario_aware_counts = {
            config_scenario: scenario_aware_counts.get(config_scenario, {})
        }

    count_rows: list[dict[str, str]] = []
    summary_rows: list[dict[str, str]] = []

    ordered_sizes = sorted(set(slot_sizes))
    if scenario_aware_counts:
        selected_scenario_counts = {
            scenario_name: exact_required_by_size
            for scenario_name, exact_required_by_size in scenario_aware_counts.items()
            if scenario_name and exact_required_by_size
        }
        if not selected_scenario_counts:
            scenario_aware_counts = {}
        else:
            scenario_aware_counts = selected_scenario_counts

    if scenario_aware_counts:
        for scenario_name, exact_required_by_size in scenario_aware_counts.items():
            cumulative_required_by_size: dict[float, int] = {}
            running_required = 0
            for slot_size in sorted(exact_required_by_size.keys(), reverse=True):
                running_required += int(exact_required_by_size.get(slot_size, 0))
                cumulative_required_by_size[slot_size] = running_required

            for slot_size in ordered_sizes:
                exact_required_by_size.setdefault(slot_size, 0)
                cumulative_required_by_size.setdefault(slot_size, 0)

            exact_total = sum(int(value) for value in exact_required_by_size.values())
            summary_rows.append(
                {
                    "Config_ID": config.get("Config_ID", ""),
                    "Method": config.get("Method", ""),
                    "Scenario": config.get("Scenario", ""),
                    "K": config.get("K", ""),
                    "SKU_Scenario": scenario_name,
                    "SKU_Count": str(exact_total),
                    "Required_Locations_Total": str(exact_total),
                    "Exact_Count_Distribution": "|".join(f"{int(size)}:{count}" for size, count in sorted(exact_required_by_size.items())),
                    "Relative_Slot_Size_Distribution": _distribution_value(config),
                }
            )

            for slot_size in ordered_sizes:
                required_count = int(exact_required_by_size.get(slot_size, 0))
                count_rows.append(
                    {
                        "Config_ID": config.get("Config_ID", ""),
                        "Method": config.get("Method", ""),
                        "Scenario": config.get("Scenario", ""),
                        "K": config.get("K", ""),
                        "SKU_Scenario": scenario_name,
                        "SKU_Count": str(exact_total),
                        "Representative_Slot_Size": f"{slot_size:.0f}",
                        "Cluster_Count_Percentage": "",
                        "Assigned_SKUs_At_Representative_Size": str(required_count),
                        "Cumulative_Assigned_SKUs_At_Or_Above_Size": str(cumulative_required_by_size.get(slot_size, 0)),
                        "Min_Required_Locations_At_Or_Above_Size": str(cumulative_required_by_size.get(slot_size, 0)),
                        "Required_Locations_Total": str(exact_total),
                    }
                )

        return summary_rows, count_rows

    active_scenarios = [config_scenario] if config_scenario else list(sku_scenarios.keys())
    for scenario_name in active_scenarios:
        sku_count = sku_scenarios.get(scenario_name, next(iter(sku_scenarios.values()), 0))
        # Allocate the active scenario SKU count across representative slot sizes.
        allocated_counts = common._allocate_counts_from_percentages(sku_count, distributions)
        cumulative_required_by_size: dict[float, int] = {}
        running_required = 0
        for index in range(len(slot_sizes) - 1, -1, -1):
            running_required += allocated_counts[index]
            cumulative_required_by_size[slot_sizes[index]] = running_required

        # Convert cumulative at-or-above constraints to exact counts per slot size.
        exact_required_by_size: dict[float, int] = {}
        for index, slot_size in enumerate(ordered_sizes):
            next_size = ordered_sizes[index + 1] if index + 1 < len(ordered_sizes) else None
            next_required = cumulative_required_by_size.get(next_size, 0) if next_size is not None else 0
            exact_required_by_size[slot_size] = max(cumulative_required_by_size.get(slot_size, 0) - next_required, 0)

        exact_required_by_size = common._enforce_occupied_location_target(exact_required_by_size)
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
                "Relative_Slot_Size_Distribution": _distribution_value(config),
            }
        )

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
    occupied_location_scenarios = {
        "Base_Count": int(common.BASE_OCCUPIED_LOCATIONS_COUNT),
    }

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
