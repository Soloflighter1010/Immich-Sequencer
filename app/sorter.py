"""
sorter.py — Background job manager.

Sorting runs in a daemon thread so FastAPI stays responsive.
Progress is published via an asyncio-safe event bus that feeds SSE clients.
"""

import asyncio
import threading
import time
from datetime import datetime, timezone

from database import (
    get_db, get_settings,
    set_album_status, mark_album_sorted,
    start_sort_log, finish_sort_log,
)
from immich import (
    build_session, get_album_assets,
    natural_key, order_hash, fmt_ts,
    determine_timing, update_asset,
)


# ---------------------------------------------------------------------------
# Event bus (thread → asyncio SSE clients)
# ---------------------------------------------------------------------------

class EventBus:
    """Thread-safe bridge between the worker thread and async SSE subscribers."""

    def __init__(self) -> None:
        self._subs: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subs.remove(q)
        except ValueError:
            pass

    def emit(self, event: dict) -> None:
        """Publish from any thread. No-op if loop not set yet."""
        if self._loop and not self._loop.is_closed():
            for q in list(self._subs):
                self._loop.call_soon_threadsafe(q.put_nowait, event)


# ---------------------------------------------------------------------------
# Job manager
# ---------------------------------------------------------------------------

class JobManager:
    """
    Manages a single sort job at a time.

    Lifecycle:
      start()  → spins up a daemon thread, returns immediately
      cancel() → sets the cancel event; thread finishes current asset then stops
      status   → dict always reflecting latest state, safe to read from any thread
    """

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.bus     = EventBus()

        # Snapshot of current run state — read from the API thread for /api/status
        self.status: dict = {
            "running":         False,
            "album_id":        None,
            "album_name":      "",
            "album_progress":  0,
            "album_total":     0,
            "batch_progress":  0,
            "batch_total":     0,
            "batch_start_ms":  None,   # epoch ms so the browser can compute ETA
            "message":         "Idle",
        }

    # ------------------------------------------------------------------
    # Public API (called from async FastAPI handlers)
    # ------------------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bus.set_loop(loop)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        self._cancel.set()

    def start(self, album_ids: list[str]) -> None:
        """Start a sort job. Raises RuntimeError if one is already running."""
        with self._lock:
            if self.is_running():
                raise RuntimeError("A sort job is already running.")
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._worker,
                args=(album_ids, get_settings()),
                daemon=True,
            )
            self._thread.start()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit(self, update: dict) -> None:
        """Merge update into status snapshot and push to SSE subscribers."""
        self.status.update(update)
        self.bus.emit(update)

    def _worker(self, album_ids: list[str], settings: dict) -> None:
        """Main worker — iterates album list in the background thread."""
        total      = len(album_ids)
        log_id     = start_sort_log(total)
        sorted_n   = 0
        failed_n   = 0
        skipped_n  = 0
        start_ms   = int(time.time() * 1000)

        session    = build_session(settings)
        base_url   = settings.get("immich_url", "")
        http_method = (
            session.put if settings.get("http_method", "PUT") == "PUT"
            else session.patch
        )
        album_delay = float(settings.get("album_delay", 1.0))

        self._emit({
            "running":        True,
            "batch_progress": 0,
            "batch_total":    total,
            "batch_start_ms": start_ms,
            "message":        f"Starting {total} album(s)…",
        })

        for i, album_id in enumerate(album_ids):
            if self._cancel.is_set():
                skipped_n += total - i
                break

            result = self._sort_one(session, http_method, base_url, album_id, settings, i, total)

            if result == "sorted":
                sorted_n += 1
            elif result == "failed":
                failed_n += 1
            elif result == "skipped":
                skipped_n += 1

            self._emit({
                "batch_progress": i + 1,
                "refresh":        True,   # tell the browser to reload the album row
            })

            if i < total - 1 and not self._cancel.is_set():
                time.sleep(album_delay)

        cancelled = self._cancel.is_set()
        finish_sort_log(
            log_id,
            status="cancelled" if cancelled else "completed",
            sorted_n=sorted_n,
            failed_n=failed_n,
            skipped_n=skipped_n,
        )

        self._emit({
            "running":    False,
            "album_id":   None,
            "album_name": "",
            "message":    "Cancelled" if cancelled else "Complete",
            "refresh":    True,
        })

    def _sort_one(
        self,
        session,
        http_method,
        base_url: str,
        album_id: str,
        settings: dict,
        batch_idx: int,
        batch_total: int,
    ) -> str:
        """Sort a single album. Returns 'sorted' | 'failed' | 'skipped'."""

        # Get album name from DB (already synced during scan)
        with get_db() as conn:
            row = conn.execute(
                "SELECT name FROM albums WHERE id = ?", (album_id,)
            ).fetchone()
        album_name = row["name"] if row else album_id[:8] + "…"

        self._emit({
            "album_id":       album_id,
            "album_name":     album_name,
            "album_progress": 0,
            "album_total":    0,
            "message":        f"Fetching assets for '{album_name}'…",
        })

        set_album_status(album_id, "sorting", sort_progress=0, sort_total=0)

        # ── Fetch assets ──────────────────────────────────────────────────────
        try:
            assets = get_album_assets(session, base_url, album_id)
        except Exception as exc:
            set_album_status(album_id, "error", error_message=str(exc))
            self._emit({"message": f"❌ Error fetching '{album_name}': {exc}"})
            return "failed"

        if not assets:
            set_album_status(album_id, "empty", asset_count=0)
            return "skipped"

        # ── Sort filenames ─────────────────────────────────────────────────────
        sorted_assets    = sorted(assets, key=lambda a: natural_key(a.get("originalFileName", "")))
        sorted_filenames = [a.get("originalFileName", "") for a in sorted_assets]
        seq_hash         = order_hash(sorted_filenames)
        n                = len(sorted_assets)

        # ── Determine timing ───────────────────────────────────────────────────
        base_time_arg = settings.get("base_time_override") or None
        min_year      = int(settings.get("min_year", 1990))
        base_dt, interval, mode, meta = determine_timing(
            sorted_assets, n, base_time_arg, min_year
        )

        request_delay = float(settings.get("request_delay", 0.05))
        max_retries   = int(settings.get("max_retries", 3))

        set_album_status(album_id, "sorting", sort_total=n, sort_progress=0)

        self._emit({
            "album_id":       album_id,
            "album_name":     album_name,
            "album_progress": 0,
            "album_total":    n,
            "message":        f"Sorting '{album_name}' ({n} assets)…",
        })

        # ── Process assets ─────────────────────────────────────────────────────
        success_count = 0
        failed_count  = 0
        current_dt    = base_dt

        for i, asset in enumerate(sorted_assets):
            if self._cancel.is_set():
                break

            timestamp = fmt_ts(current_dt)
            ok = update_asset(
                http_method, base_url, asset["id"], timestamp, max_retries
            )

            if ok:
                success_count += 1
            else:
                failed_count += 1

            current_dt += interval

            # Emit every asset for smooth progress bar
            self._emit({
                "album_progress": i + 1,
                "album_total":    n,
            })

            # Persist progress every 20 assets (resilience without hammering SQLite)
            if (i + 1) % 20 == 0:
                set_album_status(album_id, "sorting", sort_progress=i + 1)

            if i < n - 1:
                time.sleep(request_delay)

        # ── Persist result ─────────────────────────────────────────────────────
        cancelled = self._cancel.is_set()

        if cancelled:
            final_status = "interrupted"
        elif success_count == 0:
            final_status = "error"
        else:
            final_status = "sorted"

        mark_album_sorted(
            album_id,
            status=final_status,
            order_hash=seq_hash,
            assets_updated=success_count,
            assets_failed=failed_count,
            mode=mode,
            base_time_used=fmt_ts(base_dt),
            interval_s=interval.total_seconds(),
            sort_progress=success_count,
            sort_total=n,
            asset_count=n,
            error_message="" if final_status != "error" else "All asset updates failed",
        )

        return "sorted" if final_status == "sorted" else (
            "skipped" if cancelled else "failed"
        )


# Module-level singleton used by main.py
job_manager = JobManager()