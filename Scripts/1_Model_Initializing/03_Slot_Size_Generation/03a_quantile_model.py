import csv
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT_FILE = ROOT / "Output" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"
OUTPUT_DIR = ROOT / "Output" / "03_Slot_Size_Generation" / "quantile_binning"

SCENARIO_COLUMNS = [
    "Scenario_1_Item_Height",
    "Scenario_2_Item_Height",
    "Scenario_3_Item_Height",
    "Scenario_4_Item_Height",
    "Scenario_5_Item_Height",
    "Scenario_6_Item_Height",
]
SCENARIO_LABELS = {
    "Scenario_1_Item_Height": "Scenario 1",
    "Scenario_2_Item_Height": "Scenario 2",
    "Scenario_3_Item_Height": "Scenario 3",
    "Scenario_4_Item_Height": "Scenario 4",
    "Scenario_5_Item_Height": "Scenario 5",
    "Scenario_6_Item_Height": "Scenario 6",
}
CLUSTER_COUNTS = (3, 4, 5, 6, 7)
CLEARANCE_CM = 5.0


def _to_float(value: object | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _format_number(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def _round_up_to_next_4_or_9(value: float) -> float:
    candidate = math.ceil(value)
    while candidate % 10 not in (4, 9):
        candidate += 1
    return float(candidate)


def _read_input_rows() -> list[dict[str, str]]:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")

    with INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Input CSV has no header row.")
        return list(reader)


def _scenario_values(rows: list[dict[str, str]], scenario_column: str) -> list[tuple[str, float]]:
    values: list[tuple[str, float]] = []
    for row in rows:
        location = str(row.get("Location", "")).strip()
        value = _to_float(row.get(scenario_column))
        if location and value is not None:
            values.append((location, value))
    return values


def _natural_cut_starts(numeric: list[float], k: int) -> list[int]:
    n = len(numeric)
    transitions = [j for j in range(1, n) if numeric[j] != numeric[j - 1]]
    cut_starts: list[int] = [0]
    used: set[int] = set()

    for i in range(1, k):
        ideal = i * n / k
        available = [t for t in transitions if t not in used]
        if available:
            nearest = min(available, key=lambda t: abs(t - ideal))
            used.add(nearest)
            cut_starts.append(nearest)
        else:
            cut_starts.append(int(ideal))

    cut_starts.sort()
    cut_starts.append(n)
    return cut_starts


def generate_quantile_model() -> Path:
    rows = _read_input_rows()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    summary_file = OUTPUT_DIR / "Slot_Size_Configuration_Summary.csv"
    assignments_file = OUTPUT_DIR / "Slot_Size_Configuration_Assignments.csv"

    summary_fields = [
        "Scenario",
        "Method",
        "K",
        "Cluster ID",
        "Cluster Count",
        "Lower Bound",
        "Upper Bound",
        "Representative Slot Size",
        "Mean Item Height",
    ]
    assignment_fields = [
        "Scenario",
        "Method",
        "K",
        "Location",
        "Scenario Value",
        "Assigned Cluster",
        "Cluster Lower Bound",
        "Cluster Upper Bound",
        "Representative Slot Size",
    ]

    with summary_file.open("w", newline="", encoding="utf-8") as summary_target, assignments_file.open(
        "w", newline="", encoding="utf-8"
    ) as assignment_target:
        summary_writer = csv.DictWriter(summary_target, fieldnames=summary_fields)
        assignment_writer = csv.DictWriter(assignment_target, fieldnames=assignment_fields)
        summary_writer.writeheader()
        assignment_writer.writeheader()

        for scenario_column in SCENARIO_COLUMNS:
            scenario_label = SCENARIO_LABELS[scenario_column]
            scenario_values = sorted(_scenario_values(rows, scenario_column), key=lambda item: item[1])
            numeric = [value for _, value in scenario_values]

            for k in CLUSTER_COUNTS:
                if len(numeric) < k:
                    continue

                cut_starts = _natural_cut_starts(numeric, k)
                for index in range(k):
                    start = cut_starts[index]
                    end = cut_starts[index + 1]
                    bucket = scenario_values[start:end]
                    if not bucket:
                        continue

                    bucket_values = [value for _, value in bucket]
                    lower_bound = min(bucket_values)
                    upper_bound = max(bucket_values)
                    slot_size = _round_up_to_next_4_or_9(upper_bound + CLEARANCE_CM)
                    mean_value = sum(bucket_values) / len(bucket_values)
                    cluster_id = index + 1

                    summary_writer.writerow(
                        {
                            "Scenario": scenario_label,
                            "Method": "quantile_binning",
                            "K": k,
                            "Cluster ID": cluster_id,
                            "Cluster Count": len(bucket),
                            "Lower Bound": _format_number(lower_bound),
                            "Upper Bound": _format_number(upper_bound),
                            "Representative Slot Size": _format_number(slot_size),
                            "Mean Item Height": _format_number(mean_value),
                        }
                    )

                    for location, value in bucket:
                        assignment_writer.writerow(
                            {
                                "Scenario": scenario_label,
                                "Method": "quantile_binning",
                                "K": k,
                                "Location": location,
                                "Scenario Value": _format_number(value),
                                "Assigned Cluster": cluster_id,
                                "Cluster Lower Bound": _format_number(lower_bound),
                                "Cluster Upper Bound": _format_number(upper_bound),
                                "Representative Slot Size": _format_number(slot_size),
                            }
                        )

    return OUTPUT_DIR


if __name__ == "__main__":
    output_path = generate_quantile_model()
    print(f"Quantile slot-size model complete. Output written to: {output_path}")
