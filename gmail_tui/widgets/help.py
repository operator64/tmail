from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Markdown


HELP_TEXT = """\
# Gmail TUI — Keybindings

## Navigation
- `j` / `k`         — next / prev message
- `gg` / `G`        — top / bottom of list
- `Enter`           — open message
- `Tab`             — change pane focus
- `[` / `]`         — shrink / grow sidebar
- `{` / `}`         — grow / shrink preview

## Actions
- `c`               — new mail
- `r` / `R`         — reply / reply all
- `f`               — forward
- `s`               — toggle star
- `e`               — archive
- `#`               — move to trash
- `u`               — toggle read/unread
- `l`               — label picker
- `m`               — context menu
- `/`               — search
- `Ctrl+R`          — refresh inbox
- `Space`           — multi-select toggle
- `?`               — this help
- `q`               — quit
"""


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help-box { width: 72; height: 80%; background: $surface; border: round $accent; padding: 1 2; }
    """

    BINDINGS = [
        Binding("escape,q,?", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Markdown(HELP_TEXT)

    def action_close(self) -> None:
        self.dismiss(None)
