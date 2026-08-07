import csv
import importlib.util
from collections import Counter, defaultdict
import shutil
from pathlib import Path
import sys

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


def _replace_with_suffix(path: Path, suffix: str = "_greedy") -> Path:
    if not path.exists():
        return path.with_name(f"{path.stem}{suffix}{path.suffix}")
    target = path.with_name(f"{path.stem}{suffix}{path.suffix}")
    shutil.move(str(path), str(target))
    return target


def _load_base_stage6_module():
    stage6_path = Path(__file__).resolve().parent / "06_layout_generation.py"
    spec = importlib.util.spec_from_file_location("stage6_base_module", stage6_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load Stage 6 module from {stage6_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row_sort_key(row_label: str) -> tuple[int, str]:
    text = str(row_label).strip().lower()
    if text == "":
        return (10**9, "")

    digits = ""
    suffix = ""
    for ch in text:
        if ch.isdigit() and suffix == "":
            digits += ch
        else:
            suffix += ch

    row_number = common._to_int_default(digits, 10**9)
    return (row_number, suffix)


def _original_rows_by_column(prepared_rows: list[dict[str, str]]) -> dict[str, list[tuple[str, int]]]:
    grouped: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in prepared_rows:
        location_type = str(row.get("Location Type", "")).strip().lower()
        if location_type == "doorgang":
            continue

        rack = str(row.get("Rack", "")).strip()
        column = str(row.get("Column", "")).strip()
        row_label = str(row.get("Row", "")).strip()
        slot_size = common._to_int_default(row.get("Location height"), -1)
        if not rack or not column or not row_label or slot_size <= 0:
            continue

        grouped[f"{rack}{column}"].append((row_label, slot_size))

    ordered: dict[str, list[tuple[str, int]]] = {}
    for column_key, values in grouped.items():
        ordered[column_key] = sorted(values, key=lambda item: _row_sort_key(item[0]))
    return ordered


def _build_anchor_prefix_by_column(
    slot_sizes_for_config: set[int],
    column_keys: list[str],
    rows_by_column: dict[str, list[tuple[str, int]]],
) -> dict[str, list[float]]:
    # Start from row 01/1a matches and greedily propagate upward in the original
    # layout while slot sizes keep matching this configuration.
    base_row_labels = {"01", "1a"}
    fixed_prefix: dict[str, list[float]] = {}

    for column_key in column_keys:
        original_rows = rows_by_column.get(column_key, [])
        if not original_rows:
            continue

        selected_indices: set[int] = set()
        for idx, (row_label, slot_size) in enumerate(original_rows):
            if row_label.strip().lower() not in base_row_labels:
                continue
            if slot_size not in slot_sizes_for_config:
                continue

            walk = idx
            while walk < len(original_rows):
                _walk_row, walk_size = original_rows[walk]
                if walk_size not in slot_sizes_for_config:
                    break
                selected_indices.add(walk)
                walk += 1

        if not selected_indices:
            continue

        # The allocator can only seed a bottom prefix, so keep contiguous matches
        # from the first physical row upward.
        prefix_sizes: list[float] = []
        cursor = 0
        while cursor < len(original_rows) and cursor in selected_indices:
            prefix_sizes.append(float(original_rows[cursor][1]))
            cursor += 1

        if prefix_sizes:
            fixed_prefix[column_key] = prefix_sizes

    return fixed_prefix


def _merge_fixed_prefixes(
    existing_prefix: dict[str, list[float]] | object,
    anchor_prefix: dict[str, list[float]],
) -> dict[str, list[float]]:
    merged: dict[str, list[float]] = {}

    if isinstance(existing_prefix, dict):
        for column_key, values in existing_prefix.items():
            merged[column_key] = [float(value) for value in list(values)]

    for column_key, values in anchor_prefix.items():
        if column_key not in merged:
            merged[column_key] = []
        merged[column_key].extend(float(value) for value in values)

    return merged


def build_layout_generation_greedy_anchor() -> Path:
    stage6 = _load_base_stage6_module()

    output_dir = common.OUTPUT_ROOT / "06_Layout_Generation_Greedy"
    output_dir.mkdir(parents=True, exist_ok=True)

    stage6.LAYOUT_OUTPUT_DIR = output_dir
    stage6.LAYOUT_TOPFILLED_DIR = output_dir
    stage6.LAYOUT_DIAGNOSTICS_DIR = output_dir

    prepared_rows = common._read_csv(common.STAGE1_OUTPUT_DIR / "Location_Details_Prepared.csv")
    rows_by_column = _original_rows_by_column(prepared_rows)

    config_ids = [str(row.get("Config_ID", "")).strip() for row in stage6._candidate_configs()]
    call_index = 0
    anchor_records: list[dict[str, str]] = []

    original_allocate = common._allocate_layout_by_column

    def _allocate_with_greedy_anchor(
        target_exact_counts: dict[float, int],
        column_keys: list[str],
        style: str,
        style_context: dict[str, object] | None = None,
    ):
        nonlocal call_index

        config_id = config_ids[call_index] if call_index < len(config_ids) else f"CFG_CALL_{call_index + 1:03d}"
        call_index += 1

        config_slot_sizes = {int(round(size)) for size, count in target_exact_counts.items() if int(count) > 0}
        anchor_prefix = _build_anchor_prefix_by_column(config_slot_sizes, column_keys, rows_by_column)

        anchor_counts = Counter()
        for values in anchor_prefix.values():
            for value in values:
                anchor_counts[int(round(value))] += 1

        remaining_counts: dict[float, int] = {}
        for size, required in target_exact_counts.items():
            used = anchor_counts.get(int(round(size)), 0)
            remaining_counts[size] = max(int(required) - used, 0)

        context = dict(style_context or {})
        existing_fixed = context.get("fixed_prefix_by_column", {})
        context["fixed_prefix_by_column"] = _merge_fixed_prefixes(existing_fixed, anchor_prefix)

        anchor_records.append(
            {
                "Config_ID": config_id,
                "Config_Slot_Sizes": common._encode_excel_text(",".join(str(size) for size in sorted(config_slot_sizes))
                ),
                "Anchor_Fixed_Slot_Distribution": "|".join(
                    f"{size}:{count}" for size, count in sorted(anchor_counts.items())
                ),
                "Anchor_Fixed_Slot_Count": str(sum(anchor_counts.values())),
                "Anchor_Fixed_Columns_Count": str(len(anchor_prefix)),
                "Remaining_Required_Counts_After_Anchoring": "|".join(
                    f"{int(size)}:{count}" for size, count in sorted(remaining_counts.items())
                ),
            }
        )

        return original_allocate(
            target_exact_counts=remaining_counts,
            column_keys=column_keys,
            style=style,
            style_context=context,
        )

    try:
        common._allocate_layout_by_column = _allocate_with_greedy_anchor
        stage6.build_layout_generation()
    finally:
        common._allocate_layout_by_column = original_allocate

    _replace_with_suffix(output_dir / "Candidate_Layout_Summary_TopFilled.csv")
    _replace_with_suffix(output_dir / "Candidate_Layout_By_Rack_Column_TopFilled.csv")
    _replace_with_suffix(output_dir / "Candidate_Layout_By_Location_TopFilled.csv")
    _replace_with_suffix(output_dir / "Empty_Locations_By_Slot_Size.csv")

    anchor_file = output_dir / "Greedy_Fixed_Slots_Summary_greedy.csv"
    with anchor_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=[
                "Config_ID",
                "Config_Slot_Sizes",
                "Anchor_Fixed_Slot_Distribution",
                "Anchor_Fixed_Slot_Count",
                "Anchor_Fixed_Columns_Count",
                "Remaining_Required_Counts_After_Anchoring",
            ],
        )
        writer.writeheader()
        writer.writerows(anchor_records)

    return output_dir


if __name__ == "__main__":
    out_dir = build_layout_generation_greedy_anchor()
    print(f"Greedy anchored Stage 6 variant complete. Output written to: {out_dir}")
