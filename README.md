# Immich Sequencer

A self-hosted web app that sorts albums in [Immich](https://immich.app) by filename order. It assigns sequential timestamps to assets so Immich displays them in the correct alphabetical sequence — useful for scanned documents, comic pages, photo sets named by sequence number, or any album where filename order should define the timeline.

> **Compatible with Immich 3.x** (tested on 3.1.0).

---

## Screenshots

```
┌──────────────────────────────────────────────────────────────────┐
│ 🗂 Immich Sequencer  ● Connected          [↻ Scan] [⚙ Settings] │
├──────────────────────────────────────────────────────────────────┤
│  1024 Total   45 New   12 Updated   3 Interrupted   964 Sorted  │
├──────────────────────────────────────────────────────────────────┤
│  🔍 Search…   [▶ Sort New]  [▶ Sort Updated]  [▶ Sort All]      │
├──────────────────────────────────────────────────────────────────┤
│  Album                    Status       Assets   Last Sorted      │
│  Holiday 2023             ✅ Sorted      142    Jan 5, 2024      │
│  Birthday Party           🆕 New          38    —                │
│  Scanned Book Vol 1       🔄 Updated       89   Jan 3, 2024      │
│  Vacation Photos          ⚠️ Interrupted   23   Jan 4, 2024      │
└──────────────────────────────────────────────────────────────────┘

[ Live progress panel when sorting ]
  Sorting: 'Holiday 2023'  (Album 45 of 1024)           [✕ Cancel]
  Album   [████████████░░░░░░░░░░░░] 89/142  63%
  Overall [███░░░░░░░░░░░░░░░░░░░░░] 45/1024  4%   ETA 2h 14m
```

---

## How it works

Immich orders assets within a group by `dateTimeOriginal`. This app fetches every asset in an album, sorts them alphabetically by filename using **natural sort** (`page10` comes after `page9`, not before), then stamps each asset with a unique sequential timestamp.

**Auto range mode (default):** The start and end timestamps are derived from the album's own existing `dateTimeOriginal` values, so the album stays anchored to its current position in your timeline. Assets are spread evenly across that span.

**Outlier filtering:** Before computing the range, timestamps before `MIN_YEAR` (default: 1990) and statistical outliers (Tukey 1.5× IQR fence) are discarded. This prevents a single corrupt timestamp — such as a camera with a dead clock battery defaulting to 1969 — from stretching the range across decades.

No original files are ever modified. Only Immich's internal `dateTimeOriginal` database field is updated — the same field you edit when you change a date manually in the Immich web UI.

---

## Requirements

- Docker and Docker Compose
- Immich 3.x running and accessible on your network
- An Immich API key with four scopes (see [API Key](#api-key))

---

## Quick start

```bash
# 1. Clone or download this repository
git clone https://github.com/yourname/immich-sequencer
cd immich-sequencer

# 2. Edit docker-compose.yml with your Immich URL and API key
nano docker-compose.yml

# 3. Start the container
docker compose up -d

# 4. Open the UI
open http://localhost:8080
```

On first launch the app automatically scans your Immich instance and imports all albums.

---

## Configuration

### docker-compose.yml

```yaml
services:
  immich-sequencer:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./data:/data       # SQLite database — survives container restarts
    environment:
      - IMMICH_URL=http://immich-server:2283
      - API_KEY=your-api-key-here
    restart: unless-stopped
```

The environment variables seed the settings database on **first run only**. After that, all settings are managed through the **Settings** panel in the UI — no container restart needed.

### Settings panel

All settings are available at runtime via ⚙ Settings:

| Setting | Default | Description |
|---|---|---|
| Immich URL | `http://immich-server:2283` | Base URL of your Immich instance |
| API Key | — | Your Immich API key (see below) |
| Min Year | `1990` | Timestamps before this year are treated as outliers |
| HTTP Method | `PUT` | `PUT` for Immich v3, `PATCH` for v4+ |
| Base Time Override | *(empty)* | ISO 8601 timestamp; leave empty for auto range detection |
| Request Delay | `0.05s` | Pause between per-asset API calls |
| Album Delay | `1.0s` | Pause between albums in a batch |
| Max Retries | `3` | Retry attempts for failed asset updates |

---

## API Key

Create a dedicated key in Immich under **Account Settings → API Keys → New API Key**.

Only four scopes are required:

| Scope | Used for |
|---|---|
| `user.read` | Verifying connectivity on startup |
| `album.read` | Fetching album metadata and the full album list |
| `asset.read` | Searching for assets within each album via the search API |
| `asset.update` | Writing the new `dateTimeOriginal` timestamps |

> `asset.read` is required in Immich v3 because assets are fetched via `POST /api/search/metadata` — they are no longer included in the album response.

---

## Album statuses

| Status | Meaning |
|---|---|
| 🆕 **New** | Album exists in Immich but has never been sorted |
| ✅ **Sorted** | Last sort completed successfully and the file set hasn't changed |
| 🔄 **Updated** | Album was sorted before but Immich reports a different asset count (photos added or removed) |
| ⚠️ **Interrupted** | A sort was started but was cancelled, or the container was stopped mid-run |
| ⏳ **Sorting** | Currently being processed |
| 📭 **Empty** | Album exists but has no assets |
| ❌ **Error** | Last sort attempt failed entirely (hover for details) |

Albums transition between states automatically:

- **Sorted → Updated** when the asset count changes on the next scan
- **Sorting → Interrupted** when the container restarts with a sort in progress
- **Any → Sorted** when a sort completes successfully

---

## Usage

### Scanning

Click **↻ Scan** to refresh the album list from Immich. The scan:

- Imports new albums as **New**
- Detects asset count changes on **Sorted** albums and promotes them to **Updated**
- Detects albums stuck in **Sorting** (from a previous crashed run) and marks them **Interrupted**
- Removes albums that no longer exist in Immich

### Sorting

Use the toolbar buttons to queue a sort job:

| Button | Albums queued |
|---|---|
| **▶ Sort New** | All albums with status New |
| **▶ Sort Updated** | All albums with status Updated |
| **▶ Sort All Pending** | New + Updated + Interrupted |
| **▶** (row button) | That album only |
| **▶ Sort Selected** | Checked albums (use checkboxes to build the list) |

Only one sort job runs at a time. While a job is running the progress panel shows:

- Current album name and asset-level progress bar
- Batch-level progress bar with ETA

### Cancelling

Click **✕ Cancel** in the progress panel. The sort finishes the current asset and stops. The album being sorted at the time is marked **Interrupted**; all remaining queued albums stay in their previous status and will be picked up on the next sort.

### Filtering

Click any status card at the top to filter the album table. Click again or click **Total** to clear the filter. Use the search box to find albums by name.

---

## Time range modes

Each album sort reports which mode was used:

**Auto** — The oldest and newest timestamps in the album are detected and assets are spread evenly across that span. The interval between assets is `(newest − oldest) / (asset_count − 1)`. This is the default and recommended mode.

**Auto (1s)** — Auto was used but the detected span was too narrow to give each asset at least 1 second of separation, so 1-second increments are used from the oldest timestamp instead.

**Fixed base** — A **Base Time Override** was set in settings. Assets are stamped 1 second apart starting from that value.

**Fallback** — No usable timestamps were found in the album (all were outliers or missing). Assets are stamped 1 second apart starting from `2000-01-01T00:00:00+00:00`.

The mode, base timestamp, and per-asset interval are stored in the database and shown as a subtitle under each sorted album name.

---

## Recovering from the 1969 / epoch timestamp problem

If you ran a sort before outlier filtering was in place and an album's timestamps are now spread across decades:

1. In Immich go to **Administration → Jobs → Extract Metadata → Run All**. Since the script only modifies the database field and never touches the files, the original EXIF timestamps are still intact on disk. This restores them.

2. Find the image with the corrupt timestamp (search Immich for dates around 1969–1970) and correct its date manually in Immich.

3. Click **↻ Scan** in the Sequencer — affected albums will appear as **Updated**.

4. Sort the affected albums again. With outlier filtering active, the 1969 timestamp will be excluded from range detection automatically.

---

## Upgrading to Immich v4

Immich v3 deprecated `PUT /api/assets/{id}` in favour of `PATCH`. Both verbs are accepted in v3.x. When you upgrade to Immich v4:

1. Open ⚙ Settings
2. Change **HTTP Method** from `PUT` to `PATCH`
3. Click **Save**

No container rebuild needed.

---

## Data persistence

The SQLite database is stored at `/data/sequencer.db` inside the container, mounted to `./data/sequencer.db` on the host via the volume in `docker-compose.yml`. It contains:

- **`settings`** — all configuration values
- **`albums`** — album state, sort history, and progress tracking
- **`sort_log`** — audit log of every sort job run

Back up the `./data` directory to preserve your sort history. Deleting it resets everything to a clean state (settings revert to the values in `docker-compose.yml`; albums re-import as **New** on the next scan).

---

## Architecture

```
immich-sequencer/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── app/
    ├── main.py        FastAPI app, all HTTP routes, SSE endpoint
    ├── database.py    SQLite schema, state transitions, helpers
    ├── immich.py      Immich API client, timestamp logic, outlier filtering
    ├── sorter.py      Background job manager, thread↔asyncio event bus
    └── static/
        └── index.html Single-page UI (Alpine.js + Tailwind CDN)
```

**Backend:** FastAPI + uvicorn. The sort runs in a daemon thread so the API stays fully responsive during a long batch. Progress is pushed to the browser via **Server-Sent Events** (SSE) — no polling, no WebSocket handshake overhead.

**Thread safety:** The SQLite connection uses WAL mode, allowing concurrent reads from the API thread and writes from the sort thread. The SSE event bus uses `asyncio.loop.call_soon_threadsafe()` to bridge thread-originated events into the async event loop cleanly.

**Frontend:** A single HTML file with no build step. Alpine.js handles reactivity; Tailwind CSS (CDN) handles styling. The SSE handler merges partial updates into local state so the table updates per-asset without full-page refreshes or flickering.

---

## Troubleshooting

**"Disconnected" shown in the header**
Check that `IMMICH_URL` is reachable from inside the container. If Immich is on the same Docker network, use the service name (`http://immich-server:2283`), not `localhost`.

**Albums show as New after every scan**
The API key is missing `album.read` or `asset.read` scope.

**Sort fails immediately with HTTP 401/403**
The API key is missing `asset.update` scope, or the key has expired.

**Timestamps are still wrong after sorting**
In Immich, go to **Administration → Jobs → Run EXIF extraction** on the affected albums. Then re-sort. If a corrupt timestamp (e.g. 1969) was the root cause, see [Recovering from the epoch problem](#recovering-from-the-1969--epoch-timestamp-problem).

**Container exits immediately**
Check logs with `docker compose logs`. The most common cause is a missing `/data` directory — this is created automatically, but if the volume mount path has a permission issue on the host, uvicorn will fail to start.

**"A sort job is already running" after a crash**
The previous job's thread is gone but the API thinks it's still running. Restart the container: `docker compose restart`. The crashed album will be marked **Interrupted** on the next scan.

## Authentication and deployment security

The web interface and API are protected by OpenID Connect (OIDC). Pocket ID is supported, as are other OIDC providers.

Configure these environment variables before starting the container:

| Variable | Purpose |
| --- | --- |
| `OIDC_ISSUER_URL` | Provider issuer URL; discovery is loaded from its `/.well-known/openid-configuration` endpoint |
| `OIDC_CLIENT_ID` | OIDC client ID |
| `OIDC_CLIENT_SECRET` | OIDC client secret |
| `OIDC_REDIRECT_URI` | Exact callback URL registered with the provider, ending in `/api/auth/callback` |
| `SESSION_SECRET` | Long random secret used to sign the session cookie |
| `OIDC_ALLOWED_EMAILS` | Optional comma-separated email allowlist; empty allows any account accepted by the provider |
| `SESSION_COOKIE_SECURE` | Keep `true` over HTTPS; set `false` only for isolated local HTTP testing |

Generate a session secret with `openssl rand -hex 32`. Register the callback URL exactly as configured in `OIDC_REDIRECT_URI`. Do not commit client secrets, API keys, or session secrets.

Authentication fails closed when OIDC configuration is missing. `AUTH_DISABLED=true` is intended only for isolated local development and must not be used on a network-accessible deployment. Use HTTPS and keep the application behind an appropriate access-control layer.

The settings API never returns the Immich API key. Leaving the API-key field blank preserves the existing key. Immich URLs must be absolute HTTP(S) URLs without embedded credentials or query strings, and Immich requests do not follow redirects, preventing the API key from being forwarded to a different host.

The application continues to store its SQLite database at `/data/sequencer.db`. Keep the existing `/data` volume when upgrading.
