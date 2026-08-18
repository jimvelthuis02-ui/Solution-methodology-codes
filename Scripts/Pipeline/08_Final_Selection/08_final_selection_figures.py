import csv
import math
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common

INITIAL_SPACE_FILE = common.STAGE1_OUTPUT_DIR / "Current_Layout_Space_Utilization.csv"
INITIAL_PREPARED_FILE = common.STAGE1_OUTPUT_DIR / "Location_Details_Prepared.csv"

FIGURE_INDEX_FIELDS = ["Figure", "Purpose", "Source"]
METRICS = [
    ("Assigned_Locations_Total", "Assigned locations"),
    ("Empty_Locations", "Empty locations"),
    ("Occupancy_Rate", "Occupancy rate"),
    ("Total_Slot_Space_m3", "Total slot space"),
    ("Occupied_Slot_Space_m3", "Occupied slot space"),
    ("Empty_Slot_Space_m3", "Empty slot space"),
    ("Space_Utilization_Pct", "Space utilization"),
    ("Beam_Relocations_Total", "Beam relocations"),
    ("Additional_Beams_Required", "Additional beams"),
    ("Additional_Grids_Required", "Additional grids"),
    ("Standardization_Unique_Slot_Sizes", "Unique slot sizes"),
    ("Additional_Fill_Extra_Slot_Size_Variants", "Extra slot-size variants"),
    ("Additional_Fill_Height_Total_cm", "Additional fill height"),
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        return list(csv.DictReader(source))


def _number(row: dict[str, str], field: str, default: float = 0.0) -> float:
    try:
        return float(str(row.get(field, "")).strip())
    except ValueError:
        return default


def _save(figure, path: Path) -> None:
    figure.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(figure)


def _label(row: dict[str, str]) -> str:
    return f"{row.get('Config_ID', '')} | {row.get('Heuristic_Label', '')}"


def _top_rows(rows: list[dict[str, str]], count: int = 12) -> list[dict[str, str]]:
    return sorted(rows, key=lambda row: (_number(row, "Weighted_Sum_Rank", 10**9), _label(row)))[:count]


def _initial_metrics() -> dict[str, float]:
    return _status_metrics()["Initial"]


def _status_metrics() -> dict[str, dict[str, float]]:
    space_rows = _read_csv(INITIAL_SPACE_FILE)
    prepared_rows = _read_csv(INITIAL_PREPARED_FILE)
    slot_sizes = {
        int(float(row.get("Location height", "0")))
        for row in prepared_rows
        if str(row.get("Location height", "")).strip()
    }
    references: dict[str, dict[str, float]] = {}
    for row in space_rows:
        status = str(row.get("Status_Measurement", "")).strip()
        if status == "Status Initial":
            label = "Initial"
        elif status in {"Status 7-7", "Status 4-8", "Status 11-8"}:
            label = status.replace("Status ", "")
        else:
            continue
        references[label] = {
            "Assigned_Locations_Total": _number(row, "Total_Locations"),
            "Empty_Locations": _number(row, "Empty_Locations"),
            "Occupied_Locations": _number(row, "Occupied_Locations"),
            "Occupancy_Rate": _number(row, "Occupancy_Rate"),
            "Total_Slot_Space_m3": _number(row, "Total_Slot_Space_m3"),
            "Occupied_Slot_Space_m3": _number(row, "Occupied_Slot_Space_m3"),
            "Empty_Slot_Space_m3": _number(row, "Empty_Slot_Space_m3"),
            "Space_Utilization_Pct": _number(row, "Space_Utilization_Pct"),
            "Required_Beams_Total": 305.0,
            "Required_Grids_Total": 949.0,
            "Standardization_Unique_Slot_Sizes": float(len(slot_sizes)),
        }
    return references


COMPARISON_METRICS = [
    ("Assigned_Locations_Total", "Total locations", "max"),
    ("Empty_Locations", "Empty locations", "min"),
    ("Occupied_Locations", "Occupied locations", "max"),
    ("Occupancy_Rate", "Occupancy rate", "max"),
    ("Total_Slot_Space_m3", "Total slot space (m3)", "max"),
    ("Occupied_Slot_Space_m3", "Occupied slot space (m3)", "max"),
    ("Empty_Slot_Space_m3", "Empty slot space (m3)", "min"),
    ("Space_Utilization_Pct", "Space utilization (%)", "max"),
    ("Required_Beams_Total", "Total beams required", "min"),
    ("Required_Grids_Total", "Total grids required", "min"),
    ("Standardization_Unique_Slot_Sizes", "Unique slot sizes", "min"),
]
HEURISTIC_SHORT_LABELS = {
    "Baseline_None": "Baseline",
    "Baseline_LocalSearch": "Baseline + LS",
    "Greedy_None": "Greedy",
    "Greedy_LocalSearch": "Greedy + LS",
    "ConstructiveBeam_None": "Beam-aware",
    "ConstructiveBeam_LocalSearch": "Beam-aware + LS",
}


def _proposed_value(row: dict[str, str], metric: str) -> float:
    if metric == "Required_Beams_Total":
        return _number(row, "Required_Beams_Total_Raw")
    if metric == "Required_Grids_Total":
        return _number(row, "Required_Grids_Total_Raw")
    if metric == "Occupied_Locations":
        return _proposed_value(row, "Assigned_Locations_Total") - _proposed_value(row, "Empty_Locations")
    field = f"{metric}_Raw"
    return _number(row, field) if field in row else _number(row, metric)


def _better_count(values: list[float], original: float, direction: str) -> int:
    if direction == "max":
        return sum(value > original + 1e-9 for value in values)
    return sum(value < original - 1e-9 for value in values)


def _write_original_comparison(rows: list[dict[str, str]], output_dir: Path) -> list[tuple[str, str, str]]:
    references = _status_metrics()
    reference_order = [label for label in ("Initial", "7-7", "4-8", "11-8") if label in references]
    reference_colors = {"Initial": "#dc2626", "7-7": "#d97706", "4-8": "#7c3aed", "11-8": "#059669"}
    entries: list[tuple[str, str, str]] = []
    for figure_number, metrics in enumerate((COMPARISON_METRICS[:6], COMPARISON_METRICS[6:]), start=1):
        figure, axes = plt.subplots(2, 3, figsize=(15, 9))
        axes_flat = list(axes.flat)
        for axis, (metric, title, direction) in zip(axes_flat, metrics):
            values = [_proposed_value(row, metric) for row in rows]
            axis.boxplot(values, positions=[4], widths=0.45, patch_artist=True, boxprops={"facecolor": "#bfdbfe"})
            jitter = [4.0 + ((index % 9) - 4) * 0.012 for index in range(len(values))]
            axis.scatter(jitter, values, s=5, alpha=0.16, color="#1d4ed8")
            for position, label in enumerate(reference_order):
                reference_value = references[label][metric]
                better = _better_count(values, reference_value, direction)
                axis.scatter([position], [reference_value], s=80, color=reference_colors[label], marker="D", zorder=4, label=f"{label} ({better} better)")
            axis.set_title(title, fontsize=10)
            axis.set_xticks(range(len(reference_order) + 1), reference_order + ["Proposed"], rotation=35, ha="right", fontsize=8)
            axis.grid(axis="y", alpha=0.25)
            axis.legend(fontsize=7, loc="best")
        for axis in axes_flat[len(metrics):]:
            axis.set_visible(False)
        figure.suptitle("Original layout versus all proposed layouts", fontsize=15)
        name = f"0{figure_number + 7}_original_vs_proposed_{figure_number}.png"
        _save(figure, output_dir / name)
        entries.append((name, "Original value, proposed-layout distribution, and count better than original", "Candidate_Layout_Metric_Ranking.csv; Stage 1 baseline"))
    return entries


def _write_ranking(rows: list[dict[str, str]], output_dir: Path) -> tuple[str, str, str]:
    selected = _top_rows(rows)
    selected.reverse()
    labels = [_label(row) for row in selected]
    scores = [_number(row, "Weighted_Sum_Score") for row in selected]
    colors = ["#0f766e" if index == len(selected) - 1 else "#94a3b8" for index in range(len(selected))]
    figure, axis = plt.subplots(figsize=(11, 7))
    axis.barh(labels, scores, color=colors)
    axis.set_title("Top weighted-sum configurations")
    axis.set_xlabel("Weighted-sum score")
    axis.grid(axis="x", alpha=0.25)
    _save(figure, output_dir / "01_wsm_ranking.png")
    return "01_wsm_ranking.png", "Top weighted-sum configurations", "Weighted_Sum_Method_Ranking.csv"


def _write_contributions(rows: list[dict[str, str]], output_dir: Path) -> tuple[str, str, str]:
    by_score: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_score[str(row.get("Weighted_Sum_Score", ""))].append(row)
    score_groups = sorted(by_score.items(), key=lambda item: float(item[0]))
    representatives = [group[1][0] for group in score_groups]
    labels = [f"{score} (n={len(group)})" for score, group in score_groups]
    figure, axis = plt.subplots(figsize=(22, max(9, len(labels) * 0.11)))
    left = [0.0] * len(representatives)
    palette = plt.cm.tab20.colors
    for index, (metric, title) in enumerate(METRICS):
        values = [_number(row, f"{metric}_Weighted") for row in representatives]
        axis.barh(labels, values, left=left, label=title, color=palette[index % len(palette)])
        left = [current + value for current, value in zip(left, values)]
    axis.set_title("Weighted-score contribution breakdown for every unique score")
    axis.set_xlabel("Contribution to weighted-sum score")
    axis.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8)
    axis.grid(axis="x", alpha=0.2)
    _save(figure, output_dir / "02_weighted_contributions.png")
    contribution_file = output_dir / "Weighted_Contribution_By_Unique_Score.csv"
    fields = ["Weighted_Sum_Score", "Configuration_Count", "Config_IDs"]
    fields.extend(f"{metric}_Weighted" for metric, _title in METRICS)
    with contribution_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for score, group in score_groups:
            representative = group[0]
            writer.writerow(
                {
                    "Weighted_Sum_Score": score,
                    "Configuration_Count": len(group),
                    "Config_IDs": "|".join(sorted(str(row.get("Config_ID", "")) for row in group)),
                    **{f"{metric}_Weighted": representative.get(f"{metric}_Weighted", "") for metric, _title in METRICS},
                }
            )
    return "02_weighted_contributions.png", "Weighted contributions for every unique score; n shows configurations sharing a score", "Weighted_Sum_Method_Ranking.csv; Weighted_Contribution_By_Unique_Score.csv"


def _write_beam_grid_tradeoff(rows: list[dict[str, str]], output_dir: Path) -> tuple[str, str, str]:
    figure, axis = plt.subplots(figsize=(10, 7))
    labels_seen: set[str] = set()
    for row in rows:
        label = str(row.get("Heuristic_Label", ""))
        axis.scatter(_number(row, "Additional_Beams_Required_Raw"), _number(row, "Additional_Grids_Required_Raw"), label=label, alpha=0.65, s=32)
        if label not in labels_seen:
            labels_seen.add(label)
    axis.set_title("Additional beam versus grid requirements")
    axis.set_xlabel("Additional beams required")
    axis.set_ylabel("Additional grids required")
    axis.legend(fontsize=8)
    axis.grid(alpha=0.25)
    _save(figure, output_dir / "03_beam_grid_tradeoff.png")
    return "03_beam_grid_tradeoff.png", "Material trade-off between additional beams and grids", "Weighted_Sum_Method_Ranking.csv"


def _write_pareto(rows: list[dict[str, str]], output_dir: Path) -> tuple[str, str, str]:
    points = [(_number(row, "Additional_Beams_Required_Raw"), _number(row, "Additional_Grids_Required_Raw"), row) for row in rows]
    frontier = []
    for x, y, row in points:
        if not any(other_x <= x and other_y <= y and (other_x < x or other_y < y) for other_x, other_y, _ in points):
            frontier.append((x, y, row))
    frontier.sort(key=lambda point: (point[0], point[1]))
    figure, axis = plt.subplots(figsize=(10, 7))
    axis.scatter([point[0] for point in points], [point[1] for point in points], color="#cbd5e1", alpha=0.45, s=28, label="Other configurations")
    axis.scatter([point[0] for point in frontier], [point[1] for point in frontier], color="#dc2626", s=42, label="Pareto-efficient")
    if frontier:
        axis.plot([point[0] for point in frontier], [point[1] for point in frontier], color="#dc2626", alpha=0.6)
    axis.set_title("Pareto frontier: additional beams and grids")
    axis.set_xlabel("Additional beams required")
    axis.set_ylabel("Additional grids required")
    axis.legend()
    axis.grid(alpha=0.25)
    _save(figure, output_dir / "04_pareto_beams_grids.png")
    return "04_pareto_beams_grids.png", "Pareto-efficient material configurations", "Weighted_Sum_Method_Ranking.csv"


def _write_normalized_heatmap(rows: list[dict[str, str]], output_dir: Path) -> tuple[str, str, str]:
    selected = _top_rows(rows, 15)
    values = [[_number(row, f"{metric}_Normalized") for metric, _ in METRICS] for row in selected]
    figure, axis = plt.subplots(figsize=(15, 8))
    cmap = LinearSegmentedColormap.from_list("wsm", ["#fee2e2", "#fef3c7", "#dcfce7"])
    image = axis.imshow(values, aspect="auto", cmap=cmap, vmin=0, vmax=1)
    axis.set_title("Normalized metric profile of top configurations")
    axis.set_yticks(range(len(selected)), [_label(row) for row in selected], fontsize=8)
    axis.set_xticks(range(len(METRICS)), [title for _, title in METRICS], rotation=45, ha="right")
    figure.colorbar(image, ax=axis, label="Normalized score")
    _save(figure, output_dir / "05_normalized_metric_heatmap.png")
    return "05_normalized_metric_heatmap.png", "Normalized metric comparison for top configurations", "Weighted_Sum_Method_Ranking.csv"


def _write_heuristic_comparison(rows: list[dict[str, str]], output_dir: Path) -> tuple[str, str, str]:
    labels = sorted({str(row.get("Heuristic_Label", "")) for row in rows})
    short_labels = [HEURISTIC_SHORT_LABELS.get(label, label) for label in labels]
    metrics = [("Weighted_Sum_Score", "WSM score"), ("Beam_Relocations_Total_Raw", "Beam relocations"), ("Space_Utilization_Pct_Raw", "Space utilization (%)"), ("Occupancy_Rate_Raw", "Occupancy rate")]
    figure, axes = plt.subplots(2, 2, figsize=(15, 10))
    for axis, (field, title) in zip(axes.flat, metrics):
        values_by_label = [[_number(row, field) for row in rows if row.get("Heuristic_Label") == label] for label in labels]
        axis.boxplot(values_by_label, tick_labels=short_labels, patch_artist=True, boxprops={"facecolor": "#99f6e4"}, medianprops={"color": "#b91c1c", "linewidth": 2})
        axis.set_title(title)
        axis.tick_params(axis="x", rotation=35, labelsize=8)
        axis.grid(axis="y", alpha=0.25)
    figure.suptitle("Distribution of results by heuristic", fontsize=15)
    _save(figure, output_dir / "06_heuristic_comparison.png")
    return "06_heuristic_comparison.png", "Readable distributions of WSM score, implementation, space, and occupancy by heuristic", "Weighted_Sum_Method_Ranking.csv"


def _write_layout_heatmap(rows: list[dict[str, str]], output_dir: Path) -> tuple[str, str, str] | None:
    if not rows:
        return None
    selected_id = min(rows, key=lambda row: _number(row, "Weighted_Sum_Rank", 10**9)).get("Config_ID", "")
    locations_path = common.OUTPUT_ROOT / "08_Final_Selection_Comparison_AllHeuristics" / "Final_Layout_By_Location.csv"
    if not locations_path.exists():
        locations_path = common.STAGE8_OUTPUT_DIR / "Final_Layout_By_Location.csv"
    if not locations_path.exists():
        return None
    locations = [row for row in _read_csv(locations_path) if row.get("Config_ID") == selected_id]
    racks = sorted({row.get("Rack", "") for row in locations})
    matrix = [[math.nan for _ in range(22)] for _ in racks]
    rack_index = {rack: index for index, rack in enumerate(racks)}
    for row in locations:
        try:
            column = int(row.get("Column", ""))
        except ValueError:
            continue
        if row.get("Usable_Location") == "YES":
            matrix[rack_index[row.get("Rack", "")]][column] = float(row.get("Assigned_Slot_Size_cm") or 0)
    figure, axis = plt.subplots(figsize=(13, 6))
    image = axis.imshow(matrix, aspect="auto", cmap="viridis")
    axis.set_title(f"Storage layout heatmap: {selected_id}")
    axis.set_xlabel("Rack column")
    axis.set_ylabel("Rack")
    axis.set_xticks(range(22), [f"{value:02d}" for value in range(22)])
    axis.set_yticks(range(len(racks)), racks)
    figure.colorbar(image, ax=axis, label="Assigned slot size (cm)")
    _save(figure, output_dir / "07_top_layout_heatmap.png")
    return "07_top_layout_heatmap.png", f"Assigned slot sizes by rack and column for {selected_id}", str(locations_path.name)


def generate_figures(ranking_file: Path, output_dir: Path) -> list[Path]:
    rows = _read_csv(ranking_file)
    if not rows:
        raise ValueError(f"No rows available for figures: {ranking_file}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_name in (
        "01_wsm_ranking.png",
        "03_beam_grid_tradeoff.png",
        "04_pareto_beams_grids.png",
        "05_normalized_metric_heatmap.png",
        "07_top_layout_heatmap.png",
    ):
        old_path = output_dir / old_name
        if old_path.exists():
            old_path.unlink()
    entries = [
        _write_contributions(rows, output_dir),
        _write_heuristic_comparison(rows, output_dir),
    ]
    entries.extend(_write_original_comparison(rows, output_dir))
    index_path = output_dir / "Figure_Index.csv"
    with index_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=FIGURE_INDEX_FIELDS)
        writer.writeheader()
        writer.writerows({"Figure": name, "Purpose": purpose, "Source": source} for name, purpose, source in entries)
    return [output_dir / name for name, _purpose, _source in entries]


if __name__ == "__main__":
    source = common.OUTPUT_ROOT / "08_Final_Selection_Comparison_AllHeuristics" / "Weighted_Sum_Method_Ranking.csv"
    destination = common.OUTPUT_ROOT / "08_Final_Selection_Comparison_AllHeuristics" / "Figures"
    generated = generate_figures(source, destination)
    print(f"Generated {len(generated)} figures in {destination}")
