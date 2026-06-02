"""Shared path and run metadata utilities for stage runners."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass
class RunContext:
    run_id: str
    stage_name: str
    output_dir: Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def generate_run_id(stage_name: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid4().hex[:8]
    return f"{stage_name}_{timestamp}_{suffix}"


def build_run_context(stage_name: str) -> RunContext:
    root = repo_root()
    run_id = generate_run_id(stage_name)
    output_dir = root / "Output" / stage_name / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    return RunContext(run_id=run_id, stage_name=stage_name, output_dir=output_dir)
