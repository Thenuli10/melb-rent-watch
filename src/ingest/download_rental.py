"""Download the latest DFFH 'Moving annual median rent by suburb and town' workbook.

The rental report landing page links to a short URL that redirects to the
quarterly .xlsx. Only quarters not already in data/raw/rental/ are downloaded.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from src.common import RAW_DIR, download_file, http_get, load_config, log
from src.ingest import manifest
from src.ingest.manifest import DownloadResult

SOURCE = "rental"
MONTH_TO_Q = {"March": 1, "June": 2, "September": 3, "December": 4}


def find_report_link(html: str, base_url: str, pattern: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        if re.search(pattern, a["href"], re.IGNORECASE):
            return urljoin(base_url, a["href"])
    raise RuntimeError(f"No link matching '{pattern}' on {base_url}; the page layout may have changed")


def quarter_from_filename(filename: str, pattern: str) -> str:
    m = re.search(pattern, unquote(filename), re.IGNORECASE)
    if not m:
        raise RuntimeError(f"Cannot parse a quarter from '{filename}'")
    month = m.group("month").capitalize()
    return f"{m.group('year')}-Q{MONTH_TO_Q[month]}"


def resolve_file_url(url: str) -> str:
    """Follow the redirect to the real .xlsx URL without downloading the body."""
    with http_get(url, stream=True) as resp:
        return resp.url


def register_local_file(path: Path) -> DownloadResult:
    """Use a workbook downloaded by hand (e.g. if the site blocks scripted requests)."""
    cfg = load_config()["rental"]
    quarter = quarter_from_filename(path.name, cfg["filename_quarter_pattern"])
    existing = manifest.find(SOURCE, quarter)
    if existing:
        return manifest.from_entry(existing)
    dest = RAW_DIR / SOURCE / quarter / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, dest)
    return manifest.record(DownloadResult(source=SOURCE, path=dest, quarter=quarter, url=f"local:{path.name}"))


def run(force: bool = False) -> DownloadResult:
    cfg = load_config()["rental"]
    page = http_get(cfg["landing_page"]).text
    link = find_report_link(page, cfg["landing_page"], cfg["link_pattern"])
    file_url = resolve_file_url(link)
    filename = unquote(file_url.rsplit("/", 1)[-1])
    quarter = quarter_from_filename(filename, cfg["filename_quarter_pattern"])

    existing = manifest.find(SOURCE, quarter)
    if existing and not force:
        log.info("Rental report %s already downloaded (%s)", quarter, existing["path"])
        return manifest.from_entry(existing)

    dest: Path = RAW_DIR / SOURCE / quarter / filename
    log.info("Downloading rental report %s from %s", quarter, file_url)
    download_file(file_url, dest)
    return manifest.record(DownloadResult(source=SOURCE, path=dest, quarter=quarter, url=file_url))


if __name__ == "__main__":
    from src.common import setup_logging

    setup_logging()
    print(run())
