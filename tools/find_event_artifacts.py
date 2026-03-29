"""Resolve event IDs to linked run artifacts.

Usage:
    uv run python tools/find_event_artifacts.py \
        --run-dir outputs/smoke_event_index/seed0_memv0 \
        --event-id 4900ccf2969a4a5eb8e51a269a62dab9-23
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _load_index(index_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with index_path.open("r", encoding="utf-8") as handle:
        for line_num, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {index_path}:{line_num}: {exc}") from exc
            if isinstance(obj, dict):
                records.append(obj)
    return records


def _filter_by_event(records: list[dict[str, Any]], event_id: str) -> list[dict[str, Any]]:
    return [record for record in records if str(record.get("event_id", "")) == event_id]


def _render_human(run_dir: Path, event_id: str, matches: list[dict[str, Any]]) -> str:
    if not matches:
        return f"No records found for event_id={event_id!r}"

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in matches:
        grouped[str(item.get("source", "unknown"))].append(item)

    lines: list[str] = []
    lines.append(f"event_id: {event_id}")
    lines.append(f"matches: {len(matches)}")
    lines.append("")

    for source in sorted(grouped):
        lines.append(f"[{source}]")
        for row in grouped[source]:
            artifact = str(row.get("artifact_path", ""))
            resolved = (run_dir / artifact).resolve() if artifact else ""
            event_type = str(row.get("event_type", ""))
            callback_method = str(row.get("callback_method", ""))
            iteration = row.get("iteration")
            lines.append(f"- event_type={event_type} callback={callback_method} iteration={iteration}")
            lines.append(f"  artifact={artifact}")
            lines.append(f"  resolved={resolved}")
            details = row.get("details")
            if details:
                lines.append(f"  details={json.dumps(details, ensure_ascii=False)}")
        lines.append("")

    return "\n".join(lines).rstrip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Find all artifact links for a callback event ID")
    parser.add_argument("--run-dir", required=True, help="Run directory containing event_correlation_index.jsonl")
    parser.add_argument("--event-id", required=True, help="Event ID to look up")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON array of matching records",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    index_path = run_dir / "event_correlation_index.jsonl"
    if not index_path.exists():
        raise FileNotFoundError(f"Correlation index not found: {index_path}")

    records = _load_index(index_path)
    matches = _filter_by_event(records, args.event_id)

    if args.json:
        print(json.dumps(matches, indent=2, ensure_ascii=False))
    else:
        print(_render_human(run_dir, args.event_id, matches))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
