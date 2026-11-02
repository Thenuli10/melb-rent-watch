"""A JSON manifest of every downloaded file, so reruns skip files already on disk.

The manifest is the local record; the pipeline also copies each entry into
dw.etl_run_log once the database is reachable.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from src.common import RAW_DIR, ROOT, sha256

MANIFEST = RAW_DIR / "manifest.json"


@dataclass
class DownloadResult:
    source: str
    path: Path
    quarter: str | None = None  # e.g. "2026-Q2" for quarterly sources
    is_new: bool = True
    checksum: str = ""
    url: str = ""
    downloaded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _read() -> list[dict]:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return []


def find(source: str, quarter: str | None = None) -> dict | None:
    """Most recent manifest entry for a source (and quarter, if given) whose file still exists."""
    for entry in reversed(_read()):
        if entry["source"] != source:
            continue
        if quarter is not None and entry.get("quarter") != quarter:
            continue
        if (ROOT / entry["path"]).exists():
            return entry
    return None


def record(result: DownloadResult) -> DownloadResult:
    """Fill in the checksum and append the download to the manifest."""
    result.checksum = sha256(result.path)
    entries = _read()
    entry = asdict(result)
    entry["path"] = result.path.resolve().relative_to(ROOT).as_posix()
    entries.append(entry)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return result


def from_entry(entry: dict) -> DownloadResult:
    return DownloadResult(
        source=entry["source"],
        path=ROOT / entry["path"],
        quarter=entry.get("quarter"),
        is_new=False,
        checksum=entry.get("checksum", ""),
        url=entry.get("url", ""),
        downloaded_at=entry.get("downloaded_at", ""),
    )
