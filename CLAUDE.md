# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**PolyCheck** is a PyQt6 desktop app that shows Polymarket events closing within the next 3 days. It fetches event and market data from the Polymarket Gamma API, persists it locally in SQLite, and displays it in a filterable table. Double-clicking an event opens a detail window showing all markets with Yes/No prices and a visual ratio bar.

## Environment

- Python 3.13 (interpreter at `C:\Python313\python.exe`)
- The virtual environment is at the repository root — activate with:
  ```
  Scripts\Activate.ps1   # PowerShell
  Scripts\activate.bat   # CMD
  ```

## Running the app

```
python app.py
```

## Installing dependencies

```
pip install -r requirements.txt
```

Dependencies: `PyQt6`, `requests`, `py-clob-client`

## Source files

| File | Purpose |
|---|---|
| `app.py` | Entry point and all UI code |
| `db.py` | SQLite persistence layer |
| `polycheck.db` | Auto-created SQLite database (not tracked by git) |
| `requirements.txt` | Pinned dependencies |

## Architecture

### Data flow

1. On startup, `db.load_events()` populates the table immediately from the local cache.
2. `EventFetcher` (QThread) calls `GET https://gamma-api.polymarket.com/events` with `end_date_min=now` and `end_date_max=now+3days`, paginating through all results.
3. On success, `db.upsert_events()` persists the results (`INSERT OR REPLACE` keyed on event `id`), then the UI re-renders.
4. Auto-refresh runs every 60 seconds.

### Key classes (`app.py`)

- **`EventFetcher`** — QThread; emits `events_ready(list)`, `error_occurred(str)`, `status_update(str)`.
- **`MainWindow`** — main window with filterable table. Category combo and title search box both feed `_apply_filter()`. Stores each event dict on the title `QTableWidgetItem` via `UserRole`; double-click opens `EventDetailWindow`.
- **`EventDetailWindow`** — standalone window per event showing all markets in a table with Yes %, No %, and a `RatioBar` widget.
- **`RatioBar`** — custom `QWidget` that paints a green/red split bar using `QPainter`.

### API note

`py-clob-client` wraps the CLOB trading API (`clob.polymarket.com`). Event/market metadata comes from the separate Gamma API (`gamma-api.polymarket.com`) and is fetched directly via `requests`. A `ClobClient` instance is initialized in `MainWindow` for future CLOB operations.

### Database schema (`db.py`)

Table `events`: `id` (PK), `title`, `end_date`, `volume_24h`, `volume`, `liquidity`, `market_count`, `category`, `fetched_at`, `raw` (full JSON blob). Index on `end_date`.

## .gitignore note

The root `.gitignore` uses a blanket `*` — new source files must be explicitly un-ignored (e.g. `!newfile.py`) or force-added with `git add --force`.
