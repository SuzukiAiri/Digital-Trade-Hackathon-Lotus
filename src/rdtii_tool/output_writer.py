"""JSON writer shared by active acquisition outputs."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable

def write_json_records(
    records: Iterable[Any],
    output_path: str | Path,
) -> Path:
    """Write dataclass, model, or mapping records as a JSON array."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = []
    for record in records:
        if hasattr(record, "to_json_dict"):
            payload.append(record.to_json_dict())
        elif is_dataclass(record):
            payload.append(asdict(record))
        else:
            payload.append(record)

    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path
