import csv
import math
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT_FILE = ROOT / "Output" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"
OUTPUT_DIR = ROOT / "Output" / "03_Slot_Size_Generation" / "kmeans_clustering"

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


@dataclass(frozen=True)
class Cluster:
    values: list[float]

    @property
    def min_value(self) -> float:
        return min(self.values)

    @property
    def max_value(self) -> float:
        return max(self.values)

    @property
    def mean_value(self) -> float:
        return sum(self.values) / len(self.values)

    @property
    def size(self) -> int:
        return len(self.values)


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


def _percentile_linear(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("Cannot compute percentile from an empty list.")
    if len(values) == 1:
        return values[0]

    sorted_values = sorted(values)
    rank = (len(sorted_values) - 1) * percentile
    lower_index = int(math.floor(rank))
    upper_index = int(math.ceil(rank))
    if lower_index == upper_index:
        return sorted_values[lower_index]

    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    weight = rank - lower_index
    return lower_value + (upper_value - lower_value) * weight


def _initial_kmeans_centroids(values: list[float], k: int) -> list[float]:
    sorted_values = sorted(values)
    return [_percentile_linear(sorted_values, (index + 0.5) / k) for index in range(k)]


def _kmeans_clusters(values: list[float], k: int, max_iterations: int = 100) -> list[Cluster]:
    sorted_values = sorted(values)
    centroids = _initial_kmeans_centroids(sorted_values, k)

    for _ in range(max_iterations):
        assignments: list[list[float]] = [[] for _ in range(k)]
        for value in sorted_values:
            nearest = min(range(k), key=lambda index: abs(value - centroids[index]))
            assignments[nearest].append(value)

        empty_indices = [index for index, cluster in enumerate(assignments) if not cluster]
        if empty_indices:
            populated = sorted([cluster for cluster in assignments if cluster], key=len, reverse=True)
            for empty_index in empty_indices:
                donor = populated[0]
                moved_value = max(donor)
                donor.remove(moved_value)
                assignments[empty_index].append(moved_value)
                if not donor:
                    populated.pop(0)
                populated.append(assignments[empty_index])

        new_centroids = [sum(cluster) / len(cluster) for cluster in assignments]
        if all(abs(old - new) < 1e-9 for old, new in zip(centroids, new_centroids)):
            break
        centroids = new_centroids

    clusters = [Cluster(sorted(cluster)) for cluster in assignments if cluster]
    clusters.sort(key=lambda cluster: cluster.min_value)
    return clusters


def _assignments(values: list[tuple[str, float]], clusters: list[Cluster]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    summary_rows: list[dict[str, object]] = []
    assignment_rows: list[dict[str, object]] = []

    for index, cluster in enumerate(clusters, start=1):
        slot_size = _round_up_to_next_4_or_9(cluster.max_value + CLEARANCE_CM)
        summary_rows.append(
            {
                "Cluster ID": index,
                "Cluster Count": cluster.size,
                "Lower Bound": cluster.min_value,
                "Upper Bound": cluster.max_value,
                "Representative Slot Size": slot_size,
                "Mean Item Height": cluster.mean_value,
            }
        )

    for location, value in values:
        assigned = None
        for row in summary_rows:
            lower = _to_float(row.get("Lower Bound"))
            upper = _to_float(row.get("Upper Bound"))
            if lower is None or upper is None:
                continue
            if lower <= value <= upper:
                assigned = row
                break
        if assigned is None:
            def _distance_to_representative(row: dict[str, object]) -> float:
                representative = _to_float(row.get("Representative Slot Size"))
                if representative is None:
                    return float("inf")
                return abs(value - representative)

            assigned = min(summary_rows, key=_distance_to_representative)

        assignment_rows.append(
            {
                "Location": location,
                "Scenario Value": value,
                "Assigned Cluster": assigned["Cluster ID"],
                "Cluster Lower Bound": assigned["Lower Bound"],
                "Cluster Upper Bound": assigned["Upper Bound"],
                "Representative Slot Size": assigned["Representative Slot Size"],
            }
        )

    return summary_rows, assignment_rows


def generate_kmeans_model() -> Path:
    rows = _read_input_rows()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    summary_file = OUTPUT_DIR / "Slot_Size_Configuration_Summary.csv"
    assignments_file = OUTPUT_DIR / "Slot_Size_Configuration_Assignments.csv"

    summary_fields = ["Scenario", "Method", "K", "Cluster ID", "Cluster Count", "Lower Bound", "Upper Bound", "Representative Slot Size", "Mean Item Height"]
    assignment_fields = ["Scenario", "Method", "K", "Location", "Scenario Value", "Assigned Cluster", "Cluster Lower Bound", "Cluster Upper Bound", "Representative Slot Size"]

    with summary_file.open("w", newline="", encoding="utf-8") as summary_target, assignments_file.open("w", newline="", encoding="utf-8") as assignment_target:
        summary_writer = csv.DictWriter(summary_target, fieldnames=summary_fields)
        assignment_writer = csv.DictWriter(assignment_target, fieldnames=assignment_fields)
        summary_writer.writeheader()
        assignment_writer.writeheader()

        for scenario_column in SCENARIO_COLUMNS:
            scenario_label = SCENARIO_LABELS[scenario_column]
            values = _scenario_values(rows, scenario_column)
            numeric = [value for _, value in values]

            for k in CLUSTER_COUNTS:
                if len(numeric) < k:
                    continue

                clusters = _kmeans_clusters(numeric, k)
                summary_rows, assignment_rows = _assignments(values, clusters)

                for row in summary_rows:
                    summary_writer.writerow(
                        {
                            "Scenario": scenario_label,
                            "Method": "kmeans_clustering",
                            "K": k,
                            "Cluster ID": row["Cluster ID"],
                            "Cluster Count": row["Cluster Count"],
                            "Lower Bound": _format_number(_to_float(row.get("Lower Bound"))),
                            "Upper Bound": _format_number(_to_float(row.get("Upper Bound"))),
                            "Representative Slot Size": _format_number(_to_float(row.get("Representative Slot Size"))),
                            "Mean Item Height": _format_number(_to_float(row.get("Mean Item Height"))),
                        }
                    )

                for row in assignment_rows:
                    assignment_writer.writerow(
                        {
                            "Scenario": scenario_label,
                            "Method": "kmeans_clustering",
                            "K": k,
                            "Location": row["Location"],
                            "Scenario Value": _format_number(_to_float(row.get("Scenario Value"))),
                            "Assigned Cluster": row["Assigned Cluster"],
                            "Cluster Lower Bound": _format_number(_to_float(row.get("Cluster Lower Bound"))),
                            "Cluster Upper Bound": _format_number(_to_float(row.get("Cluster Upper Bound"))),
                            "Representative Slot Size": _format_number(_to_float(row.get("Representative Slot Size"))),
                        }
                    )

    return OUTPUT_DIR


if __name__ == "__main__":
    output_path = generate_kmeans_model()
    print(f"K-means slot-size model complete. Output written to: {output_path}")
