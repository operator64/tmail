from __future__ import annotations

import logging
import socket
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.markup import escape as _esc
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Input, Label, Static

from . import auth
from .cache import Cache
from .gmail_client import (
    GmailAPIError,
    GmailClient,
    HistoryGoneError,
    ReAuthRequired,
    build_forward_message,
    build_new_message,
    build_reply_message,
)
from .models import Label as LabelModel, MessageFull, MessageSummary
from .widgets.compose import (
    ComposeData,
    ComposeResultMessage,
    ComposeScreen,
)
from .widgets.context_menu import ContextAction, ContextMenu
from .widgets.help import HelpScreen
from .widgets.label_picker import LabelPickResult, LabelPickResultMessage, LabelPicker
from .widgets.message_list import (
    LoadMoreRequested,
    MessageContextMenuRequested,
    MessageList,
    MessageOpened,
    SelectionChanged,
)
from .widgets.preview import (
    AttachmentDownloadRequested,
    AttachmentInlineImageRequested,
    Preview,
    default_download_dir,
)
from .widgets.sidebar import LabelSelected, Sidebar

log = logging.getLogger(__name__)

HISTORY_POLL_SECONDS = 60
DRAFT_AUTOSAVE_SECONDS = 10


def _has_network() -> bool:
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2).close()
        return True
    except OSError:
        return False


class GmailTUIApp(App):
    CSS_PATH = "styles/app.tcss"
    TITLE = "Gmail TUI"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("c", "compose_new", "Compose"),
        Binding("r", "reply", "Reply"),
        Binding("R", "reply_all", "Reply All"),
        Binding("f", "forward", "Forward"),
        Binding("s", "toggle_star", "Star"),
        Binding("e", "archive", "Archive"),
        Binding("#", "trash", "Trash"),
        Binding("u", "toggle_unread", "Read/Unread"),
        Binding("l", "label_picker", "Labels"),
        Binding("m", "context_menu", "Menu"),
        Binding("/", "focus_search", "Search"),
        Binding("ctrl+r", "refresh_inbox", "Refresh"),
        Binding("[", "sidebar_shrink", "Sidebar -"),
        Binding("]", "sidebar_grow", "Sidebar +"),
        Binding("{", "preview_grow", "Preview +"),
        Binding("}", "preview_shrink", "Preview -"),
        Binding("?", "help", "Help"),
        Binding("tab", "focus_next_pane", "Focus", show=False),
        Binding("p", "toggle_preview", "Preview", show=False),
        Binding("escape", "close_preview", "Close preview", show=False),
    ]

    online: reactive[bool] = reactive(True)
    sync_text: reactive[str] = reactive("Syncing…")

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[GmailClient] = None
        self._cache = Cache()
        self._account_email: str = ""
        self._current_label: Optional[str] = "INBOX"
        self._current_query: Optional[str] = None
        self._labels: list[LabelModel] = []
        self._next_page_token: Optional[str] = None
        self._current_open: Optional[MessageFull] = None
        self._list_worker = None
        self._preview_worker = None
        self._preview_visible = False
        self._saved_preview_width = "40%"

    # ---------------- layout ----------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="header-bar"):
            yield Label("", id="header-account")
            yield Label("Syncing…", id="header-sync")
            yield Input(placeholder="/search…  (Gmail query syntax)", id="header-search")
            yield Label("?", id="header-help")
        with Horizontal(id="main"):
            with Vertical(id="sidebar-pane"):
                yield Sidebar()
            with Vertical(id="list-pane"):
                yield MessageList()
            with Vertical(id="preview-pane"):
                yield Preview()
        yield Footer()

    async def on_mount(self) -> None:
        self._apply_preview_visibility(False)
        self._start_auth_flow()

    # ---------------- auth + bootstrap ----------------

    @work(thread=True, exclusive=True)
    def _start_auth_flow(self) -> None:
        try:
            creds, email = auth.get_or_create_credentials()
            self._account_email = email
            self._client = GmailClient(creds, email)
            self.call_from_thread(self._on_authed)
        except auth.CredentialsMissingError as e:
            self.call_from_thread(self._fatal, str(e))
        except Exception as e:
            log.exception("Auth failed")
            self.call_from_thread(self._fatal, f"Authentication failed: {e}")

    def _on_authed(self) -> None:
        self.query_one("#header-account", Label).update(_esc(self._account_email))
        self._refresh_labels_worker()
        self._load_label_messages("INBOX")
        self.set_interval(HISTORY_POLL_SECONDS, self._poll_history, name="history-poll")

    def _fatal(self, msg: str) -> None:
        short = msg.splitlines()[0][:200]
        self.query_one("#header-sync", Label).update(_esc(short))
        self.bell()
        log.error(msg)

    # ---------------- sidebar refresh ----------------

    @work(thread=True, group="labels")
    def _refresh_labels_worker(self) -> None:
        if not self._client:
            return
        try:
            labels = self._client.labels_with_counts()
            self._cache.upsert_labels(labels)
            self.call_from_thread(self._on_labels, labels)
        except ReAuthRequired:
            self.call_from_thread(self._require_reauth)
        except Exception:
            log.exception("Label refresh failed")
            cached = self._cache.list_labels()
            if cached:
                self.call_from_thread(self._on_labels, cached)

    def _on_labels(self, labels: list[LabelModel]) -> None:
        self._labels = labels
        self.query_one(Sidebar).set_labels(labels)

    # ---------------- message list ----------------

    def _load_label_messages(self, label_id: str, query: Optional[str] = None) -> None:
        self._current_label = label_id
        self._current_query = query
        self._next_page_token = None
        # instant cache view
        cached = self._cache.get_summaries_by_label(label_id) if label_id else []
        self.query_one(MessageList).set_summaries(cached)
        if label_id == "INBOX":
            self.sync_text = "Inbox — loading…"
        self._list_worker = self.run_worker(
            self._fetch_messages_thread(label_id, query, None, replace=True),
            thread=True,
            exclusive=True,
            group="list",
        )

    def _fetch_messages_thread(
        self,
        label_id: Optional[str],
        query: Optional[str],
        page_token: Optional[str],
        replace: bool,
    ):
        def run():
            if not self._client:
                return
            try:
                label_ids = [label_id] if label_id else None
                ids, next_token = self._client.list_messages(
                    label_ids=label_ids,
                    query=query,
                    page_token=page_token,
                    max_results=50,
                )
                summaries = self._client.batch_get_metadata(ids) if ids else []
                self._cache.upsert_message_summaries(summaries)
                self.call_from_thread(
                    self._on_messages_loaded,
                    summaries,
                    next_token,
                    replace,
                )
            except ReAuthRequired:
                self.call_from_thread(self._require_reauth)
            except Exception:
                log.exception("message fetch failed")
                self.call_from_thread(self._on_network_error)
        return run

    def _on_messages_loaded(
        self,
        summaries: list[MessageSummary],
        next_token: Optional[str],
        replace: bool,
    ) -> None:
        lst = self.query_one(MessageList)
        if replace:
            lst.set_summaries(summaries)
        else:
            lst.append_summaries(summaries)
        self._next_page_token = next_token
        self.online = True
        self.sync_text = f"Synced {datetime.now().strftime('%H:%M')}"
        self._update_sync_label()

    def _on_network_error(self) -> None:
        self.online = False
        self.sync_text = "Offline"
        self._update_sync_label()

    def _require_reauth(self) -> None:
        self.sync_text = "Session expired — please re-run"
        self._update_sync_label()
        self.bell()

    def _update_sync_label(self) -> None:
        lbl = self.query_one("#header-sync", Label)
        text = self.sync_text
        if not self.online:
            text = "[OFFLINE] " + text
        lbl.update(_esc(text))

    def watch_online(self, online: bool) -> None:
        self._update_sync_label()
        if online:
            self._flush_pending()

    # ---------------- Sidebar → list ----------------

    def on_label_selected(self, event: LabelSelected) -> None:
        if event.query:
            self._load_label_messages(label_id="", query=event.query)
        elif event.label_id:
            self._load_label_messages(event.label_id)

    # ---------------- list → preview ----------------

    def on_message_opened(self, event: MessageOpened) -> None:
        self._open_message(event.message_id)

    def _apply_preview_visibility(self, visible: bool) -> None:
        self._preview_visible = visible
        try:
            preview_pane = self.query_one("#preview-pane")
            list_pane = self.query_one("#list-pane")
        except Exception:
            return
        preview_pane.display = visible
        if visible:
            list_pane.styles.width = "38%"
        else:
            list_pane.styles.width = "1fr"

    def action_toggle_preview(self) -> None:
        if self._preview_visible:
            self._apply_preview_visibility(False)
            self.query_one(MessageList).focus()
        else:
            self._apply_preview_visibility(True)

    def action_close_preview(self) -> None:
        if self._preview_visible:
            self._apply_preview_visibility(False)
            try:
                self.query_one(MessageList).focus()
            except Exception:
                pass

    def _open_message(self, message_id: str) -> None:
        if not self._preview_visible:
            self._apply_preview_visibility(True)
        preview = self.query_one(Preview)
        cached = self._cache.get_message_body(message_id)
        summary = self._cache.get_summary(message_id)
        if cached and summary:
            full = MessageFull(
                id=message_id,
                thread_id=summary.thread_id,
                headers=cached["headers"],
                body_text=cached["body_text"],
                body_html=cached["body_html"],
                attachments=[],
                labels=summary.labels,
                date=summary.date,
            )
            self._current_open = full
            preview.show_message(full)
        else:
            preview.show_loading()

        self._preview_worker = self.run_worker(
            self._fetch_full_thread(message_id),
            thread=True,
            exclusive=True,
            group="preview",
        )

        # auto-mark-as-read if unread
        if summary and summary.is_unread:
            self._modify_local_and_remote(message_id, add=None, remove=["UNREAD"])

    def _fetch_full_thread(self, message_id: str):
        def run():
            if not self._client:
                return
            try:
                full = self._client.get_message_full(message_id)
                self._cache.upsert_message_full(full)
                self.call_from_thread(self._on_preview_loaded, full)
            except ReAuthRequired:
                self.call_from_thread(self._require_reauth)
            except Exception:
                log.exception("preview fetch failed")
        return run

    def _on_preview_loaded(self, m: MessageFull) -> None:
        self._current_open = m
        self.query_one(Preview).show_message(m)

    # ---------------- List events ----------------

    def on_load_more_requested(self, event: LoadMoreRequested) -> None:
        if not self._next_page_token:
            return
        label_id = self._current_label
        self._list_worker = self.run_worker(
            self._fetch_messages_thread(
                label_id, self._current_query, self._next_page_token, replace=False
            ),
            thread=True,
            group="list",
        )

    def on_selection_changed(self, event: SelectionChanged) -> None:
        pass

    # ---------------- Modify actions ----------------

    def _selected_ids(self) -> list[str]:
        return self.query_one(MessageList).selected_ids()

    def _modify_local_and_remote(
        self,
        message_id_or_ids,
        add: Optional[list[str]],
        remove: Optional[list[str]],
    ) -> None:
        ids = message_id_or_ids if isinstance(message_id_or_ids, list) else [message_id_or_ids]
        if not ids:
            return
        # optimistic local
        for mid in ids:
            self._cache.update_message_labels(mid, add=add, remove=remove)
            s = self._cache.get_summary(mid)
            if s:
                self.query_one(MessageList).update_summary(s)
        if self._current_open and self._current_open.id in ids:
            if add:
                self._current_open.labels = sorted(set(self._current_open.labels) | set(add))
            if remove:
                self._current_open.labels = [l for l in self._current_open.labels if l not in remove]

        self.run_worker(
            self._batch_modify_remote(ids, add, remove),
            thread=True,
            group="modify",
        )

    def _batch_modify_remote(self, ids: list[str], add, remove):
        def run():
            if not self._client:
                self._cache.add_pending("modify", {"ids": ids, "add": add, "remove": remove})
                return
            try:
                if len(ids) == 1:
                    self._client.modify_message(ids[0], add=add, remove=remove)
                else:
                    self._client.batch_modify(ids, add=add, remove=remove)
            except ReAuthRequired:
                self.call_from_thread(self._require_reauth)
            except Exception:
                log.exception("modify failed — queueing")
                self._cache.add_pending("modify", {"ids": ids, "add": add, "remove": remove})
                self.call_from_thread(self._on_network_error)
        return run

    def action_toggle_star(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        # Use cache state to decide toggle direction
        first = self._cache.get_summary(ids[0])
        if not first:
            return
        if first.is_starred:
            self._modify_local_and_remote(ids, add=None, remove=["STARRED"])
        else:
            self._modify_local_and_remote(ids, add=["STARRED"], remove=None)

    def action_archive(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        self._modify_local_and_remote(ids, add=None, remove=["INBOX"])
        if self._current_label == "INBOX":
            for mid in ids:
                self.query_one(MessageList).remove_id(mid)

    def action_toggle_unread(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        first = self._cache.get_summary(ids[0])
        if not first:
            return
        if first.is_unread:
            self._modify_local_and_remote(ids, add=None, remove=["UNREAD"])
        else:
            self._modify_local_and_remote(ids, add=["UNREAD"], remove=None)

    def action_trash(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        for mid in ids:
            self._cache.update_message_labels(mid, add=["TRASH"], remove=["INBOX"])
            self.query_one(MessageList).remove_id(mid)
        self.run_worker(self._trash_remote(ids), thread=True, group="modify")

    def _trash_remote(self, ids: list[str]):
        def run():
            if not self._client:
                for mid in ids:
                    self._cache.add_pending("trash", {"id": mid})
                return
            try:
                for mid in ids:
                    self._client.trash_message(mid)
            except ReAuthRequired:
                self.call_from_thread(self._require_reauth)
            except Exception:
                log.exception("trash failed — queueing")
                for mid in ids:
                    self._cache.add_pending("trash", {"id": mid})
                self.call_from_thread(self._on_network_error)
        return run

    # ---------------- Reply / Forward / New ----------------

    def action_compose_new(self) -> None:
        self.push_screen(ComposeScreen(ComposeData(), title="New mail"))

    def action_reply(self) -> None:
        self._open_reply(all_flag=False)

    def action_reply_all(self) -> None:
        self._open_reply(all_flag=True)

    def _open_reply(self, all_flag: bool) -> None:
        if not self._current_open:
            self.bell()
            return
        data = ComposeData(
            to=self._current_open.from_addr,
            subject=self._current_open.subject
                    if self._current_open.subject.lower().startswith("re:")
                    else f"Re: {self._current_open.subject}",
            reply_to_message_id=self._current_open.id,
            reply_all=all_flag,
            thread_id=self._current_open.thread_id,
        )
        if all_flag:
            extras = [
                x.strip() for x in (
                    self._current_open.to_addr.split(",") + self._current_open.cc.split(",")
                )
                if x.strip() and self._account_email.lower() not in x.lower()
            ]
            if extras:
                data.cc = ", ".join(extras)
        self.push_screen(ComposeScreen(data, title="Reply"))

    def action_forward(self) -> None:
        if not self._current_open:
            self.bell()
            return
        data = ComposeData(
            to="",
            subject=self._current_open.subject
                    if self._current_open.subject.lower().startswith("fwd:")
                    else f"Fwd: {self._current_open.subject}",
            forward_message_id=self._current_open.id,
        )
        self.push_screen(ComposeScreen(data, title="Forward"))

    def on_compose_result_message(self, event: ComposeResultMessage) -> None:
        if event.data is None:
            return
        data = event.data
        if not data.to.strip():
            self.sync_text = "Send aborted — no recipient"
            self._update_sync_label()
            return
        self.run_worker(self._send_compose(data), thread=True, group="send")

    def _send_compose(self, data: ComposeData):
        def run():
            if not self._client:
                self._cache.add_pending("send", data.__dict__)
                return
            try:
                attachments = [Path(p) for p in data.attachments if Path(p).exists()]
                if data.reply_to_message_id:
                    orig = self._client.get_message_full(data.reply_to_message_id)
                    msg = build_reply_message(
                        orig,
                        sender=self._account_email,
                        body=data.body,
                        reply_all=data.reply_all,
                    )
                    # Override To if user edited it
                    if data.to:
                        del msg["To"]
                        msg["To"] = data.to
                    if data.cc:
                        del msg["Cc"]
                        msg["Cc"] = data.cc
                    self._client.send_raw(msg, thread_id=orig.thread_id)
                elif data.forward_message_id:
                    orig = self._client.get_message_full(data.forward_message_id)
                    msg = build_forward_message(
                        orig,
                        sender=self._account_email,
                        to=data.to,
                        body=data.body,
                    )
                    if data.cc:
                        msg["Cc"] = data.cc
                    self._client.send_raw(msg)
                else:
                    msg = build_new_message(
                        sender=self._account_email,
                        to=data.to,
                        subject=data.subject,
                        body=data.body,
                        cc=data.cc,
                        bcc=data.bcc,
                        attachments=attachments,
                    )
                    self._client.send_raw(msg)
                self.call_from_thread(self._on_sent)
            except ReAuthRequired:
                self.call_from_thread(self._require_reauth)
            except Exception:
                log.exception("send failed — queueing")
                self._cache.add_pending("send", data.__dict__)
                self.call_from_thread(self._on_network_error)
        return run

    def _on_sent(self) -> None:
        self.sync_text = f"Sent at {datetime.now().strftime('%H:%M')}"
        self._update_sync_label()

    # ---------------- Label picker ----------------

    def action_label_picker(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self.bell()
            return
        # seed with labels of first message
        first = self._cache.get_summary(ids[0])
        current = first.labels if first else []
        self.push_screen(LabelPicker(self._labels, current))

    def on_label_pick_result_message(self, event: LabelPickResultMessage) -> None:
        r = event.result
        if r is None:
            return
        ids = self._selected_ids()
        if not ids:
            return

        if r.create_name:
            self.run_worker(self._create_label_and_apply(r.create_name, ids), thread=True, group="labels")
            return

        if r.add or r.remove:
            self._modify_local_and_remote(ids, add=r.add or None, remove=r.remove or None)

    def _create_label_and_apply(self, name: str, ids: list[str]):
        def run():
            if not self._client:
                return
            try:
                new_lbl = self._client.create_label(name)
                self._cache.upsert_labels([new_lbl])
                self.call_from_thread(self._refresh_labels_worker_sync)
                if len(ids) == 1:
                    self._client.modify_message(ids[0], add=[new_lbl.id], remove=None)
                else:
                    self._client.batch_modify(ids, add=[new_lbl.id], remove=None)
                for mid in ids:
                    self._cache.update_message_labels(mid, add=[new_lbl.id], remove=None)
            except ReAuthRequired:
                self.call_from_thread(self._require_reauth)
            except Exception:
                log.exception("create label failed")
        return run

    def _refresh_labels_worker_sync(self) -> None:
        self._refresh_labels_worker()

    # ---------------- Context menu ----------------

    def action_context_menu(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self.bell()
            return
        mid = ids[0]
        cx = self.size.width // 3
        cy = self.size.height // 3
        self.push_screen(ContextMenu(mid, cx, cy))

    def on_message_context_menu_requested(
        self, event: MessageContextMenuRequested
    ) -> None:
        self.push_screen(ContextMenu(event.message_id, event.x, event.y))

    def on_context_action(self, event: ContextAction) -> None:
        mid = event.message_id
        if event.action == "reply":
            self._open_message(mid)
            self.call_after_refresh(lambda: self._open_reply(False))
        elif event.action == "reply_all":
            self._open_message(mid)
            self.call_after_refresh(lambda: self._open_reply(True))
        elif event.action == "forward":
            self._open_message(mid)
            self.call_after_refresh(self.action_forward)
        elif event.action == "toggle_star":
            self.action_toggle_star()
        elif event.action == "label":
            self.action_label_picker()
        elif event.action == "archive":
            self.action_archive()
        elif event.action == "toggle_unread":
            self.action_toggle_unread()
        elif event.action == "trash":
            self.action_trash()

    # ---------------- History polling ----------------

    def _poll_history(self) -> None:
        if not self._client:
            return
        self.run_worker(self._history_sync(), thread=True, group="history")

    def _history_sync(self):
        def run():
            if not self._client:
                return
            try:
                last = self._cache.get_state("last_history_id")
                if not last:
                    hid = self._client.get_profile_history_id()
                    self._cache.set_state("last_history_id", hid)
                    return
                page_token = None
                new_hid = last
                while True:
                    try:
                        changes, page_token, hid = self._client.list_history(
                            start_history_id=last, page_token=page_token
                        )
                    except HistoryGoneError:
                        self._cache.wipe()
                        self.call_from_thread(self._full_resync_after_wipe)
                        return
                    if hid:
                        new_hid = hid
                    added_ids = set()
                    removed_ids = set()
                    for h in changes:
                        for m in h.get("messagesAdded", []):
                            mid = m.get("message", {}).get("id")
                            if mid:
                                added_ids.add(mid)
                        for m in h.get("messagesDeleted", []):
                            mid = m.get("message", {}).get("id")
                            if mid:
                                removed_ids.add(mid)
                        for label_change in h.get("labelsAdded", []):
                            mid = label_change.get("message", {}).get("id")
                            labels = label_change.get("labelIds", [])
                            if mid:
                                self._cache.update_message_labels(mid, add=labels, remove=None)
                        for label_change in h.get("labelsRemoved", []):
                            mid = label_change.get("message", {}).get("id")
                            labels = label_change.get("labelIds", [])
                            if mid:
                                self._cache.update_message_labels(mid, add=None, remove=labels)
                    if added_ids:
                        summaries = self._client.batch_get_metadata(list(added_ids))
                        self._cache.upsert_message_summaries(summaries)
                    for mid in removed_ids:
                        self._cache.delete_message(mid)
                    if not page_token:
                        break
                self._cache.set_state("last_history_id", new_hid)
                self.call_from_thread(self._refresh_current_view)
            except ReAuthRequired:
                self.call_from_thread(self._require_reauth)
            except Exception:
                log.exception("history poll failed")
        return run

    def _full_resync_after_wipe(self) -> None:
        self.sync_text = "Re-syncing…"
        self._update_sync_label()
        self._refresh_labels_worker()
        if self._current_label:
            self._load_label_messages(self._current_label, self._current_query)

    def _refresh_current_view(self) -> None:
        if not self._current_label:
            return
        cached = self._cache.get_summaries_by_label(self._current_label)
        self.query_one(MessageList).set_summaries(cached)
        self.sync_text = f"Synced {datetime.now().strftime('%H:%M')}"
        self._update_sync_label()

    # ---------------- Offline queue flush ----------------

    def _flush_pending(self) -> None:
        if not self._client:
            return
        self.run_worker(self._flush_thread(), thread=True, group="pending")

    def _flush_thread(self):
        def run():
            if not self._client:
                return
            for pa in self._cache.list_pending():
                try:
                    if pa.action_type == "modify":
                        ids = pa.payload["ids"]
                        add = pa.payload.get("add")
                        remove = pa.payload.get("remove")
                        if len(ids) == 1:
                            self._client.modify_message(ids[0], add=add, remove=remove)
                        else:
                            self._client.batch_modify(ids, add=add, remove=remove)
                    elif pa.action_type == "trash":
                        self._client.trash_message(pa.payload["id"])
                    elif pa.action_type == "send":
                        d = ComposeData(**pa.payload)
                        self._send_compose(d)()  # blocking from within worker
                    self._cache.remove_pending(pa.id)
                except Exception:
                    log.exception("pending action failed, attempt %d", pa.attempts)
                    self._cache.bump_pending_attempts(pa.id)
                    if pa.attempts + 1 >= 3:
                        self._cache.remove_pending(pa.id)
        return run

    # ---------------- Attachment download ----------------

    def on_attachment_download_requested(
        self, event: AttachmentDownloadRequested
    ) -> None:
        self.run_worker(self._download_attachment(event.attachment), thread=True, group="attachments")

    def on_attachment_inline_image_requested(
        self, event: AttachmentInlineImageRequested
    ) -> None:
        self.run_worker(
            self._fetch_and_inline_image(event.attachment),
            thread=True,
            group="attachments",
        )

    def _fetch_and_inline_image(self, att):
        def run():
            if not self._client:
                return
            try:
                data = self._client.get_attachment(att.message_id, att.attachment_id)
                self.call_from_thread(self.query_one(Preview).render_inline_image, att, data)
            except Exception:
                log.exception("inline image fetch failed")
        return run

    def _download_attachment(self, att):
        def run():
            if not self._client:
                return
            try:
                data = self._client.get_attachment(att.message_id, att.attachment_id)
                target_dir = default_download_dir()
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / att.filename
                if target.exists():
                    stem = target.stem
                    suffix = target.suffix
                    i = 1
                    while target.exists():
                        target = target_dir / f"{stem} ({i}){suffix}"
                        i += 1
                target.write_bytes(data)
                self.call_from_thread(self._on_download_done, target)
            except Exception:
                log.exception("attachment download failed")
        return run

    def _on_download_done(self, path: Path) -> None:
        self.sync_text = f"Saved {path.name}"
        self._update_sync_label()

    # ---------------- Search ----------------

    def action_focus_search(self) -> None:
        self.query_one("#header-search", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "header-search":
            return
        q = event.value.strip()
        if not q:
            self._load_label_messages("INBOX")
            return
        self._load_label_messages("", query=q)

    # ---------------- Pane focus & resize ----------------

    def action_focus_next_pane(self) -> None:
        order = [Sidebar, MessageList, Preview, Input]
        focused = self.focused
        idx = 0
        for i, cls in enumerate(order):
            if isinstance(focused, cls):
                idx = (i + 1) % len(order)
                break
        target = self.query(order[idx]).first()
        if target:
            target.focus()

    def _adjust_pane_width(self, pane_id: str, delta: int) -> None:
        try:
            pane = self.query_one(pane_id)
            style_w = pane.styles.width
            current = int(style_w.value) if style_w and style_w.value else 22
            new = max(10, min(60, current + delta))
            pane.styles.width = f"{new}%"
        except Exception:
            pass

    def action_sidebar_shrink(self) -> None:
        self._adjust_pane_width("#sidebar-pane", -2)

    def action_sidebar_grow(self) -> None:
        self._adjust_pane_width("#sidebar-pane", +2)

    def action_preview_shrink(self) -> None:
        # growing list pane = preview's left border moves right → preview shrinks
        self._adjust_pane_width("#list-pane", +2)

    def action_preview_grow(self) -> None:
        # shrinking list pane = preview's left border moves left → preview grows
        self._adjust_pane_width("#list-pane", -2)

    # ---------------- Refresh ----------------

    def action_refresh_inbox(self) -> None:
        if self._current_label:
            self._load_label_messages(self._current_label, self._current_query)
        self._refresh_labels_worker()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
