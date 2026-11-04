"""Shared paths, config loading, HTTP and database helpers."""
from __future__ import annotations

import hashlib
import logging
import os
from functools import lru_cache
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
INTERIM_DIR = DATA_DIR / "interim"
REFERENCE_DIR = DATA_DIR / "reference"
SQL_DIR = ROOT / "sql"
REPORTS_DIR = ROOT / "reports"

USER_AGENT = "melb-rent-watch/1.0 (+https://github.com/Thenuli10/melb-rent-watch)"

log = logging.getLogger("melb")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(CONFIG_DIR / "sources.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, max=30), reraise=True)
def http_get(url: str, *, stream: bool = False, headers: dict | None = None, timeout: int = 60) -> requests.Response:
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    resp = requests.get(url, headers=hdrs, stream=stream, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp


def download_file(url: str, dest: Path) -> Path:
    """Stream a URL to disk via a temporary file so a failed download never leaves a partial file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with http_get(url, stream=True, timeout=600) as resp:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def database_url() -> str:
    load_dotenv(ROOT / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return url


def connect():
    """Open a psycopg connection to the warehouse."""
    import psycopg

    return psycopg.connect(database_url(), autocommit=False)


def get_engine():
    """SQLAlchemy engine, used by pandas.read_sql in the modelling code."""
    from sqlalchemy import create_engine

    url = database_url()
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return create_engine(url, pool_pre_ping=True)
