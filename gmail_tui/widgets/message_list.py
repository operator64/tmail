from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widgets import DataTable

from ..models import MessageSummary


def _fmt_date(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    local_now = now.astimezone()
    time_str = local.strftime("%H:%M")
    if local.date() == local_now.date():
        return time_str
    delta = (local_now.date() - local.date()).days
    if delta == 1:
        return f"Y {time_str}"
    if 1 < delta < 7:
        return f"{local.strftime('%a')} {time_str}"
    if local.year == local_now.year:
        return local.strftime("%d.%b")
    return local.strftime("%d.%m.%y")


def _elide(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s.ljust(n)
    return s[: n - 1] + "…"


def _short_from(addr: str) -> str:
    # "Name <mail@x>" → "Name"; "mail@x" → "mail@x"
    addr = addr or ""
    if "<" in addr:
        return addr.split("<", 1)[0].strip().strip('"') or addr
    return addr


class MessageOpened(Message):
    def __init__(self, message_id: str) -> None:
        super().__init__()
        self.message_id = message_id


class MessageContextMenuRequested(Message):
    def __init__(self, message_id: str, x: int, y: int) -> None:
        super().__init__()
        self.message_id = message_id
        self.x = x
        self.y = y


class SelectionChanged(Message):
    def __init__(self, selected_ids: list[str]) -> None:
        super().__init__()
        self.selected_ids = selected_ids


class LoadMoreRequested(Message):
    pass


class MessageList(DataTable):
    DEFAULT_CSS = """
    MessageList { height: 1fr; }
    """

    def __init__(self) -> None:
        super().__init__(zebra_stripes=False, cursor_type="row", header_height=1)
        self._summaries: dict[str, MessageSummary] = {}
        self._ordered_ids: list[str] = []
        self._selected: set[str] = set()

    def on_mount(self) -> None:
        self.add_column("★", width=2, key="star")
        self.add_column("📎", width=2, key="att")
        self.add_column("Date", width=9, key="date")
        self.add_column("From", width=18, key="from")
        self.add_column("Subject", key="subject")

    def set_summaries(
        self,
        items: list[MessageSummary],
        preserve_cursor: bool = True,
    ) -> None:
        prev_key = self._current_row_key() if preserve_cursor else None
        prev_selected = set(self._selected) if preserve_cursor else set()
        prev_scroll_y = self.scroll_y if preserve_cursor else 0
        self.clear()
        self._summaries = {}
        self._ordered_ids = []
        self._selected = set()
        self.append_summaries(items)
        if not preserve_cursor:
            return
        still_there = prev_selected & set(self._ordered_ids)
        if still_there:
            self._selected = still_there
            for sid in still_there:
                if sid in self._summaries:
                    self.update_summary(self._summaries[sid])
        if prev_key and prev_key in self._ordered_ids:
            idx = self._ordered_ids.index(prev_key)

            def restore() -> None:
                try:
                    self.move_cursor(row=idx, animate=False)
                except TypeError:
                    self.move_cursor(row=idx)
                self.scroll_to(y=prev_scroll_y, animate=False)

            self.call_after_refresh(restore)

    def append_summaries(self, items: list[MessageSummary]) -> None:
        for m in items:
            if m.id in self._summaries:
                continue
            self._summaries[m.id] = m
            self._ordered_ids.append(m.id)
            self.add_row(*self._render_row(m), key=m.id)

    def update_summary(self, m: MessageSummary) -> None:
        self._summaries[m.id] = m
        if m.id not in self._ordered_ids:
            self._ordered_ids.append(m.id)
            self.add_row(*self._render_row(m), key=m.id)
            return
        try:
            cells = self._render_row(m)
            self.update_cell(m.id, "star", cells[0])
            self.update_cell(m.id, "att", cells[1])
            self.update_cell(m.id, "date", cells[2])
            self.update_cell(m.id, "from", cells[3])
            self.update_cell(m.id, "subject", cells[4])
        except Exception:
            pass

    def remove_id(self, message_id: str) -> None:
        if message_id in self._summaries:
            del self._summaries[message_id]
        if message_id in self._ordered_ids:
            self._ordered_ids.remove(message_id)
        self._selected.discard(message_id)
        try:
            self.remove_row(message_id)
        except Exception:
            pass

    def _render_row(self, m: MessageSummary) -> tuple[Text, Text, Text, Text, Text]:
        bold = m.is_unread
        style = "bold" if bold else "dim"
        sel_marker = "●" if m.id in self._selected else ""
        star = Text("★" if m.is_starred else " ", style="yellow" if m.is_starred else "")
        att = Text("📎" if m.has_attachment else " ", style=style)
        date_col = Text(_fmt_date(m.date), style=style)
        from_col = Text(_elide(_short_from(m.from_addr), 16) + " " + sel_marker, style=style)
        subj = m.subject or "(no subject)"
        snip = m.snippet or ""
        combined = subj
        if snip:
            combined = f"{subj}  —  {snip}"
        subject_col = Text(combined, style=style, overflow="ellipsis", no_wrap=True)
        return star, att, date_col, from_col, subject_col

    # ---------------- interaction ----------------

    def selected_ids(self) -> list[str]:
        if self._selected:
            return [i for i in self._ordered_ids if i in self._selected]
        # fallback: current row only
        key = self._current_row_key()
        return [key] if key else []

    def _current_row_key(self) -> Optional[str]:
        try:
            return self.coordinate_to_cell_key(self.cursor_coordinate).row_key.value
        except Exception:
            return None

    def on_key(self, event: events.Key) -> None:
        if event.key == "space":
            key = self._current_row_key()
            if key:
                if key in self._selected:
                    self._selected.remove(key)
                else:
                    self._selected.add(key)
                if key in self._summaries:
                    self.update_summary(self._summaries[key])
                self.post_message(SelectionChanged(list(self._selected)))
                event.stop()
        elif event.key == "j":
            self.action_cursor_down()
            event.stop()
        elif event.key == "k":
            self.action_cursor_up()
            event.stop()
        elif event.key == "G":
            if self._ordered_ids:
                self.move_cursor(row=len(self._ordered_ids) - 1)
            event.stop()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key:
            self.post_message(MessageOpened(key))

    async def on_click(self, event: events.Click) -> None:
        if event.button == 3:
            key = self._current_row_key()
            if key:
                self.post_message(
                    MessageContextMenuRequested(
                        key,
                        x=event.screen_x,
                        y=event.screen_y,
                    )
                )
                event.stop()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # near bottom? request more
        if not self._ordered_ids:
            return
        try:
            idx = self._ordered_ids.index(event.row_key.value)
        except (ValueError, AttributeError):
            return
        if idx >= len(self._ordered_ids) - 5:
            self.post_message(LoadMoreRequested())
