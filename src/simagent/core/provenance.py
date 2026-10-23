"""Append-only provenance ledger (design 12, RULE-I-06).

Records are JSON lines. ``seq`` is assigned under a file lock so the server and the mdrun wrapper
never interleave or duplicate sequence numbers. The class offers no way to edit or delete a record.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from simagent.core.paths import sha256_file


class Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = FileLock(str(self.path) + ".lock")

    def _last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    last = json.loads(line)["seq"]
        return last

    def append(self, record: dict[str, Any]) -> int:
        with self._lock:
            seq = self._last_seq() + 1
            full = {**record, "seq": seq, "ts": datetime.now(UTC).isoformat()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(full, sort_keys=True) + "\n")
            return seq

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def has_receipt(self, rel_path: str, sha256: str) -> bool:
        for record in self.records():
            for entry in record.get("outputs", []):
                if entry.get("path") == rel_path and entry.get("sha256") == sha256:
                    return True
        return False

    def latest_end(self, stage: str) -> dict[str, Any] | None:
        for record in reversed(self.records()):
            if record.get("event") == "end" and record.get("stage") == stage:
                return record
        return None


def file_entry(base: Path, rel: str) -> dict[str, Any]:
    path = Path(base) / rel
    return {"path": rel, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def record_internal(ledger: Ledger, *, stage: str, tool: str, base: Path,
                    outputs: list[str]) -> int:
    """Write an ``internal`` receipt for files that server code wrote itself."""
    return ledger.append({
        "event": "end", "kind": "internal", "stage": stage, "tool": tool,
        "outputs": [file_entry(base, rel) for rel in outputs],
    })


def record_ack(ledger: Ledger, *, warning_id: str, text: str, note: str) -> int:
    return ledger.append({
        "event": "ack", "kind": "internal", "tool": "record_approval",
        "warning_id": warning_id, "text": text, "note": note,
    })
