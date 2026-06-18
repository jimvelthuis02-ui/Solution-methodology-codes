import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = ROOT / "Output" / "1_Initial" / "Item_Height_Scenarios_Delta_Weighted.csv"
OUTPUT_DIR = ROOT / "Output" / "1_Initial" / "Slot_Size_Configurations"

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
METHODS = ("quantile_binning", "hierarchical_clustering", "kmeans_clustering")
CLUSTER_COUNTS = (3, 4, 5, 6, 7)


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
    if value is None:
        return ""
    return f"{value:.3f}"


def _read_input_rows() -> list[dict[str, object]]:
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Missing input file: {INPUT_FILE}")

    with INPUT_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("Input CSV has no header row.")
        rows = list(reader)

    required_columns = ["Location", "Slot Height Group", "Location height", "Delta"]
    missing_columns = [column for column in required_columns if column not in reader.fieldnames]
    if missing_columns:
        raise KeyError("Missing required columns: " + ", ".join(missing_columns))

    return rows


def _scenario_values(rows: list[dict[str, object]], scenario_column: str) -> list[tuple[str, str, float]]:
    values: list[tuple[str, str, float]] = []
    for row in rows:
        location = str(row.get("Location", "")).strip()
        group = str(row.get("Slot Height Group", "")).strip()
        value = _to_float(row.get(scenario_column))
        if location == "" or group == "" or value is None:
            continue
        values.append((location, group, value))
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


def _quantile_bins(values: list[float], k: int) -> list[Cluster]:
    sorted_values = sorted(values)
    clusters: list[Cluster] = []
    for index in range(k):
        start = round(index * len(sorted_values) / k)
        end = round((index + 1) * len(sorted_values) / k)
        subset = sorted_values[start:end]
        if subset:
            clusters.append(Cluster(subset))

    # Guard against empty slices caused by tiny inputs.
    while len(clusters) < k and sorted_values:
        clusters.append(Cluster([sorted_values[-1]]))

    return clusters[:k]


def _ward_merge_cost(cluster_a: Cluster, cluster_b: Cluster) -> float:
    size_a = cluster_a.size
    size_b = cluster_b.size
    mean_a = cluster_a.mean_value
    mean_b = cluster_b.mean_value
    return (size_a * size_b / (size_a + size_b)) * (mean_a - mean_b) ** 2


def _hierarchical_clusters(values: list[float], k: int) -> list[Cluster]:
    clusters = [Cluster([value]) for value in sorted(values)]
    if len(clusters) <= k:
        return clusters

    while len(clusters) > k:
        best_index = 0
        best_cost = _ward_merge_cost(clusters[0], clusters[1])
        for index in range(1, len(clusters) - 1):
            cost = _ward_merge_cost(clusters[index], clusters[index + 1])
            if cost < best_cost:
                best_cost = cost
                best_index = index

        merged_values = clusters[best_index].values + clusters[best_index + 1].values
        clusters = (
            clusters[:best_index]
            + [Cluster(sorted(merged_values))]
            + clusters[best_index + 2 :]
        )

    return clusters


def _initial_kmeans_centroids(values: list[float], k: int) -> list[float]:
    sorted_values = sorted(values)
    return [
        _percentile_linear(sorted_values, (index + 0.5) / k)
        for index in range(k)
    ]


def _kmeans_clusters(values: list[float], k: int, max_iterations: int = 100) -> list[Cluster]:
    sorted_values = sorted(values)
    centroids = _initial_kmeans_centroids(sorted_values, k)

    for _ in range(max_iterations):
        assignments: list[list[float]] = [[] for _ in range(k)]
        for value in sorted_values:
            nearest = min(range(k), key=lambda index: abs(value - centroids[index]))
            assignments[nearest].append(value)

        # Re-seed any empty cluster with the farthest point from the largest cluster.
        empty_indices = [index for index, cluster in enumerate(assignments) if not cluster]
        if empty_indices:
            populated = sorted(
                [cluster for cluster in assignments if cluster],
                key=len,
                reverse=True,
            )
            for empty_index in empty_indices:
                donor = populated[0]
                donor_values = sorted(donor)
                moved_value = donor_values[-1]
                donor.remove(moved_value)
                assignments[empty_index].append(moved_value)
                if not donor:
                    populated.pop(0)
                populated.append(assignments[empty_index])

        new_centroids = [sum(cluster) / len(cluster) for cluster in assignments]
        if all(abs(old - new) < 1e-9 for old, new in zip(centroids, new_centroids)):
            centroids = new_centroids
            break
        centroids = new_centroids

    final_clusters = [Cluster(sorted(cluster)) for cluster in assignments if cluster]
    final_clusters.sort(key=lambda cluster: cluster.min_value)
    return final_clusters


def _assignments_for_clusters(values: list[tuple[str, str, float]], clusters: list[Cluster]) -> list[dict[str, object]]:
    cluster_ranges = [
        {
            "cluster_id": index + 1,
            "lower_bound": cluster.min_value,
            "upper_bound": cluster.max_value,
            "representative": cluster.max_value,
            "count": cluster.size,
        }
        for index, cluster in enumerate(clusters)
    ]

    assignments: list[dict[str, object]] = []
    for location, group, value in values:
        cluster_id = None
        representative = None
        lower_bound = None
        upper_bound = None
        for cluster_info in cluster_ranges:
            if cluster_info["lower_bound"] <= value <= cluster_info["upper_bound"]:
                cluster_id = cluster_info["cluster_id"]
                representative = cluster_info["representative"]
                lower_bound = cluster_info["lower_bound"]
                upper_bound = cluster_info["upper_bound"]
                break

        if cluster_id is None:
            # Handle any numerical edge case by choosing the nearest representative.
            nearest = min(cluster_ranges, key=lambda item: abs(value - item["representative"]))
            cluster_id = nearest["cluster_id"]
            representative = nearest["representative"]
            lower_bound = nearest["lower_bound"]
            upper_bound = nearest["upper_bound"]

        assignments.append(
            {
                "Location": location,
                "Slot Height Group": group,
                "Scenario Value": value,
                "Assigned Cluster": cluster_id,
                "Cluster Lower Bound": lower_bound,
                "Cluster Upper Bound": upper_bound,
                "Representative Slot Size": representative,
            }
        )

    return assignments


def generate_slot_size_configurations() -> Path:
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
        "Mean Scenario Value",
    ]
    assignment_fields = [
        "Scenario",
        "Method",
        "K",
        "Location",
        "Slot Height Group",
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
            scenario_values = _scenario_values(rows, scenario_column)
            numeric_values = [value for _, _, value in scenario_values]

            for method in METHODS:
                for k in CLUSTER_COUNTS:
                    if len(numeric_values) < k:
                        continue

                    if method == "quantile_binning":
                        clusters = _quantile_bins(numeric_values, k)
                    elif method == "hierarchical_clustering":
                        clusters = _hierarchical_clusters(numeric_values, k)
                    else:
                        clusters = _kmeans_clusters(numeric_values, k)

                    clusters = sorted(clusters, key=lambda cluster: cluster.min_value)
                    assignments = _assignments_for_clusters(scenario_values, clusters)

                    for index, cluster in enumerate(clusters, start=1):
                        summary_writer.writerow(
                            {
                                "Scenario": scenario_label,
                                "Method": method,
                                "K": k,
                                "Cluster ID": index,
                                "Cluster Count": cluster.size,
                                "Lower Bound": _format_number(cluster.min_value),
                                "Upper Bound": _format_number(cluster.max_value),
                                "Representative Slot Size": _format_number(cluster.max_value),
                                "Mean Scenario Value": _format_number(cluster.mean_value),
                            }
                        )

                    for row in assignments:
                        assignment_writer.writerow(
                            {
                                "Scenario": scenario_label,
                                "Method": method,
                                "K": k,
                                "Location": row["Location"],
                                "Slot Height Group": row["Slot Height Group"],
                                "Scenario Value": _format_number(row["Scenario Value"]),
                                "Assigned Cluster": row["Assigned Cluster"],
                                "Cluster Lower Bound": _format_number(row["Cluster Lower Bound"]),
                                "Cluster Upper Bound": _format_number(row["Cluster Upper Bound"]),
                                "Representative Slot Size": _format_number(row["Representative Slot Size"]),
                            }
                        )

    return OUTPUT_DIR


if __name__ == "__main__":
    output_path = generate_slot_size_configurations()
    print(f"Slot size configuration generation complete. Output written to: {output_path}")
