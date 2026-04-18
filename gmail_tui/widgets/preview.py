from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Label, Markdown, Static

try:
    from textual_image.widget import Image as _TxImage
except Exception:  # pragma: no cover
    _TxImage = None  # type: ignore[assignment]

from ..models import Attachment, MessageFull

IMAGE_MIME_PREFIXES = ("image/",)

log = logging.getLogger(__name__)


def _fmt_datetime(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} TB"


class AttachmentDownloadRequested(Message):
    def __init__(self, attachment: Attachment) -> None:
        super().__init__()
        self.attachment = attachment


class AttachmentInlineImageRequested(Message):
    def __init__(self, attachment: Attachment) -> None:
        super().__init__()
        self.attachment = attachment


class Preview(VerticalScroll):
    DEFAULT_CSS = """
    Preview { padding: 0 1; }
    Preview .pv-headers { height: auto; padding: 1 0; border-bottom: solid $accent; }
    Preview .pv-body {
        height: auto;
        padding: 1 0;
        link-style: not underline;
        link-color: $text;
        link-background: transparent;
        link-style-hover: underline;
        link-color-hover: $accent;
    }
    Preview .pv-atts { height: auto; padding: 1 0; border-top: solid $accent; }
    Preview .attachment-row { height: auto; padding: 0; }
    Preview .attachment-row Button { margin-left: 2; }
    Preview .attachment-image { height: auto; max-height: 20; padding: 1 0; }
    Preview .placeholder { color: $text-muted; padding: 2; text-align: center; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._current: Optional[MessageFull] = None

    def compose(self) -> ComposeResult:
        yield Static("Select a message to read", classes="placeholder")

    def show_placeholder(self, text: str = "Select a message to read") -> None:
        self._current = None
        self._swap(lambda: [Static(text, classes="placeholder")])

    def show_loading(self) -> None:
        self._swap(lambda: [Static("Loading…", classes="placeholder")])

    def show_message(self, m: MessageFull) -> None:
        self._current = m

        def build() -> list:
            headers = (
                f"**From:** {m.from_addr}\n\n"
                f"**To:** {m.to_addr}\n"
            )
            if m.cc:
                headers += f"\n**Cc:** {m.cc}\n"
            headers += f"\n**Date:** {_fmt_datetime(m.date)}\n"
            headers += f"\n**Subject:** {m.subject or '(no subject)'}\n"

            widgets: list = [
                Markdown(headers, classes="pv-headers"),
                Markdown(m.body_text.strip() or "(no text body)", classes="pv-body"),
            ]
            if m.attachments:
                widgets.append(_AttachmentBlock(m.attachments))
            return widgets

        self._swap(build)

    def render_inline_image(self, attachment: Attachment, data: bytes) -> None:
        if _TxImage is None:
            return
        for row in self.query(_AttachmentRow):
            if row.attachment.attachment_id == attachment.attachment_id:
                row.mount_image(data)
                return

    def _swap(self, builder) -> None:
        """Safely replace all children with the output of builder(), awaiting removal."""

        async def do():
            try:
                await self.remove_children()
                for w in builder():
                    await self.mount(w)
            except Exception:
                log.exception("preview swap failed")

        self.app.call_later(do)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        att = getattr(event.button, "att", None)
        if att is None:
            return
        action = getattr(event.button, "action", "download")
        if action == "inline":
            self.post_message(AttachmentInlineImageRequested(att))
        else:
            self.post_message(AttachmentDownloadRequested(att))


class _AttachmentRow(Vertical):
    DEFAULT_CSS = """
    _AttachmentRow { height: auto; }
    _AttachmentRow Horizontal { height: auto; }
    _AttachmentRow Button { margin-left: 2; }
    """

    def __init__(self, attachment: Attachment) -> None:
        super().__init__()
        self.attachment = attachment
        self._image_mounted = False

    def compose(self) -> ComposeResult:
        a = self.attachment
        with Horizontal(classes="attachment-row"):
            yield Label(f"• {a.filename}  ({_fmt_size(a.size)})")
            if a.mime_type.startswith(IMAGE_MIME_PREFIXES) and _TxImage is not None:
                btn_preview = Button("Preview")
                btn_preview.att = a  # type: ignore[attr-defined]
                btn_preview.action = "inline"  # type: ignore[attr-defined]
                yield btn_preview
            btn = Button("Download", variant="primary")
            btn.att = a  # type: ignore[attr-defined]
            btn.action = "download"  # type: ignore[attr-defined]
            yield btn

    def mount_image(self, data: bytes) -> None:
        if self._image_mounted or _TxImage is None:
            return
        try:
            img = _TxImage(io.BytesIO(data), classes="attachment-image")
            self.mount(img)
            self._image_mounted = True
        except Exception:
            log.exception("inline image render failed")


class _AttachmentBlock(VerticalScroll):
    DEFAULT_CSS = """
    _AttachmentBlock { height: auto; padding: 1 0; border-top: solid $accent; }
    """

    def __init__(self, attachments: list[Attachment]) -> None:
        super().__init__(classes="pv-atts")
        self._attachments = attachments

    def compose(self) -> ComposeResult:
        yield Label("Attachments:")
        for a in self._attachments:
            yield _AttachmentRow(a)


def default_download_dir() -> Path:
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        return Path(userprofile) / "Downloads"
    return Path.home() / "Downloads"
