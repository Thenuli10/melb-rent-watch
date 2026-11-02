"""Download the PTV GTFS feed (train stations) and ABS ASGS boundary files.

Boundaries are static for the 2021 edition, so they are downloaded once. GTFS
changes with timetables but station locations rarely do, so it is refreshed
only when the cached copy is older than `stations.max_age_days`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.common import RAW_DIR, download_file, load_config, log
from src.ingest import manifest
from src.ingest.manifest import DownloadResult


def download_gtfs(force: bool = False) -> DownloadResult:
    cfg = load_config()["stations"]
    existing = manifest.find("gtfs")
    if existing and not force:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(existing["downloaded_at"])
        if age < timedelta(days=cfg["max_age_days"]):
            return manifest.from_entry(existing)
    dest = RAW_DIR / "gtfs" / "gtfs.zip"
    log.info("Downloading PTV GTFS feed (~180 MB)")
    download_file(cfg["gtfs_url"], dest)
    return manifest.record(DownloadResult(source="gtfs", path=dest, url=cfg["gtfs_url"]))


def download_boundaries(force: bool = False) -> dict[str, DownloadResult]:
    cfg = load_config()["boundaries"]
    out = {}
    for key, filename in cfg["files"].items():
        source = f"boundary_{key}"
        existing = manifest.find(source)
        if existing and not force:
            out[key] = manifest.from_entry(existing)
            continue
        url = f"{cfg['base_url']}/{filename}"
        dest = RAW_DIR / "boundaries" / filename
        log.info("Downloading %s", filename)
        download_file(url, dest)
        out[key] = manifest.record(DownloadResult(source=source, path=dest, url=url))
    return out


def run(force: bool = False) -> dict[str, DownloadResult]:
    results = download_boundaries(force)
    results["gtfs"] = download_gtfs(force)
    return results


if __name__ == "__main__":
    from src.common import setup_logging

    setup_logging()
    for k, v in run().items():
        print(k, v)
