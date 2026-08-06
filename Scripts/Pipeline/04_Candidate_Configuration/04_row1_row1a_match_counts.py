import csv
import sys
from collections import Counter
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


CONFIG_FILE = common.STAGE4_OUTPUT_DIR / "Candidate_Configurations.csv"
SOURCE_LAYOUT_FILE = common.STAGE1_OUTPUT_DIR / "Location_Details_Prepared.csv"
OUTPUT_FILE = common.STAGE4_OUTPUT_DIR / "Configuration_Row1_Row1a_Match_Counts.csv"


def _read_csv(path: Path) -> list[dict[str, str]]:
    return common._read_csv(path)


def _parse_size_list(value: object | None) -> list[int]:
    text = common._decode_excel_text(value)
    result: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        parsed = common._to_int_default(token, -1)
        if parsed >= 0:
            result.append(parsed)
    return result


def build_row1_row1a_match_counts() -> Path:
    configs = _read_csv(CONFIG_FILE)
    locations = _read_csv(SOURCE_LAYOUT_FILE)

    row1_sizes = [
        common._to_int_default(row.get("Location height"), -1)
        for row in locations
        if str(row.get("Row", "")).strip().lower() in {"01", "1a"}
    ]
    row1_sizes = [size for size in row1_sizes if size > 0]
    size_counts = Counter(row1_sizes)

    output_rows: list[dict[str, str]] = []
    for cfg in configs:
        config_id = str(cfg.get("Config_ID", "")).strip()
        method = str(cfg.get("Method", "")).strip()
        scenario = str(cfg.get("Scenario", "")).strip()
        k = str(cfg.get("K", "")).strip()

        cfg_sizes = sorted(set(_parse_size_list(cfg.get("Slot_Sizes", ""))))
        matching_sizes = sorted(size for size in cfg_sizes if size in size_counts)
        total_matches = sum(size_counts[size] for size in matching_sizes)

        output_rows.append(
            {
                "Config_ID": config_id,
                "Method": method,
                "Scenario": scenario,
                "K": k,
                "Config_Slot_Sizes": common._encode_excel_text(",".join(str(size) for size in cfg_sizes)),
                "Original_Row1_Row1a_Matching_Slot_Sizes": common._encode_excel_text(",".join(str(size) for size in matching_sizes)),
                "Original_Row1_Row1a_Matching_Slot_Count": str(total_matches),
            }
        )

    common._write_csv_clean(
        OUTPUT_FILE,
        [
            "Config_ID",
            "Method",
            "Scenario",
            "K",
            "Config_Slot_Sizes",
            "Original_Row1_Row1a_Matching_Slot_Sizes",
            "Original_Row1_Row1a_Matching_Slot_Count",
        ],
        output_rows,
    )

    return OUTPUT_FILE


if __name__ == "__main__":
    output_path = build_row1_row1a_match_counts()
    print(f"Row 1 / 1a match count list created at: {output_path}")
