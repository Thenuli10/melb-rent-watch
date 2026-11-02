"""Download Census 2021 income and population by suburb, and the Victorian Wage Price Index.

All three come from the ABS Data API as SDMX CSV. Census tables never change,
so they are fetched once; the WPI is refreshed every run because it is small
and revised quarterly.
"""
from __future__ import annotations

from src.common import RAW_DIR, http_get, load_config, log
from src.ingest import manifest
from src.ingest.manifest import DownloadResult

CSV_HEADERS = {"Accept": "application/vnd.sdmx.data+csv"}


def _fetch_csv(url: str, dest, source: str, quarter: str | None = None) -> DownloadResult:
    log.info("Fetching %s", url)
    text = http_get(url, headers=CSV_HEADERS).text
    if not text.startswith("DATAFLOW"):
        raise RuntimeError(f"Unexpected response from ABS API for {source}: {text[:200]!r}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return manifest.record(DownloadResult(source=source, path=dest, quarter=quarter, url=url))


def download_census(force: bool = False) -> dict[str, DownloadResult]:
    cfg = load_config()["income"]
    out = {}
    for key, source in (("median_hh_income", "census_income"), ("population", "census_population")):
        existing = manifest.find(source)
        if existing and not force:
            out[key] = manifest.from_entry(existing)
            continue
        url = f"{cfg['api_base']}/{cfg[key]}"
        out[key] = _fetch_csv(url, RAW_DIR / "census" / f"{source}_sal_vic_2021.csv", source)
    return out


def download_wage_index() -> DownloadResult:
    cfg = load_config()["wage_index"]
    url = f"{cfg['api_base']}/{cfg['series']}?startPeriod={cfg['start_period']}"
    return _fetch_csv(url, RAW_DIR / "wage_index" / "wpi_vic.csv", "wage_index")


def run(force: bool = False) -> dict[str, DownloadResult]:
    results = download_census(force)
    results["wage_index"] = download_wage_index()
    return results


if __name__ == "__main__":
    from src.common import setup_logging

    setup_logging()
    for k, v in run().items():
        print(k, v)
