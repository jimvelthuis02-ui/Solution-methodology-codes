"""Stage 1 (Initial): initialize baseline context, assumptions, and run scaffolding."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_id(stage_name: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stage_name}_{ts}_{uuid4().hex[:8]}"


def main() -> int:
    stage_name = "1_Initial"
    # TODO: Replace this placeholder with actual initial-stage preparation logic.
    root = _repo_root()
    run_id = _run_id(stage_name)
    out_dir = root / "Output" / stage_name / run_id
    out_dir.mkdir(parents=True, exist_ok=False)

    metadata = {
        "run_id": run_id,
        "stage": stage_name,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "placeholder",
        "notes": "Initial stage scaffold executed successfully.",
        "todo": [
            "Define initial assumptions and constraints.",
            "Connect first immutable input sources.",
        ],
    }

    with (out_dir / "run_metadata.json").open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    print(f"[Stage {stage_name}] run_id={run_id}")
    print(f"[Stage {stage_name}] output={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
