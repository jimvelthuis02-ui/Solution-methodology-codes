import csv
from collections import defaultdict
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


OUTPUT_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
MAX_CANDIDATE_CONFIGURATIONS = 90
NEAR_SLOT_TOLERANCE_CM = 5.0
NEAR_DISTRIBUTION_TOLERANCE = 0.05


def _read_stage3_rows() -> list[dict[str, str]]:
    """Read slot-size summaries from all Stage 3 clustering methods."""
    rows: list[dict[str, str]] = []
    for path in [common.SLOT_SIZE_ROOT / method / "Slot_Size_Configuration_Summary.csv" for method in common.METHODS]:
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            rows.extend(list(reader))
    return rows


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


def build_candidate_configuration_filtering() -> Path:
    """Build Stage 4 candidate list via grouping, dedupe, pruning, and shortlist."""
    stage3_rows = _read_stage3_rows()
    grouped: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    # Group source rows by method/scenario/K so each group becomes one candidate.
    for row in stage3_rows:
        method = str(row.get("Method", "")).strip()
        scenario = str(row.get("Scenario", "")).strip()
        k = str(row.get("K", "")).strip()
        if method and scenario and k:
            grouped[(method, scenario, k)].append(row)

    candidate_rows: list[dict[str, object]] = []
    for (method, scenario, k), rows in grouped.items():
        # Keep slot sizes ordered to make signatures and comparisons stable.
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
                "Source_Sample": ";".join(sorted({f"{method}|{scenario}|K={k}" for _ in [0]})),
            }
        )

    exact_map: dict[tuple[str, str, tuple[int, ...], tuple[int, ...]], dict[str, object]] = {}
    # Pass 1: remove exact duplicates.
    for candidate in candidate_rows:
        method = str(candidate["Method"])
        k = str(candidate["K"])
        slot_sizes = candidate["Slot_Sizes"]
        distributions = candidate["Relative_Slot_Size_Distribution"]
        if not isinstance(slot_sizes, list) or not isinstance(distributions, list):
            continue
        size_sig, dist_sig = _candidate_signature(slot_sizes, distributions)
        dedupe_key = (method, k, size_sig, dist_sig)
        if dedupe_key not in exact_map:
            exact_map[dedupe_key] = dict(candidate)

    exact_candidates = list(exact_map.values())

    near_map: dict[tuple[str, str, tuple[int, ...], tuple[int, ...]], dict[str, object]] = {}
    # Pass 2: merge near-identical candidates and keep best-scoring representative.
    for candidate in exact_candidates:
        method = str(candidate["Method"])
        k = str(candidate["K"])
        slot_sizes = candidate["Slot_Sizes"]
        distributions = candidate["Relative_Slot_Size_Distribution"]
        if not isinstance(slot_sizes, list) or not isinstance(distributions, list):
            continue
        relaxed_key = (method, k, *_near_signature(slot_sizes, distributions))
        if relaxed_key not in near_map:
            near_map[relaxed_key] = candidate
        else:
            existing = near_map[relaxed_key]
            existing_score = (
                _as_float(existing.get("Weighted_Distribution_Spread", 0.0)),
                _as_float(existing.get("Slot_Size_Spread", 0.0)),
                _as_float(existing.get("Mean_Slot_Size", 0.0)),
            )
            candidate_score = (
                _as_float(candidate.get("Weighted_Distribution_Spread", 0.0)),
                _as_float(candidate.get("Slot_Size_Spread", 0.0)),
                _as_float(candidate.get("Mean_Slot_Size", 0.0)),
            )
            if candidate_score < existing_score:
                near_map[relaxed_key] = candidate

    filtered_candidates = list(near_map.values())
    # Pass 3: drop dominated candidates to reduce the search space.
    filtered_candidates = [candidate for candidate in filtered_candidates if not _is_dominated(candidate, filtered_candidates)]

    filtered_candidates.sort(
        key=lambda candidate: (
            str(candidate.get("Method", "")),
            str(candidate.get("Scenario", "")),
            int(str(candidate.get("K", "0"))),
            _as_float(candidate.get("Mean_Slot_Size", 0.0)),
            _as_float(candidate.get("Slot_Size_Spread", 0.0)),
            _as_float(candidate.get("Weighted_Distribution_Spread", 0.0)),
        )
    )

    shortlisted = filtered_candidates[:MAX_CANDIDATE_CONFIGURATIONS]
    selected_ids = {id(candidate) for candidate in shortlisted}
    _ = selected_ids

    output_rows: list[dict[str, str]] = []
    # Emit all filtered candidates and mark shortlist membership for downstream stages.
    for index, candidate in enumerate(filtered_candidates, start=1):
        slot_sizes = candidate.get("Slot_Sizes", [])
        distributions = candidate.get("Relative_Slot_Size_Distribution", [])
        output_rows.append(
            {
                "Config_ID": f"CFG_{index:03d}",
                "Method": str(candidate.get("Method", "")),
                "Scenario": str(candidate.get("Scenario", "")),
                "K": str(candidate.get("K", "")),
                "Slot_Sizes": ",".join(f"{_as_float(size):.0f}" for size in slot_sizes) if isinstance(slot_sizes, list) else "",
                "Relative_Slot_Size_Distribution": ",".join(f"{_as_float(value):.4f}" for value in distributions) if isinstance(distributions, list) else "",
                "Mean_Slot_Size": f"{_as_float(candidate.get('Mean_Slot_Size', 0.0)):.3f}",
                "Slot_Size_Spread": f"{_as_float(candidate.get('Slot_Size_Spread', 0.0)):.3f}",
                "Distinct_Slot_Count": str(int(_as_float(candidate.get("Distinct_Slot_Count", 0.0)))),
                "Weighted_Distribution_Spread": f"{_as_float(candidate.get('Weighted_Distribution_Spread', 0.0)):.3f}",
                "Source_Combination_Count": str(int(_as_float(candidate.get("Source_Combination_Count", 0.0)))),
                "Selection_Status": "SHORTLISTED" if candidate in shortlisted else "PRUNED",
                "Prune_Reason": "" if candidate in shortlisted else "Near-identical, dominated, or outside shortlist limit",
                "Source_Sample": str(candidate.get("Source_Sample", "")),
            }
        )

    common._write_csv_clean(
        OUTPUT_FILE,
        [
            "Config_ID",
            "Method",
            "Scenario",
            "K",
            "Slot_Sizes",
            "Relative_Slot_Size_Distribution",
            "Mean_Slot_Size",
            "Slot_Size_Spread",
            "Distinct_Slot_Count",
            "Weighted_Distribution_Spread",
            "Source_Combination_Count",
            "Selection_Status",
            "Prune_Reason",
            "Source_Sample",
        ],
        output_rows,
    )

    return OUTPUT_FILE


if __name__ == "__main__":
    output_path = build_candidate_configuration_filtering()
    print(f"Candidate configuration filtering complete. Output written to: {output_path}")
