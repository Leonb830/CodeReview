"""Atomic local run records. Checksums detect corruption, not untrusted author identity."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from codereview.models import FixResult, ReviewResult


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


class RunStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, run_id: str, kind: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid run identifier; use the ID printed by a completed run")
        return self.directory / f"{kind}-{run_id}.json"

    def _save(self, kind: str, result: ReviewResult | FixResult) -> Path:
        path = self._path(result.run_id, kind)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("Run directory must not be a symlink")
        payload = result.model_dump(mode="json")
        envelope = {
            "schema_version": 1,
            "sha256": hashlib.sha256(canonical_json(payload)).hexdigest(),
            "payload": payload,
        }
        data = canonical_json(envelope)
        if path.exists():
            if path.is_symlink() or path.read_bytes() != data:
                raise ValueError("An immutable run with this identifier already exists")
            return path
        fd, name = tempfile.mkstemp(prefix=".run-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # A hard link creates the final file atomically without overwriting a competing writer.
            os.link(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
        return path

    def save_review(self, result: ReviewResult) -> Path:
        return self._save("review", result)

    def save_fix(self, result: FixResult) -> Path:
        return self._save("fix", result)

    def load_review(self, run_id: str) -> ReviewResult:
        path = self._path(run_id, "review")
        if path.is_symlink():
            raise ValueError("Run record must not be a symlink")
        if path.stat().st_size > 100_000_000:
            raise ValueError("Run record exceeds the supported size")
        envelope = json.loads(path.read_text())
        if envelope.get("schema_version") != 1:
            raise ValueError("Unsupported run record version")
        payload = envelope.get("payload")
        if hashlib.sha256(canonical_json(payload)).hexdigest() != envelope.get("sha256"):
            raise ValueError("Run record checksum mismatch; rerun the review")
        result = ReviewResult.model_validate(payload)
        if result.run_id != run_id:
            raise ValueError("Run identifier does not match the requested record")
        return result

    def list_reviews(self) -> list[dict[str, str]]:
        if not self.directory.exists():
            return []
        rows = []
        for path in self.directory.glob("review-*.json"):
            run_id = path.stem.removeprefix("review-")
            try:
                result = self.load_review(run_id)
                rows.append(
                    {
                        "run_id": run_id,
                        "created_at": result.created_at,
                        "status": result.status,
                        "operation": result.operation,
                        "root": result.snapshot.root,
                    }
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(rows, key=lambda row: row["created_at"], reverse=True)
