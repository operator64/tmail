from __future__ import annotations

from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button


class ContextAction(Message):
    def __init__(self, action: str, message_id: str) -> None:
        super().__init__()
        self.action = action
        self.message_id = message_id


ITEMS: list[tuple[str, str]] = [
    ("reply", "Reply (r)"),
    ("reply_all", "Reply All (R)"),
    ("forward", "Forward (f)"),
    ("toggle_star", "Toggle Star (s)"),
    ("label", "Assign Label… (l)"),
    ("archive", "Archive (e)"),
    ("toggle_unread", "Mark Unread/Read (u)"),
    ("trash", "Move to Trash (#)"),
]


class ContextMenu(ModalScreen[None]):
    DEFAULT_CSS = """
    ContextMenu { align: left top; }
    #ctx-box {
        background: $surface; border: round $accent; padding: 0;
        width: 30; height: auto;
    }
    #ctx-box Button {
        width: 100%; height: 1; margin: 0; padding: 0 1;
        border: none; background: $surface; text-align: left;
    }
    #ctx-box Button:hover { background: $boost; }
    """

    BINDINGS = [
        Binding("escape", "dismiss_none", "Close", show=False),
    ]

    def __init__(self, message_id: str, x: int, y: int) -> None:
        super().__init__()
        self.message_id = message_id
        self._x = x
        self._y = y

    def compose(self) -> ComposeResult:
        with Vertical(id="ctx-box"):
            for key, label in ITEMS:
                yield Button(label, id=f"ctx-{key}")

    def on_mount(self) -> None:
        try:
            box = self.query_one("#ctx-box")
            # clamp to screen
            w = int(box.styles.width.value) if box.styles.width else 30
            sx = max(0, min(self._x, self.size.width - w - 1))
            sy = max(0, min(self._y, self.size.height - 12))
            box.styles.offset = (sx, sy)
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("ctx-"):
            action = event.button.id[4:]
            self.app.post_message(ContextAction(action, self.message_id))
            self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)
