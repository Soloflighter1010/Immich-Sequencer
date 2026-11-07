"""
main.py — FastAPI application.

Routes:
  GET  /                          → index.html (static mount)
  GET  /api/albums                → album list (filterable)
  GET  /api/albums/counts         → status count summary
  POST /api/scan                  → refresh album list from Immich
  POST /api/sort                  → start a sort job
  POST /api/cancel                → cancel the running job
  GET  /api/status                → current job state (polling fallback)
  GET  /api/stream                → SSE event stream (real-time progress)
  GET  /api/settings              → read settings
  PUT  /api/settings              → write settings
  GET  /api/test-connection       → validate Immich connectivity
  GET  /api/history               → recent sort log entries
"""

import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from database import (
    init_db, get_db, get_settings, save_settings,
    upsert_album_from_scan, delete_album,
    get_album_counts,
)
from immich import build_session, check_auth, get_all_albums
from sorter import job_manager

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Immich Sequencer", version="1.0.0")


@app.on_event("startup")
async def _startup() -> None:
    os.makedirs("/data", exist_ok=True)
    init_db()
    job_manager.set_loop(asyncio.get_event_loop())


# ---------------------------------------------------------------------------
# Album endpoints
# ---------------------------------------------------------------------------

@app.get("/api/albums")
async def list_albums(status: str = "", search: str = ""):
    with get_db() as conn:
        clauses: list[str] = []
        params:  list      = []

        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if search:
            clauses.append("name LIKE ?")
            params.append(f"%{search}%")

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows  = conn.execute(
            f"SELECT * FROM albums {where} ORDER BY name COLLATE NOCASE",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/albums/counts")
async def album_counts():
    return get_album_counts()


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

@app.post("/api/scan")
async def scan_albums():
    """
    Fetch the full album list from Immich, upsert into the local DB,
    and remove any albums that no longer exist on the server.
    """
    settings = get_settings()
    session  = build_session(settings)

    try:
        remote_albums = get_all_albums(session, settings["immich_url"])
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    with get_db() as conn:
        known_ids = {r["id"] for r in conn.execute("SELECT id FROM albums").fetchall()}

    remote_ids   = {a["id"] for a in remote_albums}
    deleted_ids  = known_ids - remote_ids
    new_statuses: dict[str, int] = {}

    for album in remote_albums:
        status = upsert_album_from_scan(
            album_id=album["id"],
            name=album.get("albumName", "Unknown"),
            asset_count=album.get("assetCount", 0),
        )
        new_statuses[status] = new_statuses.get(status, 0) + 1

    for aid in deleted_ids:
        delete_album(aid)

    return {
        "total":   len(remote_albums),
        "added":   len(remote_ids - known_ids),
        "deleted": len(deleted_ids),
        "counts":  get_album_counts(),
    }


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------

@app.post("/api/sort")
async def sort_albums(request: Request):
    """
    Start a sort job.

    Request body:
      album_ids     : list[str] - explicitly selected albums
      status_filter : list[str] - albums to sort by status
    """

    if job_manager.is_running():
        return JSONResponse(
            {"error": "A sort job is already running."},
            status_code=409,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON request body."},
            status_code=400,
        )

    # IMPORTANT:
    # If album_ids exists in the request, this is a selected/manual sort.
    # Never silently fall back to sorting all albums.
    if "album_ids" in body:
        album_ids = body["album_ids"]

        if not isinstance(album_ids, list):
            return JSONResponse(
                {"error": "album_ids must be a list."},
                status_code=400,
            )

        if len(album_ids) == 0:
            return JSONResponse(
                {"error": "No albums were selected."},
                status_code=400,
            )

    # Otherwise this is a bulk sort by status.
    else:
        status_filter = body.get(
            "status_filter",
            ["unsorted", "updated", "interrupted"],
        )

        if not isinstance(status_filter, list) or len(status_filter) == 0:
            return JSONResponse(
                {"error": "status_filter must be a non-empty list."},
                status_code=400,
            )

        with get_db() as conn:
            placeholders = ",".join("?" for _ in status_filter)

            rows = conn.execute(
                f"""
                SELECT id
                FROM albums
                WHERE status IN ({placeholders})
                ORDER BY name COLLATE NOCASE
                """,
                status_filter,
            ).fetchall()

            album_ids = [row["id"] for row in rows]

    if not album_ids:
        return JSONResponse(
            {"error": "No albums match the requested criteria."},
            status_code=400,
        )

    try:
        job_manager.start(album_ids)
    except RuntimeError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=409,
        )

    return {
        "queued": len(album_ids),
        "album_ids": album_ids,
    }


# ---------------------------------------------------------------------------
# Status + SSE
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status():
    return {**job_manager.status, "running": job_manager.is_running()}


@app.get("/api/stream")
async def sse_stream(request: Request):
    """
    Server-Sent Events stream.
    Sends the current status snapshot immediately on connect, then pushes
    incremental updates as the sort job progresses.
    Keepalive comments sent every 15 s to prevent proxies closing the connection.
    """
    queue = job_manager.bus.subscribe()

    async def generator():
        try:
            # Snapshot on connect so the browser is immediately up to date
            yield {"data": json.dumps({**job_manager.status, "running": job_manager.is_running()})}

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {"data": json.dumps(event)}
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}   # SSE comment, not a message
        finally:
            job_manager.bus.unsubscribe(queue)

    return EventSourceResponse(generator())


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def read_settings():
    s = get_settings()
    # Mask the API key after the first 4 characters
    key = s.get("api_key", "")
    if len(key) > 4:
        s["api_key"] = key[:4] + "•" * 8
        s["api_key_set"] = True
    else:
        s["api_key_set"] = bool(key)
    return s


@app.put("/api/settings")
async def write_settings(request: Request):
    body = await request.json()
    # Don't overwrite the stored key with the masked preview
    if body.get("api_key", "").endswith("•" * 8):
        body.pop("api_key", None)
    save_settings(body)
    return {"status": "ok"}


@app.get("/api/test-connection")
async def test_connection():
    settings = get_settings()
    session  = build_session(settings)
    ok       = check_auth(session, settings.get("immich_url", ""))
    return {"connected": ok, "immich_url": settings.get("immich_url")}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@app.get("/api/history")
async def sort_history(limit: int = 50):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM sort_log ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Static files (must be last — catches everything else as index.html)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")