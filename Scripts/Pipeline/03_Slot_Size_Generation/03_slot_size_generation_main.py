import csv
import math
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
INPUT_FILE = ROOT / "Output" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"
OUTPUT_DIR = ROOT / "Output" / "03_Slot_Size_Generation"
MERGED_SUMMARY_FILE = OUTPUT_DIR / "Stage3_Slot_Size_Configuration_Summary_All.csv"

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
MAX_REPRESENTATIVE_SLOT_SIZE_CM = 234.0


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
    # Align generated slot sizes to operationally allowed endings (..4 / ..9).
    candidate = math.ceil(value)
    while candidate % 10 not in (4, 9):
        candidate += 1
    return float(min(candidate, int(MAX_REPRESENTATIVE_SLOT_SIZE_CM)))


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
    # Build bucket boundaries close to ideal quantiles while respecting value changes.
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


def _ward_merge_cost(cluster_a: Cluster, cluster_b: Cluster) -> float:
    # Ward criterion: increase in within-cluster variance if two clusters are merged.
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

        merged = sorted(clusters[best_index].values + clusters[best_index + 1].values)
        clusters = clusters[:best_index] + [Cluster(merged)] + clusters[best_index + 2 :]

    return clusters


def _percentile_linear(values: list[float], percentile: float) -> float:
    # Linear interpolation percentile to seed deterministic initial centroids.
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


def _summary_rows_from_clusters(
    values: list[tuple[str, float]],
    clusters: list[Cluster],
    method_name: str,
    scenario_label: str,
    k: int,
) -> list[dict[str, str]]:
    total_count = len(values)
    rows: list[dict[str, str]] = []

    for cluster_id, cluster in enumerate(sorted(clusters, key=lambda c: c.min_value), start=1):
        cluster_count = cluster.size
        slot_size = _round_up_to_next_4_or_9(cluster.max_value + CLEARANCE_CM)
        rows.append(
            {
                "Scenario": scenario_label,
                "Method": method_name,
                "K": str(k),
                "Cluster ID": str(cluster_id),
                "Cluster Count": str(cluster_count),
                "Cluster Count Percentage": f"{(cluster_count / total_count) * 100:.2f}%",
                "Lower Bound": _format_number(cluster.min_value),
                "Upper Bound": _format_number(cluster.max_value),
                "Representative Slot Size": _format_number(slot_size),
            }
        )

    return rows


def _generate_quantile_summary_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    merged_rows: list[dict[str, str]] = []
    for scenario_column in SCENARIO_COLUMNS:
        scenario_label = SCENARIO_LABELS[scenario_column]
        scenario_values = sorted(_scenario_values(rows, scenario_column), key=lambda item: item[1])
        numeric = [value for _, value in scenario_values]

        for k in CLUSTER_COUNTS:
            if len(numeric) < k:
                continue

            cut_starts = _natural_cut_starts(numeric, k)
            total_count = len(scenario_values)
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
                cluster_count = len(bucket)

                merged_rows.append(
                    {
                        "Scenario": scenario_label,
                        "Method": "quantile_binning",
                        "K": str(k),
                        "Cluster ID": str(index + 1),
                        "Cluster Count": str(cluster_count),
                        "Cluster Count Percentage": f"{(cluster_count / total_count) * 100:.2f}%",
                        "Lower Bound": _format_number(lower_bound),
                        "Upper Bound": _format_number(upper_bound),
                        "Representative Slot Size": _format_number(slot_size),
                    }
                )

    return merged_rows


def _generate_hierarchical_summary_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    merged_rows: list[dict[str, str]] = []
    for scenario_column in SCENARIO_COLUMNS:
        scenario_label = SCENARIO_LABELS[scenario_column]
        values = _scenario_values(rows, scenario_column)
        numeric = [value for _, value in values]

        for k in CLUSTER_COUNTS:
            if len(numeric) < k:
                continue
            clusters = _hierarchical_clusters(numeric, k)
            merged_rows.extend(
                _summary_rows_from_clusters(values, clusters, "hierarchical_clustering", scenario_label, k)
            )

    return merged_rows


def _generate_kmeans_summary_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    merged_rows: list[dict[str, str]] = []
    for scenario_column in SCENARIO_COLUMNS:
        scenario_label = SCENARIO_LABELS[scenario_column]
        values = _scenario_values(rows, scenario_column)
        numeric = [value for _, value in values]

        for k in CLUSTER_COUNTS:
            if len(numeric) < k:
                continue
            clusters = _kmeans_clusters(numeric, k)
            merged_rows.extend(
                _summary_rows_from_clusters(values, clusters, "kmeans_clustering", scenario_label, k)
            )

    return merged_rows


def _cleanup_legacy_outputs() -> None:
    # Remove any previous per-method summary/assignment artifacts.
    for filename in [
        "Quantile_Slot_Size_Configuration_Summary.csv",
        "Hierarchical_Slot_Size_Configuration_Summary.csv",
        "KMeans_Slot_Size_Configuration_Summary.csv",
        "Quantile_Slot_Size_Configuration_Assignments.csv",
        "Hierarchical_Slot_Size_Configuration_Assignments.csv",
        "KMeans_Slot_Size_Configuration_Assignments.csv",
    ]:
        path = OUTPUT_DIR / filename
        if path.exists():
            path.unlink()


def run_slot_size_generation() -> Path:
    """Run Stage 3 slot-size generation for all methods and write one merged summary."""
    rows = _read_input_rows()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    merged_rows: list[dict[str, str]] = []
    merged_rows.extend(_generate_quantile_summary_rows(rows))
    merged_rows.extend(_generate_hierarchical_summary_rows(rows))
    merged_rows.extend(_generate_kmeans_summary_rows(rows))

    fieldnames = [
        "Scenario",
        "Method",
        "K",
        "Cluster ID",
        "Cluster Count",
        "Cluster Count Percentage",
        "Lower Bound",
        "Upper Bound",
        "Representative Slot Size",
    ]

    with MERGED_SUMMARY_FILE.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(merged_rows)

    _cleanup_legacy_outputs()
    return MERGED_SUMMARY_FILE


if __name__ == "__main__":
    output_file = run_slot_size_generation()
    print(f"Merged Stage 3 summary written to: {output_file}")
    print("Slot-size method generation complete.")
