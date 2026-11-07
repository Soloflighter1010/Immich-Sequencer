"""
immich.py — Immich API client and timestamp sequencing logic.

All algorithm code is ported from the CLI script, adapted to accept
a settings dict rather than CLI args.
"""

import hashlib
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests

SEARCH_PAGE_SIZE = 1000
OUTLIER_IQR_MULT = 1.5


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def build_session(settings: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept":       "application/json",
        "Content-Type": "application/json",
        "x-api-key":    settings.get("api_key", ""),
    })
    return s


def check_auth(session: requests.Session, base_url: str) -> bool:
    try:
        r = session.get(f"{base_url}/api/users/me", timeout=10)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------------
# Album / asset fetching
# ---------------------------------------------------------------------------

def get_all_albums(session: requests.Session, base_url: str) -> list[dict]:
    r = session.get(f"{base_url}/api/albums", timeout=30)
    r.raise_for_status()
    return r.json()


def get_album_assets(
    session:  requests.Session,
    base_url: str,
    album_id: str,
) -> list[dict]:
    """Paginate through POST /api/search/metadata to collect all assets."""
    assets: list[dict] = []
    page = 1
    while True:
        r = session.post(
            f"{base_url}/api/search/metadata",
            json={"albumId": album_id, "page": page, "size": SEARCH_PAGE_SIZE},
            timeout=60,
        )
        r.raise_for_status()
        data      = r.json()
        items     = data.get("assets", {}).get("items", [])
        next_page = data.get("assets", {}).get("nextPage")
        assets.extend(items)
        if not next_page:
            break
        try:
            page = int(next_page)
        except (ValueError, TypeError):
            break
    return assets


def update_asset(
    method:    callable,
    base_url:  str,
    asset_id:  str,
    timestamp: str,
    max_retries: int = 3,
) -> bool:
    """Update a single asset's dateTimeOriginal with retry logic."""
    url = f"{base_url}/api/assets/{asset_id}"
    for attempt in range(max_retries):
        try:
            r = method(url, json={"dateTimeOriginal": timestamp}, timeout=15)
            if r.ok:
                return True
            if 400 <= r.status_code < 500:
                return False   # permanent failure
        except requests.Timeout:
            pass
        except requests.RequestException:
            pass
        if attempt < max_retries - 1:
            time.sleep(2.0)
    return False


# ---------------------------------------------------------------------------
# Sorting helpers
# ---------------------------------------------------------------------------

def natural_key(s: str) -> list:
    """Natural sort: file10 > file9."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", s)]


def order_hash(filenames: list[str]) -> str:
    """16-char SHA-256 fingerprint of the ordered filename list."""
    return hashlib.sha256("\n".join(filenames).encode()).hexdigest()[:16]


def fmt_ts(dt: datetime) -> str:
    """Immich-safe ISO 8601 with explicit +00:00 offset (not Z)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def parse_asset_datetime(asset: dict) -> datetime | None:
    """
    Extract the best available UTC datetime from an asset record.
    Priority: exifInfo.dateTimeOriginal → localDateTime → fileCreatedAt → fileModifiedAt
    """
    candidates = [
        (asset.get("exifInfo") or {}).get("dateTimeOriginal"),
        asset.get("localDateTime"),
        asset.get("fileCreatedAt"),
        asset.get("fileModifiedAt"),
    ]
    for raw in candidates:
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, AttributeError):
            continue
    return None


def get_time_range(
    assets: list[dict],
    min_year: int = 1990,
) -> tuple[datetime | None, datetime | None, int, list[tuple]]:
    """
    Scan assets for timestamps, filtering outliers.
    Returns (min_dt, max_dt, valid_count, outlier_pairs).

    Two passes:
      1. Hard bounds  — before min_year or > 24 h in future.
      2. IQR fence    — Tukey 1.5× on the surviving set.
    """
    now   = datetime.now(timezone.utc)
    floor = datetime(min_year, 1, 1, tzinfo=timezone.utc)
    ceil  = now + timedelta(hours=24)

    parsed: list[tuple[datetime, dict]] = [
        (dt, a) for a in assets if (dt := parse_asset_datetime(a)) is not None
    ]
    if not parsed:
        return None, None, 0, []

    valid    = [(dt, a) for dt, a in parsed if floor <= dt <= ceil]
    outliers = [(dt, a) for dt, a in parsed if not (floor <= dt <= ceil)]

    if len(valid) >= 4:
        ts_s   = [dt.timestamp() for dt, _ in valid]
        qs     = statistics.quantiles(ts_s, n=4)
        q1, q3 = qs[0], qs[2]
        iqr    = q3 - q1
        if iqr > 0:
            lo     = q1 - OUTLIER_IQR_MULT * iqr
            hi     = q3 + OUTLIER_IQR_MULT * iqr
            fenced = [(dt, a) for dt, a in valid if not (lo <= dt.timestamp() <= hi)]
            valid  = [(dt, a) for dt, a in valid if       lo <= dt.timestamp() <= hi]
            outliers.extend(fenced)

    if not valid:
        return None, None, 0, outliers

    dts = [dt for dt, _ in valid]
    return min(dts), max(dts), len(dts), outliers


def determine_timing(
    sorted_assets:    list[dict],
    n:                int,
    base_time_arg:    str | None,
    min_year:         int = 1990,
) -> tuple[datetime, timedelta, str, dict]:
    """
    Compute (base_dt, interval, mode, meta).
    meta carries: oldest, newest, span_s, parsed_count, warning, outlier_count.
    """
    FALLBACK_BASE = "2000-01-01T00:00:00+00:00"

    meta: dict = dict(
        oldest=None, newest=None, span_s=0.0,
        parsed_count=0, warning=None, outlier_count=0,
    )

    if base_time_arg:
        dt = datetime.fromisoformat(base_time_arg)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), timedelta(seconds=1), "manual", meta

    min_dt, max_dt, parsed_count, outlier_pairs = get_time_range(sorted_assets, min_year)
    meta["parsed_count"]  = parsed_count
    meta["outlier_count"] = len(outlier_pairs)

    if min_dt is None:
        meta["warning"] = (
            f"{len(outlier_pairs)} outlier timestamp(s) filtered, none remain"
            if outlier_pairs else "no timestamps found"
        ) + " — using fallback base time"
        base_dt = datetime.fromisoformat(FALLBACK_BASE).astimezone(timezone.utc)
        return base_dt, timedelta(seconds=1), "fallback", meta

    span_s = (max_dt - min_dt).total_seconds()
    meta.update(oldest=fmt_ts(min_dt), newest=fmt_ts(max_dt), span_s=span_s)

    raw_s = span_s / (n - 1) if n > 1 else 1.0
    if raw_s < 1.0:
        meta["warning"] = (
            f"span ({span_s:.0f}s) too narrow for {n} assets — using 1-second increments"
        )
        return min_dt, timedelta(seconds=1), "auto_narrow", meta

    return min_dt, timedelta(seconds=raw_s), "auto", meta