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
    Preview .pv-body { height: auto; padding: 1 0; }
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
                widgets.append(self._attachment_block(m.attachments))
            return widgets

        self._swap(build)

    def _attachment_block(self, atts: list[Attachment]) -> VerticalScroll:
        container = VerticalScroll(classes="pv-atts")

        def populate():
            container.mount(Label("**Attachments:**"))
            for a in atts:
                group = Vertical()
                group.att = a  # type: ignore[attr-defined]
                group.image_mounted = False  # type: ignore[attr-defined]
                container.mount(group)
                row = Horizontal(classes="attachment-row")
                group.mount(row)
                row.mount(Label(f"• {a.filename}  ({_fmt_size(a.size)})"))
                is_image = a.mime_type.startswith(IMAGE_MIME_PREFIXES)
                if is_image and _TxImage is not None:
                    btn_preview = Button("Preview", variant="default")
                    btn_preview.att = a  # type: ignore[attr-defined]
                    btn_preview.action = "inline"  # type: ignore[attr-defined]
                    btn_preview.group = group  # type: ignore[attr-defined]
                    row.mount(btn_preview)
                btn = Button("Download", variant="primary")
                btn.att = a  # type: ignore[attr-defined]
                btn.action = "download"  # type: ignore[attr-defined]
                row.mount(btn)

        container._populate = populate  # type: ignore[attr-defined]
        return container

    def render_inline_image(self, attachment: Attachment, data: bytes) -> None:
        if _TxImage is None:
            return
        # find the right group by attachment id
        for child in list(self.query(".pv-atts")):
            for group in child.children:
                att = getattr(group, "att", None)
                if att is None or att.attachment_id != attachment.attachment_id:
                    continue
                if getattr(group, "image_mounted", False):
                    return
                try:
                    img = _TxImage(io.BytesIO(data), classes="attachment-image")
                    group.mount(img)
                    group.image_mounted = True  # type: ignore[attr-defined]
                except Exception:
                    log.exception("inline image render failed")
                return

    def _swap(self, builder) -> None:
        """Safely replace all children with the output of builder(), awaiting removal."""

        async def do():
            await self.remove_children()
            for w in builder():
                await self.mount(w)
                if hasattr(w, "_populate"):
                    w._populate()

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


def default_download_dir() -> Path:
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        return Path(userprofile) / "Downloads"
    return Path.home() / "Downloads"
