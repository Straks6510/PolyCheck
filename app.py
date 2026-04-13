#!/usr/bin/env python3
"""
PolyCheck — Polymarket event viewer.
Shows all active events closing within the next 3 days.

Data sources:
  - Gamma API  (https://gamma-api.polymarket.com) — events/markets metadata
  - CLOB API   (https://clob.polymarket.com)       — via py-clob-client
"""

import json
import sys
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional

import db

from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtCore import Qt, QThread, QEvent, pyqtSignal, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPalette, QStandardItem, QStandardItemModel

from py_clob_client.client import ClobClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
REFRESH_INTERVAL_MS = 60_000   # auto-refresh every 60 s
LOOKAHEAD_DAYS = 3
PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_currency(value) -> str:
    """Format a numeric dollar amount with K / M suffix."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:.2f}"


def parse_dt(raw: str) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string, handling the 'Z' suffix."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def time_remaining(end_date_str: str) -> tuple[str, str]:
    """
    Return (human_readable_time_left, hex_color).
    Colors: red < 24 h, orange < 48 h, gold < 72 h.
    """
    end = parse_dt(end_date_str)
    if end is None:
        return "—", "#888888"

    now = datetime.now(timezone.utc)
    delta = end - now
    total_seconds = delta.total_seconds()

    if total_seconds <= 0:
        return "Ended", "#666666"

    days = int(total_seconds // 86400)
    hours = int((total_seconds % 86400) // 3600)
    minutes = int((total_seconds % 3600) // 60)

    if total_seconds < 3600:
        return f"{minutes}m", "#FF4444"
    if total_seconds < 86400:
        return f"{hours}h {minutes}m", "#FF4444"
    if total_seconds < 172800:
        return f"1d {hours}h", "#FF8C00"
    return f"{days}d {hours}h", "#FFD700"


# ---------------------------------------------------------------------------
# Helpers for the detail window
# ---------------------------------------------------------------------------

def _parse_yes_no(market: dict) -> tuple[float | None, float | None]:
    """
    Return (yes_price, no_price) in [0, 1], or (None, None) on failure.
    Both outcomePrices and outcomes are JSON-encoded strings in the API response.
    """
    try:
        prices_raw = market.get("outcomePrices")
        if not prices_raw:
            return None, None
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else list(prices_raw)

        outcomes_raw = market.get("outcomes")
        outcomes = (
            json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else list(outcomes_raw or [])
        )

        yes_idx, no_idx = 0, 1
        for i, o in enumerate(outcomes):
            lo = str(o).lower()
            if lo == "yes":
                yes_idx = i
            elif lo == "no":
                no_idx = i

        yes = float(prices[yes_idx]) if yes_idx < len(prices) else None
        no = float(prices[no_idx]) if no_idx < len(prices) else None
        return yes, no
    except (json.JSONDecodeError, ValueError, IndexError, TypeError):
        return None, None


class CheckableComboBox(QComboBox):
    """A combo box where every item has a checkbox — supports multi-category filtering."""

    selectionChanged = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setModel(QStandardItemModel(self))
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText("All categories")
        self.lineEdit().installEventFilter(self)
        self.view().pressed.connect(self._on_item_pressed)
        self._dirty = False  # tracks whether anything changed during current popup

    # ------------------------------------------------------------------
    # Popup control — keep open while clicking items; close on outside click
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        if obj is self.lineEdit() and event.type() == QEvent.Type.MouseButtonRelease:
            if self.view().isVisible():
                super().hidePopup()
            else:
                super().showPopup()
            return True
        return super().eventFilter(obj, event)

    def hidePopup(self) -> None:
        super().hidePopup()
        if self._dirty:
            self._dirty = False
            self.selectionChanged.emit()

    def showPopup(self) -> None:
        self._dirty = False
        super().showPopup()

    # ------------------------------------------------------------------
    # Item management
    # ------------------------------------------------------------------

    def addItem(self, text: str, data=None) -> None:  # type: ignore[override]
        item = QStandardItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        item.setData(Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole)
        self.model().appendRow(item)
        self._refresh_text()

    def clear(self) -> None:
        self.model().clear()
        self._refresh_text()

    def _on_item_pressed(self, index) -> None:
        item = self.model().itemFromIndex(index)
        new_state = (
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        item.setCheckState(new_state)
        self._dirty = True
        self._refresh_text()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def checkedItems(self) -> list[str]:
        """Return the labels of all checked items."""
        result = []
        for i in range(self.model().rowCount()):
            item = self.model().item(i)
            if item and item.checkState() == Qt.CheckState.Checked:
                result.append(item.text())
        return result

    def setChecked(self, text: str, checked: bool) -> None:
        """Check or uncheck an item by its label (no-op if not found)."""
        for i in range(self.model().rowCount()):
            item = self.model().item(i)
            if item and item.text() == text:
                item.setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
                break
        self._refresh_text()

    def _refresh_text(self) -> None:
        checked = self.checkedItems()
        if not checked:
            self.lineEdit().setText("")
            self.lineEdit().setPlaceholderText("All categories")
        elif len(checked) <= 2:
            self.lineEdit().setText(", ".join(checked))
        else:
            self.lineEdit().setText(f"{len(checked)} categories")


class RatioBar(QWidget):
    """Horizontal bar showing yes (green) / no (red) split."""

    _YES_COLOR = QColor("#a6e3a1")
    _NO_COLOR = QColor("#f38ba8")
    _BG_COLOR = QColor("#313244")

    def __init__(self, yes_frac: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._yes = max(0.0, min(1.0, yes_frac))
        self.setFixedHeight(14)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        w, h, r = self.width(), self.height(), 3
        yes_w = round(w * self._yes)
        no_w = w - yes_w

        # Background
        p.setBrush(self._BG_COLOR)
        p.drawRoundedRect(0, 0, w, h, r, r)

        # Yes slice
        if yes_w > 0:
            p.setBrush(self._YES_COLOR)
            p.drawRoundedRect(0, 0, yes_w, h, r, r)
            if no_w > 0:
                # Square off the right edge of the yes slice
                p.drawRect(max(0, yes_w - r), 0, r, h)

        # No slice
        if no_w > 0:
            p.setBrush(self._NO_COLOR)
            p.drawRoundedRect(yes_w, 0, no_w, h, r, r)
            if yes_w > 0:
                # Square off the left edge of the no slice
                p.drawRect(yes_w, 0, r, h)

        p.end()


# ---------------------------------------------------------------------------
# Event detail window
# ---------------------------------------------------------------------------

class EventDetailWindow(QWidget):
    """Shows all markets for a single event with Yes/No prices and ratio bars."""

    _COLUMNS: list[tuple[str, int]] = [
        ("Market", 0),       # stretch
        ("ID", 130),         # market id — fixed
        ("Yes", 65),
        ("No", 65),
        ("Ratio", 180),
    ]

    def __init__(self, event: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._event_id = str(event.get("id", ""))
        self.resize(1000, 560)
        self._build_ui(event)

    def _build_ui(self, event: dict) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        # Title
        self._title_lbl = QLabel()
        f = QFont()
        f.setPointSize(13)
        f.setBold(True)
        self._title_lbl.setFont(f)
        self._title_lbl.setWordWrap(True)
        layout.addWidget(self._title_lbl)

        # Subtitle row: end date + counts
        self._sub_lbl = QLabel()
        layout.addWidget(self._sub_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        # Markets table
        self._market_table = QTableWidget()
        self._market_table.setColumnCount(len(self._COLUMNS))
        self._market_table.setHorizontalHeaderLabels([c[0] for c in self._COLUMNS])
        self._market_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._market_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._market_table.setAlternatingRowColors(True)
        self._market_table.setSortingEnabled(False)
        self._market_table.verticalHeader().setVisible(False)
        self._market_table.setShowGrid(False)
        self._market_table.setWordWrap(True)

        hdr = self._market_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, (_, width) in enumerate(self._COLUMNS[1:], start=1):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self._market_table.setColumnWidth(col, width)

        layout.addWidget(self._market_table)

        self._populate(event)

    def _populate(self, event: dict) -> None:
        title = event.get("title") or "Event detail"
        self.setWindowTitle(f"{title}  [{self._event_id}]")
        self._title_lbl.setText(title)

        end_raw = event.get("endDate", "")
        end_dt = parse_dt(end_raw)
        markets: list[dict] = event.get("markets") or []
        time_text, time_color = time_remaining(end_raw)

        sub_parts = []
        if end_dt:
            sub_parts.append(end_dt.strftime("Closes %b %d, %Y  %H:%M UTC"))
        if time_text not in ("—", "Ended"):
            sub_parts.append(f"({time_text} remaining)")
        sub_parts.append(f"· {len(markets)} market{'s' if len(markets) != 1 else ''}")
        self._sub_lbl.setText("  ".join(sub_parts))
        self._sub_lbl.setStyleSheet(f"color: {time_color};")

        self._market_table.setRowCount(len(markets))
        for row, market in enumerate(markets):
            yes, no = _parse_yes_no(market)

            q_item = QTableWidgetItem(market.get("question") or "—")
            q_item.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )
            self._market_table.setItem(row, 0, q_item)

            mid = str(market.get("id") or "—")
            id_item = QTableWidgetItem(mid)
            id_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            id_item.setForeground(QColor("#888888"))
            self._market_table.setItem(row, 1, id_item)

            yes_item = QTableWidgetItem(f"{yes:.1%}" if yes is not None else "—")
            yes_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            yes_item.setForeground(QColor("#a6e3a1"))
            self._market_table.setItem(row, 2, yes_item)

            no_item = QTableWidgetItem(f"{no:.1%}" if no is not None else "—")
            no_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            no_item.setForeground(QColor("#f38ba8"))
            self._market_table.setItem(row, 3, no_item)

            if yes is not None:
                bar_wrap = QWidget()
                bar_wrap.setAutoFillBackground(False)
                bar_layout = QHBoxLayout(bar_wrap)
                bar_layout.setContentsMargins(8, 4, 8, 4)
                bar_layout.addWidget(RatioBar(yes))
                self._market_table.setCellWidget(row, 4, bar_wrap)

        self._market_table.resizeRowsToContents()

    def refresh(self, event: dict) -> None:
        """Re-populate with updated event data (called on each auto-refresh)."""
        self._populate(event)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class EventFetcher(QThread):
    """Fetches events from the Gamma API in a background thread."""

    events_ready = pyqtSignal(list)
    error_occurred = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def run(self) -> None:
        try:
            now = datetime.now(timezone.utc)
            end_max = now + timedelta(days=LOOKAHEAD_DAYS)

            all_events: list[dict] = []
            offset = 0

            while True:
                self.status_update.emit(
                    f"Fetching events… ({len(all_events)} so far)"
                )
                params = {
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_date_max": end_max.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "active": "true",
                    "closed": "false",
                    "order": "endDate",
                    "ascending": "true",
                }
                resp = requests.get(
                    f"{GAMMA_API}/events",
                    params=params,
                    timeout=20,
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                page: list[dict] = resp.json()

                if not page:
                    break

                all_events.extend(page)

                if len(page) < PAGE_SIZE:
                    break

                offset += PAGE_SIZE

            db.upsert_events(all_events)
            self.events_ready.emit(all_events)

        except requests.RequestException as exc:
            self.error_occurred.emit(f"Network error: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.error_occurred.emit(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    # (label, width)  — col 0 is the ★ toggle, col 1 is Title (stretches)
    COLUMNS: list[tuple[str, int]] = [
        ("★", 30),
        ("Title", 380),
        ("End Date (UTC)", 145),
        ("Time Left", 90),
        ("Markets", 72),
        ("Vol 24 h", 100),
        ("Vol Total", 105),
        ("Liquidity", 105),
        ("Category", 120),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PolyCheck — Events closing in 3 days")
        self.resize(1300, 740)

        self._all_events: list[dict] = []
        self._fetcher: Optional[EventFetcher] = None
        self._detail_windows: list[EventDetailWindow] = []
        self._favorites: set[str] = db.load_favorites()

        # Initialise py-clob-client (read-only, no credentials required)
        self._clob = ClobClient(CLOB_API)

        self._build_ui()

        # Load cached events from the database immediately (before first fetch)
        self._load_from_db()

        # Auto-refresh timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(REFRESH_INTERVAL_MS)

        # Kick off the first live fetch in the background
        self._refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _make_table(self) -> QTableWidget:
        """Create a QTableWidget pre-configured with the standard column layout."""
        t = QTableWidget()
        t.setColumnCount(len(self.COLUMNS))
        t.setHorizontalHeaderLabels([c[0] for c in self.COLUMNS])
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        t.setAlternatingRowColors(True)
        t.setSortingEnabled(True)
        t.verticalHeader().setVisible(False)
        t.setShowGrid(False)
        t.setWordWrap(False)

        hdr = t.horizontalHeader()
        # col 0: ★ (fixed narrow)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        t.setColumnWidth(0, self.COLUMNS[0][1])
        # col 1: Title (stretches)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # col 2+: fixed widths
        for col in range(2, len(self.COLUMNS)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            t.setColumnWidth(col, self.COLUMNS[col][1])

        return t

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(8)

        # ── Header row ──────────────────────────────────────────────
        header_row = QHBoxLayout()

        title = QLabel("Polymarket  ·  Events closing in the next 3 days")
        title_font = QFont()
        title_font.setPointSize(13)
        title_font.setBold(True)
        title.setFont(title_font)
        header_row.addWidget(title)
        header_row.addStretch()

        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #888;")
        header_row.addWidget(self._count_label)

        self._category_combo = CheckableComboBox()
        self._category_combo.setFixedWidth(180)
        self._category_combo.setToolTip("Filter by category — select one or more")
        self._category_combo.selectionChanged.connect(self._apply_filter)
        header_row.addWidget(self._category_combo)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by title…")
        self._search.setFixedWidth(220)
        self._search.textChanged.connect(self._apply_filter)
        header_row.addWidget(self._search)

        self._refresh_btn = QPushButton("⟳  Refresh")
        self._refresh_btn.setFixedWidth(100)
        self._refresh_btn.clicked.connect(self._refresh)
        header_row.addWidget(self._refresh_btn)

        layout.addLayout(header_row)

        # ── Divider ─────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(sep)

        # ── Tab widget ──────────────────────────────────────────────
        self._tabs = QTabWidget()
        layout.addWidget(self._tabs)

        # Events tab
        events_container = QWidget()
        events_layout = QVBoxLayout(events_container)
        events_layout.setContentsMargins(0, 4, 0, 0)
        self._table = self._make_table()
        self._table.cellClicked.connect(
            lambda r, c: self._on_cell_clicked(r, c, self._table)
        )
        self._table.cellDoubleClicked.connect(
            lambda r, c: self._on_row_double_clicked(r, c, self._table)
        )
        events_layout.addWidget(self._table)
        self._tabs.addTab(events_container, "Events")

        # Favorites tab
        fav_container = QWidget()
        fav_layout = QVBoxLayout(fav_container)
        fav_layout.setContentsMargins(0, 4, 0, 0)
        self._fav_table = self._make_table()
        self._fav_table.cellClicked.connect(
            lambda r, c: self._on_cell_clicked(r, c, self._fav_table)
        )
        self._fav_table.cellDoubleClicked.connect(
            lambda r, c: self._on_row_double_clicked(r, c, self._fav_table)
        )
        fav_layout.addWidget(self._fav_table)
        self._tabs.addTab(fav_container, "Favorites (0)")

        # ── Status bar ──────────────────────────────────────────────
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)

        self._updated_label = QLabel("Not yet loaded")
        self._statusbar.addPermanentWidget(self._updated_label)

    # ------------------------------------------------------------------
    # DB bootstrap
    # ------------------------------------------------------------------

    def _load_from_db(self) -> None:
        """Populate the table instantly from the local database."""
        events = db.load_events()
        if not events:
            self._statusbar.showMessage("No cached data — fetching from Polymarket…")
            return
        self._all_events = events
        self._refresh_category_combo()
        self._apply_filter()
        last = db.last_fetched_at()
        if last:
            try:
                ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
                local = ts.astimezone().strftime("%b %d  %H:%M")
                self._updated_label.setText(f"Cached  {local}")
            except ValueError:
                self._updated_label.setText(f"Cached  {last}")
        self._statusbar.showMessage(
            f"Loaded {len(events)} events from cache — refreshing…", 5000
        )

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        if self._fetcher and self._fetcher.isRunning():
            return
        self._refresh_btn.setEnabled(False)
        self._statusbar.showMessage("Connecting to Polymarket Gamma API…")

        self._fetcher = EventFetcher()
        self._fetcher.events_ready.connect(self._on_events_ready)
        self._fetcher.error_occurred.connect(self._on_error)
        self._fetcher.status_update.connect(self._statusbar.showMessage)
        self._fetcher.start()

    def _on_events_ready(self, events: list[dict]) -> None:
        # Prune events that are no longer returned by the API
        db.delete_stale_events([str(e.get("id", "")) for e in events])

        self._all_events = events
        self._refresh_category_combo()
        self._apply_filter()
        now_str = datetime.now().strftime("%H:%M:%S")
        self._updated_label.setText(f"Updated {now_str}")
        self._statusbar.showMessage(f"Loaded {len(events)} events.", 5000)
        self._refresh_btn.setEnabled(True)

        # Refresh any open detail windows with updated event data
        events_by_id = {str(e.get("id", "")): e for e in events}
        for win in self._detail_windows:
            updated = events_by_id.get(win._event_id)
            if updated:
                win.refresh(updated)

    def _on_error(self, msg: str) -> None:
        self._statusbar.showMessage(f"Error — {msg}", 10_000)
        self._refresh_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Click / double-click handlers
    # ------------------------------------------------------------------

    def _on_cell_clicked(self, row: int, col: int, table: QTableWidget) -> None:
        """Toggle favorite when the ★ column (col 0) is clicked."""
        if col != 0:
            return
        title_item = table.item(row, 1)
        if title_item is None:
            return
        event = title_item.data(Qt.ItemDataRole.UserRole)
        if not event:
            return
        event_id = str(event.get("id", ""))
        if event_id in self._favorites:
            self._favorites.discard(event_id)
            db.remove_favorite(event_id)
        else:
            self._favorites.add(event_id)
            db.add_favorite(event_id)
        self._apply_filter()

    def _on_row_double_clicked(self, row: int, col: int, table: QTableWidget) -> None:
        if col == 0:
            return  # handled by cellClicked
        title_item = table.item(row, 1)
        if title_item is None:
            return
        event = title_item.data(Qt.ItemDataRole.UserRole)
        if not event:
            return
        win = EventDetailWindow(event, parent=None)
        win.destroyed.connect(lambda _, w=win: self._detail_windows.remove(w))
        self._detail_windows.append(win)
        win.show()
        win.raise_()

    # ------------------------------------------------------------------
    # Filtering & rendering
    # ------------------------------------------------------------------

    def _refresh_category_combo(self) -> None:
        """Rebuild the category dropdown from the current event list."""
        categories = sorted({
            self._event_category(e)
            for e in self._all_events
            if self._event_category(e) != "—"
        })
        previously_checked = set(self._category_combo.checkedItems())
        self._category_combo.blockSignals(True)
        self._category_combo.clear()
        for cat in categories:
            self._category_combo.addItem(cat)
            if cat in previously_checked:
                self._category_combo.setChecked(cat, True)
        self._category_combo.blockSignals(False)

    @staticmethod
    def _event_category(event: dict) -> str:
        tags: list[dict] = event.get("tags") or []
        return tags[0].get("label", "—") if tags else "—"

    def _apply_filter(self, _: str = "") -> None:
        q = self._search.text().strip().lower()
        selected_cats = set(self._category_combo.checkedItems())

        visible = [
            e for e in self._all_events
            if (not q or q in (e.get("title") or "").lower())
            and (not selected_cats or self._event_category(e) in selected_cats)
        ]
        self._count_label.setText(f"{len(visible)} of {len(self._all_events)} events")
        self._populate_table(visible, self._table)

        fav_visible = [
            e for e in visible
            if str(e.get("id", "")) in self._favorites
        ]
        self._populate_table(fav_visible, self._fav_table)
        self._tabs.setTabText(1, f"Favorites ({len(fav_visible)})")

    def _populate_table(self, events: list[dict], table: QTableWidget) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(len(events))

        for row, event in enumerate(events):
            end_raw = event.get("endDate", "")
            end_dt = parse_dt(end_raw)
            end_display = end_dt.strftime("%b %d  %H:%M") if end_dt else "—"

            time_text, time_color = time_remaining(end_raw)

            tags: list[dict] = event.get("tags") or []
            category = tags[0].get("label", "—") if tags else "—"

            markets: list[dict] = event.get("markets") or []

            event_id = str(event.get("id", ""))
            is_fav = event_id in self._favorites

            cells: list[tuple[str, Optional[str]]] = [
                ("★" if is_fav else "☆", "#FFD700" if is_fav else "#585b70"),
                (event.get("title") or "—", None),
                (end_display, None),
                (time_text, time_color),
                (str(len(markets)), None),
                (fmt_currency(event.get("volume24hr", 0)), None),
                (fmt_currency(event.get("volume", 0)), None),
                (fmt_currency(event.get("liquidity", 0)), None),
                (category, None),
            ]

            for col, (text, color) in enumerate(cells):
                item = QTableWidgetItem(text)
                align = (
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
                    if col == 1
                    else Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter
                )
                item.setTextAlignment(align)
                if color:
                    item.setForeground(QColor(color))
                if col == 1:
                    item.setData(Qt.ItemDataRole.UserRole, event)
                table.setItem(row, col, item)

        table.setSortingEnabled(True)
        table.resizeRowsToContents()


# ---------------------------------------------------------------------------
# Dark palette
# ---------------------------------------------------------------------------

def _dark_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window,          QColor("#1e1e2e"))
    p.setColor(QPalette.ColorRole.WindowText,      QColor("#cdd6f4"))
    p.setColor(QPalette.ColorRole.Base,            QColor("#181825"))
    p.setColor(QPalette.ColorRole.AlternateBase,   QColor("#1e1e2e"))
    p.setColor(QPalette.ColorRole.Text,            QColor("#cdd6f4"))
    p.setColor(QPalette.ColorRole.BrightText,      QColor("#ffffff"))
    p.setColor(QPalette.ColorRole.Button,          QColor("#313244"))
    p.setColor(QPalette.ColorRole.ButtonText,      QColor("#cdd6f4"))
    p.setColor(QPalette.ColorRole.Highlight,       QColor("#89b4fa"))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("#1e1e2e"))
    p.setColor(QPalette.ColorRole.Link,            QColor("#89b4fa"))
    p.setColor(QPalette.ColorRole.Midlight,        QColor("#313244"))
    p.setColor(QPalette.ColorRole.Dark,            QColor("#11111b"))
    p.setColor(QPalette.ColorRole.Mid,             QColor("#181825"))
    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    db.init()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(
        "QTableWidget { border: none; }"
        "QHeaderView::section {"
        "  background-color: #313244;"
        "  color: #cdd6f4;"
        "  padding: 4px 8px;"
        "  border: none;"
        "  font-weight: bold;"
        "}"
        "QLineEdit {"
        "  background: #313244;"
        "  border: 1px solid #45475a;"
        "  border-radius: 4px;"
        "  padding: 4px 8px;"
        "  color: #cdd6f4;"
        "}"
        "QPushButton {"
        "  background: #313244;"
        "  border: 1px solid #45475a;"
        "  border-radius: 4px;"
        "  padding: 5px 10px;"
        "  color: #cdd6f4;"
        "}"
        "QPushButton:hover  { background: #45475a; }"
        "QPushButton:pressed { background: #585b70; }"
        "QPushButton:disabled { color: #585b70; }"
        "QComboBox {"
        "  background: #313244;"
        "  border: 1px solid #45475a;"
        "  border-radius: 4px;"
        "  padding: 4px 8px;"
        "  color: #cdd6f4;"
        "}"
        "QComboBox::drop-down { border: none; width: 20px; }"
        "QComboBox QAbstractItemView {"
        "  background: #313244;"
        "  color: #cdd6f4;"
        "  selection-background-color: #89b4fa;"
        "  selection-color: #1e1e2e;"
        "  border: 1px solid #45475a;"
        "}"
        "QStatusBar { color: #888; }"
        "QTabWidget::pane { border: none; }"
        "QTabBar::tab {"
        "  background: #313244;"
        "  color: #cdd6f4;"
        "  padding: 6px 16px;"
        "  border: 1px solid #45475a;"
        "  border-bottom: none;"
        "  border-radius: 4px 4px 0 0;"
        "  margin-right: 2px;"
        "}"
        "QTabBar::tab:selected { background: #45475a; color: #cdd6f4; }"
        "QTabBar::tab:hover    { background: #3d3f56; }"
    )

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
