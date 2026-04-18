from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, TextArea


@dataclass
class ComposeData:
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body: str = ""
    attachments: list[str] = field(default_factory=list)
    reply_to_message_id: Optional[str] = None
    reply_all: bool = False
    forward_message_id: Optional[str] = None
    thread_id: Optional[str] = None
    local_draft_id: Optional[int] = None


class ComposeResultMessage(Message):
    def __init__(self, data: Optional[ComposeData]) -> None:
        super().__init__()
        self.data = data  # None == cancelled


class ComposeScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    ComposeScreen { align: center middle; }
    #compose-box {
        width: 80%; height: 80%;
        background: $surface; border: round $accent; padding: 1 2;
    }
    #compose-box .row { height: 3; }
    #compose-box Input { width: 1fr; }
    #compose-box TextArea { height: 1fr; }
    #compose-box .toggle-row { height: 1; }
    #compose-box #compose-buttons { height: 3; align: right middle; }
    #compose-box #compose-buttons Button { margin-left: 1; }
    .hidden { display: none; }
    """

    BINDINGS = [
        Binding("ctrl+enter", "send", "Send", show=True),
        Binding("ctrl+shift+c", "toggle_cc", "Toggle Cc", show=False),
        Binding("ctrl+shift+b", "toggle_bcc", "Toggle Bcc", show=False),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, data: Optional[ComposeData] = None, title: str = "Compose") -> None:
        super().__init__()
        self.data = data or ComposeData()
        self.title_text = title
        self._cc_visible = bool(self.data.cc)
        self._bcc_visible = bool(self.data.bcc)

    def compose(self) -> ComposeResult:
        with Vertical(id="compose-box"):
            yield Label(self.title_text)
            with Horizontal(classes="row"):
                yield Label("To:    ", classes="field-label")
                yield Input(value=self.data.to, id="to-input", placeholder="recipient@example.com")
            with Horizontal(classes="row", id="cc-row"):
                yield Label("Cc:    ", classes="field-label")
                yield Input(value=self.data.cc, id="cc-input")
            with Horizontal(classes="row", id="bcc-row"):
                yield Label("Bcc:   ", classes="field-label")
                yield Input(value=self.data.bcc, id="bcc-input")
            with Horizontal(classes="row"):
                yield Label("Subj:  ", classes="field-label")
                yield Input(value=self.data.subject, id="subject-input")
            with Horizontal(classes="row"):
                yield Label("Files: ", classes="field-label")
                yield Input(
                    value=",".join(self.data.attachments),
                    id="att-input",
                    placeholder="C:\\path\\file1.pdf, C:\\path\\file2.png",
                )
            yield TextArea(self.data.body, id="body-area", language=None)
            with Horizontal(id="compose-buttons"):
                yield Button("Cancel (Esc)", id="cancel-btn")
                yield Button("Send (Ctrl+Enter)", id="send-btn", variant="primary")

    def on_mount(self) -> None:
        self._apply_visibility()
        if not self.data.to:
            self.query_one("#to-input", Input).focus()
        else:
            self.query_one("#body-area", TextArea).focus()

    def _apply_visibility(self) -> None:
        cc_row = self.query_one("#cc-row")
        bcc_row = self.query_one("#bcc-row")
        cc_row.display = self._cc_visible
        bcc_row.display = self._bcc_visible

    def action_toggle_cc(self) -> None:
        self._cc_visible = not self._cc_visible
        self._apply_visibility()

    def action_toggle_bcc(self) -> None:
        self._bcc_visible = not self._bcc_visible
        self._apply_visibility()

    def action_send(self) -> None:
        self._harvest()
        self.app.post_message(ComposeResultMessage(self.data))
        self.dismiss(None)

    def action_cancel(self) -> None:
        self._harvest()
        self.app.post_message(ComposeResultMessage(None))
        self.dismiss(None)

    def _harvest(self) -> None:
        self.data.to = self.query_one("#to-input", Input).value.strip()
        self.data.cc = self.query_one("#cc-input", Input).value.strip() if self._cc_visible else ""
        self.data.bcc = self.query_one("#bcc-input", Input).value.strip() if self._bcc_visible else ""
        self.data.subject = self.query_one("#subject-input", Input).value
        atts_raw = self.query_one("#att-input", Input).value
        self.data.attachments = [s.strip() for s in atts_raw.split(",") if s.strip()]
        self.data.body = self.query_one("#body-area", TextArea).text

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.action_cancel()
        elif event.button.id == "send-btn":
            self.action_send()

    def current_snapshot(self) -> ComposeData:
        self._harvest()
        return self.data
