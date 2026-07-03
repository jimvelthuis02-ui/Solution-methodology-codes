import csv
from collections import defaultdict
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


OUTPUT_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
MAX_CANDIDATE_CONFIGURATIONS = 30
NEAR_SLOT_TOLERANCE_CM = 5.0
NEAR_DISTRIBUTION_TOLERANCE = 0.05
FAMILY_SLOT_TOLERANCE_CM = 12.0
FAMILY_DISTRIBUTION_TOLERANCE = 0.15
FAMILY_MEAN_TOLERANCE_CM = 8.0


def _read_stage3_rows() -> list[dict[str, str]]:
    """Read slot-size summaries from all Stage 3 clustering methods."""
    rows: list[dict[str, str]] = []
    method_summary_files = {
        "quantile_binning": "Quantile_Slot_Size_Configuration_Summary.csv",
        "hierarchical_clustering": "Hierarchical_Slot_Size_Configuration_Summary.csv",
        "kmeans_clustering": "KMeans_Slot_Size_Configuration_Summary.csv",
    }
    for method in common.METHODS:
        preferred = common.SLOT_SIZE_ROOT / method / method_summary_files.get(method, "Slot_Size_Configuration_Summary.csv")
        legacy = common.SLOT_SIZE_ROOT / method / "Slot_Size_Configuration_Summary.csv"
        path = preferred if preferred.exists() else legacy
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            rows.extend(list(reader))
    return rows


def _write_csv_preserve(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _to_float(value: object | None) -> float | None:
    # Delegate to shared parser so numeric behavior is consistent across stages.
    return common._to_float(value)


def _parse_percent(value: object | None) -> float:
    # Accept values like "12.3%" or "0.123" and normalize to 0-1 range.
    text = str(value).strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    parsed = _to_float(text)
    return 0.0 if parsed is None else parsed / 100.0 if parsed > 1.0 else parsed


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    parsed = _to_float(value)
    return default if parsed is None else parsed


def _candidate_signature(slot_sizes: list[float], distributions: list[float]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    # Exact signature for strict deduplication.
    size_signature = tuple(int(round(size)) for size in slot_sizes)
    dist_signature = tuple(int(round(value * 1000)) for value in distributions)
    return size_signature, dist_signature


def _near_signature(slot_sizes: list[float], distributions: list[float]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    # Relaxed signature to merge near-identical candidates.
    size_signature = tuple(int(round(size / NEAR_SLOT_TOLERANCE_CM)) for size in slot_sizes)
    dist_signature = tuple(int(round(value / NEAR_DISTRIBUTION_TOLERANCE)) for value in distributions)
    return size_signature, dist_signature


def _candidate_metrics(slot_sizes: list[float], distributions: list[float]) -> dict[str, float]:
    # Compute ranking/pruning metrics used later in tie-breaking.
    if not slot_sizes:
        return {
            "Mean_Slot_Size": 0.0,
            "Slot_Size_Spread": 0.0,
            "Distinct_Slot_Count": 0.0,
            "Weighted_Distribution_Spread": 0.0,
        }

    weighted_mean = sum(size * weight for size, weight in zip(slot_sizes, distributions))
    spread = max(slot_sizes) - min(slot_sizes)
    weighted_spread = sum(abs(size - weighted_mean) * weight for size, weight in zip(slot_sizes, distributions))
    return {
        "Mean_Slot_Size": weighted_mean,
        "Slot_Size_Spread": spread,
        "Distinct_Slot_Count": float(len(slot_sizes)),
        "Weighted_Distribution_Spread": weighted_spread,
    }


def _is_dominated(candidate: dict[str, object], others: list[dict[str, object]]) -> bool:
    """Return True when another candidate is equal-or-better on all key metrics."""
    candidate_sizes = candidate.get("Slot_Sizes", [])
    if not isinstance(candidate_sizes, list):
        return False

    candidate_mean = _as_float(candidate.get("Mean_Slot_Size", 0.0))
    candidate_spread = _as_float(candidate.get("Slot_Size_Spread", 0.0))
    candidate_distinct = _as_float(candidate.get("Distinct_Slot_Count", 0.0))
    candidate_weighted_spread = _as_float(candidate.get("Weighted_Distribution_Spread", 0.0))

    for other in others:
        if other is candidate:
            continue
        other_sizes = other.get("Slot_Sizes", [])
        if not isinstance(other_sizes, list):
            continue
        if len(other_sizes) != len(candidate_sizes):
            continue

        if all(other_size <= candidate_size + 1e-9 for other_size, candidate_size in zip(other_sizes, candidate_sizes)):
            other_mean = _as_float(other.get("Mean_Slot_Size", 0.0))
            other_spread = _as_float(other.get("Slot_Size_Spread", 0.0))
            other_distinct = _as_float(other.get("Distinct_Slot_Count", 0.0))
            other_weighted_spread = _as_float(other.get("Weighted_Distribution_Spread", 0.0))
            if (
                other_mean <= candidate_mean + 1e-9
                and other_spread <= candidate_spread + 1e-9
                and other_distinct <= candidate_distinct + 1e-9
                and other_weighted_spread <= candidate_weighted_spread + 1e-9
                and (
                    other_mean < candidate_mean - 1e-9
                    or other_spread < candidate_spread - 1e-9
                    or other_weighted_spread < candidate_weighted_spread - 1e-9
                )
            ):
                return True

    return False


def _to_int(value: object | None, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _candidate_score(candidate: dict[str, object]) -> tuple[float, float, float, float, str, int]:
    return (
        _as_float(candidate.get("Weighted_Distribution_Spread", 0.0)),
        _as_float(candidate.get("Slot_Size_Spread", 0.0)),
        _as_float(candidate.get("Mean_Slot_Size", 0.0)),
        _as_float(candidate.get("Distinct_Slot_Count", 0.0)),
        str(candidate.get("Method", "")),
        _to_int(candidate.get("K"), 0),
    )


def _candidate_sort_key(candidate: dict[str, object]) -> tuple[str, int, float, float, float]:
    return (
        str(candidate.get("Method", "")),
        _to_int(candidate.get("K"), 0),
        _as_float(candidate.get("Mean_Slot_Size", 0.0)),
        _as_float(candidate.get("Slot_Size_Spread", 0.0)),
        _as_float(candidate.get("Weighted_Distribution_Spread", 0.0)),
    )


def _is_family_similar(candidate: dict[str, object], anchor: dict[str, object]) -> bool:
    candidate_sizes = candidate.get("Slot_Sizes", [])
    candidate_dist = candidate.get("Relative_Slot_Size_Distribution", [])
    anchor_sizes = anchor.get("Slot_Sizes", [])
    anchor_dist = anchor.get("Relative_Slot_Size_Distribution", [])

    if not isinstance(candidate_sizes, list) or not isinstance(candidate_dist, list):
        return False
    if not isinstance(anchor_sizes, list) or not isinstance(anchor_dist, list):
        return False
    if len(candidate_sizes) != len(anchor_sizes):
        return False

    slot_diffs = [abs(_as_float(a) - _as_float(b)) for a, b in zip(candidate_sizes, anchor_sizes)]
    if any(diff > FAMILY_SLOT_TOLERANCE_CM for diff in slot_diffs):
        return False

    dist_diffs = [abs(_as_float(a) - _as_float(b)) for a, b in zip(candidate_dist, anchor_dist)]
    if any(diff > FAMILY_DISTRIBUTION_TOLERANCE for diff in dist_diffs):
        return False

    mean_diff = abs(_as_float(candidate.get("Mean_Slot_Size", 0.0)) - _as_float(anchor.get("Mean_Slot_Size", 0.0)))
    if mean_diff > FAMILY_MEAN_TOLERANCE_CM:
        return False

    return True


def build_candidate_configuration_filtering() -> Path:
    """Reduce Stage 3 solution space via family representatives and dominance pruning."""
    stage3_rows = _read_stage3_rows()
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    # Build one raw candidate from each method/scenario/K combination.
    for row in stage3_rows:
        method = str(row.get("Method", "")).strip()
        scenario = str(row.get("Scenario", "")).strip()
        k = str(row.get("K", "")).strip()
        if method and scenario and k:
            grouped[(method, scenario, k)].append(row)

    candidate_rows: list[dict[str, object]] = []
    for (method, scenario, k), rows in grouped.items():
        ordered_rows = sorted(rows, key=lambda row: _to_float(row.get("Representative Slot Size")) or 0.0)
        slot_sizes = [(_to_float(row.get("Representative Slot Size")) or 0.0) for row in ordered_rows]
        distributions = [_parse_percent(row.get("Cluster Count Percentage")) for row in ordered_rows]
        if not slot_sizes:
            continue

        candidate_rows.append(
            {
                "Method": method,
                "Scenario": scenario,
                "K": k,
                "Slot_Sizes": slot_sizes,
                "Relative_Slot_Size_Distribution": distributions,
                **_candidate_metrics(slot_sizes, distributions),
                "Source_Combination_Count": float(len(rows)),
                "Source_Sample": f"{method}|{scenario}|K={k}",
            }
        )

    candidate_rows.sort(
        key=lambda candidate: (
            str(candidate.get("Method", "")),
            str(candidate.get("Scenario", "")),
            _to_int(candidate.get("K"), 0),
        )
    )
    for index, candidate in enumerate(candidate_rows, start=1):
        candidate["Config_ID"] = f"CFG_{index:03d}"

    status_by_id: dict[str, str] = {}
    prune_reason_by_id: dict[str, str] = {}
    selection_reason_by_id: dict[str, str] = {}
    family_by_id: dict[str, str] = {}
    family_rep_by_id: dict[str, str] = {}

    # Pass 1: exact duplicates (across all methods/scenarios/K) keep only best representative.
    exact_groups: dict[tuple[tuple[int, ...], tuple[int, ...]], list[dict[str, object]]] = defaultdict(list)
    for candidate in candidate_rows:
        slot_sizes = candidate.get("Slot_Sizes", [])
        distributions = candidate.get("Relative_Slot_Size_Distribution", [])
        if not isinstance(slot_sizes, list) or not isinstance(distributions, list):
            continue
        exact_groups[_candidate_signature(slot_sizes, distributions)].append(candidate)

    exact_representatives: list[dict[str, object]] = []
    exact_rep_by_id: dict[str, str] = {}
    for group in exact_groups.values():
        representative = min(group, key=_candidate_score)
        rep_id = str(representative.get("Config_ID", "")).strip()
        exact_representatives.append(representative)
        for candidate in group:
            candidate_id = str(candidate.get("Config_ID", "")).strip()
            exact_rep_by_id[candidate_id] = rep_id
            if candidate is representative:
                continue
            status_by_id[candidate_id] = "PRUNED"
            prune_reason_by_id[candidate_id] = f"Exact duplicate of {rep_id}"

    # Pass 2: near-identical family grouping and representative selection.
    family_clusters: list[list[dict[str, object]]] = []
    ordered_representatives = sorted(exact_representatives, key=_candidate_sort_key)
    for candidate in ordered_representatives:
        placed = False
        for family_candidates in family_clusters:
            anchor = min(family_candidates, key=_candidate_score)
            if _is_family_similar(candidate, anchor):
                family_candidates.append(candidate)
                placed = True
                break
        if not placed:
            family_clusters.append([candidate])

    family_representatives: list[dict[str, object]] = []
    for family_index, family_candidates in enumerate(family_clusters, start=1):
        family_id = f"FAM_{family_index:03d}"
        representative = min(family_candidates, key=_candidate_score)
        rep_id = str(representative.get("Config_ID", "")).strip()
        family_representatives.append(representative)

        for candidate in family_candidates:
            candidate_id = str(candidate.get("Config_ID", "")).strip()
            family_by_id[candidate_id] = family_id
            family_rep_by_id[candidate_id] = rep_id
            if candidate is representative:
                continue
            status_by_id[candidate_id] = "PRUNED"
            prune_reason_by_id[candidate_id] = f"Near-identical in {family_id}; represented by {rep_id}"

    # Propagate family IDs to exact-duplicate members.
    for candidate_id, rep_id in exact_rep_by_id.items():
        if candidate_id in family_by_id:
            continue
        family_by_id[candidate_id] = family_by_id.get(rep_id, "")
        family_rep_by_id[candidate_id] = family_rep_by_id.get(rep_id, rep_id)

    # Pass 3: dominance pruning among family representatives.
    active_family_representatives = [
        candidate
        for candidate in family_representatives
        if status_by_id.get(str(candidate.get("Config_ID", "")).strip(), "") != "PRUNED"
    ]

    for candidate in active_family_representatives:
        candidate_id = str(candidate.get("Config_ID", "")).strip()
        if _is_dominated(candidate, active_family_representatives):
            status_by_id[candidate_id] = "PRUNED"
            prune_reason_by_id[candidate_id] = "Dominated by another family representative"

    # Pass 4: method-balanced shortlist from surviving representatives.
    shortlist_pool = [
        candidate
        for candidate in active_family_representatives
        if status_by_id.get(str(candidate.get("Config_ID", "")).strip(), "") != "PRUNED"
    ]
    shortlist_pool.sort(key=_candidate_sort_key)

    if len(shortlist_pool) <= MAX_CANDIDATE_CONFIGURATIONS:
        shortlisted_ids = {
            str(candidate.get("Config_ID", "")).strip()
            for candidate in shortlist_pool
        }
    else:
        # Preserve diversity across methods before filling remaining slots.
        by_method: dict[str, list[dict[str, object]]] = defaultdict(list)
        for candidate in shortlist_pool:
            by_method[str(candidate.get("Method", ""))].append(candidate)

        for method in by_method:
            by_method[method].sort(key=_candidate_sort_key)

        selected_candidates: list[dict[str, object]] = []
        method_order = [method for method in common.METHODS if method in by_method]
        if not method_order:
            method_order = sorted(by_method.keys())

        progressed = True
        while progressed and len(selected_candidates) < MAX_CANDIDATE_CONFIGURATIONS:
            progressed = False
            for method in method_order:
                pool = by_method.get(method, [])
                if not pool:
                    continue
                selected_candidates.append(pool.pop(0))
                progressed = True
                if len(selected_candidates) >= MAX_CANDIDATE_CONFIGURATIONS:
                    break

        shortlisted_ids = {
            str(candidate.get("Config_ID", "")).strip()
            for candidate in selected_candidates
        }

    for candidate in shortlist_pool:
        candidate_id = str(candidate.get("Config_ID", "")).strip()
        if candidate_id in shortlisted_ids:
            status_by_id[candidate_id] = "SHORTLISTED"
            selection_reason_by_id[candidate_id] = "Family representative retained after duplicate/similarity/dominance filtering (method-balanced shortlist)"
        else:
            status_by_id[candidate_id] = "PRUNED"
            prune_reason_by_id[candidate_id] = "Outside shortlist limit after method-balanced family selection"

    family_sizes: dict[str, int] = defaultdict(int)
    for candidate in candidate_rows:
        family_id = family_by_id.get(str(candidate.get("Config_ID", "")).strip(), "")
        if family_id:
            family_sizes[family_id] += 1

    output_rows: list[dict[str, str]] = []
    for candidate in candidate_rows:
        candidate_id = str(candidate.get("Config_ID", "")).strip()
        slot_sizes = candidate.get("Slot_Sizes", [])
        distributions = candidate.get("Relative_Slot_Size_Distribution", [])
        selection_status = status_by_id.get(candidate_id, "PRUNED")
        family_id = family_by_id.get(candidate_id, "")
        output_rows.append(
            {
                "Config_ID": candidate_id,
                "Method": str(candidate.get("Method", "")),
                "Scenario": str(candidate.get("Scenario", "")),
                "K": str(candidate.get("K", "")),
                "Family_ID": family_id,
                "Family_Size": str(family_sizes.get(family_id, 0)),
                "Family_Representative_Config_ID": family_rep_by_id.get(candidate_id, candidate_id if family_id else ""),
                "Slot_Sizes": ",".join(f"{_as_float(size):.0f}" for size in slot_sizes) if isinstance(slot_sizes, list) else "",
                "Relative_Slot_Size_Distribution": ",".join(f"{_as_float(value):.4f}" for value in distributions) if isinstance(distributions, list) else "",
                "Mean_Slot_Size": f"{_as_float(candidate.get('Mean_Slot_Size', 0.0)):.3f}",
                "Slot_Size_Spread": f"{_as_float(candidate.get('Slot_Size_Spread', 0.0)):.3f}",
                "Distinct_Slot_Count": str(int(_as_float(candidate.get("Distinct_Slot_Count", 0.0)))),
                "Weighted_Distribution_Spread": f"{_as_float(candidate.get('Weighted_Distribution_Spread', 0.0)):.3f}",
                "Source_Combination_Count": str(int(_as_float(candidate.get("Source_Combination_Count", 0.0)))),
                "Selection_Status": selection_status,
                "Selection_Reason": selection_reason_by_id.get(candidate_id, ""),
                "Prune_Reason": prune_reason_by_id.get(candidate_id, ""),
                "Source_Sample": str(candidate.get("Source_Sample", "")),
            }
        )

    _write_csv_preserve(
        OUTPUT_FILE,
        [
            "Config_ID",
            "Method",
            "Scenario",
            "K",
            "Family_ID",
            "Family_Size",
            "Family_Representative_Config_ID",
            "Slot_Sizes",
            "Relative_Slot_Size_Distribution",
            "Mean_Slot_Size",
            "Slot_Size_Spread",
            "Distinct_Slot_Count",
            "Weighted_Distribution_Spread",
            "Source_Combination_Count",
            "Selection_Status",
            "Selection_Reason",
            "Prune_Reason",
            "Source_Sample",
        ],
        output_rows,
    )

    return OUTPUT_FILE


if __name__ == "__main__":
    output_path = build_candidate_configuration_filtering()
    print(f"Candidate configuration filtering complete. Output written to: {output_path}")
