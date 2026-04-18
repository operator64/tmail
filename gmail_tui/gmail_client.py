from __future__ import annotations

import base64
import logging
import random
import threading
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import html2text
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .models import (
    Attachment,
    Label,
    MessageFull,
    MessageSummary,
    is_system_label,
)

log = logging.getLogger(__name__)

MAX_RETRIES = 6
BACKOFF_BASE = 1.0
BACKOFF_CAP = 32.0


class GmailAPIError(Exception):
    pass


class ReAuthRequired(GmailAPIError):
    pass


class HistoryGoneError(GmailAPIError):
    pass


def _should_retry(status: int) -> bool:
    return status in (429, 500, 502, 503, 504)


_API_LOCK = threading.Lock()


def _retryable(call: Callable[[], Any]) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            with _API_LOCK:
                return call()
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            if status == 401:
                raise ReAuthRequired("Access token invalid") from e
            if status == 403:
                reason = ""
                try:
                    reason = str(e.error_details or e._get_reason() or "")
                except Exception:
                    pass
                if "insufficientPermissions" in reason or "insufficient" in reason.lower():
                    raise ReAuthRequired("Insufficient permissions") from e
                raise
            if status == 404:
                raise
            if not _should_retry(status):
                raise
            last_exc = e
        except Exception as e:
            last_exc = e
        delay = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
        delay += random.uniform(0, 0.25 * delay)
        log.warning("API call retry %d after %.1fs: %s", attempt + 1, delay, last_exc)
        time.sleep(delay)
    raise GmailAPIError(f"Exceeded retries: {last_exc}") from last_exc


class GmailClient:
    def __init__(self, creds: Credentials, account_email: str):
        self._creds = creds
        self.account_email = account_email
        self._service = self._build_service()

    def _build_service(self):
        return build("gmail", "v1", credentials=self._creds, cache_discovery=False)

    def ensure_fresh_token(self) -> None:
        with _API_LOCK:
            if self._creds.expired or not self._creds.token:
                self._creds.refresh(Request())
                self._service = self._build_service()

    # ---------------- Labels ----------------

    def list_labels(self) -> list[Label]:
        self.ensure_fresh_token()
        result = _retryable(lambda: self._service.users().labels().list(userId="me").execute())
        out: list[Label] = []
        for raw in result.get("labels", []):
            lbl = Label(
                id=raw["id"],
                name=raw["name"],
                type=raw.get("type", "user"),
            )
            # messagesUnread requires label.get
            out.append(lbl)
        return out

    def get_label(self, label_id: str) -> Label:
        self.ensure_fresh_token()
        raw = _retryable(
            lambda: self._service.users().labels().get(userId="me", id=label_id).execute()
        )
        return Label(
            id=raw["id"],
            name=raw["name"],
            type=raw.get("type", "user"),
            messages_unread=int(raw.get("messagesUnread", 0)),
        )

    def labels_with_counts(self) -> list[Label]:
        labels = self.list_labels()
        # enrich with counts — one get per label is pragmatic for <100 labels
        enriched: list[Label] = []
        for lbl in labels:
            try:
                enriched.append(self.get_label(lbl.id))
            except Exception:
                log.exception("Failed to enrich label %s", lbl.id)
                enriched.append(lbl)
        return enriched

    def create_label(self, name: str) -> Label:
        self.ensure_fresh_token()
        body = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        raw = _retryable(
            lambda: self._service.users().labels().create(userId="me", body=body).execute()
        )
        return Label(id=raw["id"], name=raw["name"], type=raw.get("type", "user"))

    # ---------------- Messages: list ----------------

    def list_messages(
        self,
        label_ids: Optional[list[str]] = None,
        query: Optional[str] = None,
        page_token: Optional[str] = None,
        max_results: int = 50,
    ) -> tuple[list[str], Optional[str]]:
        self.ensure_fresh_token()
        kwargs: dict[str, Any] = {"userId": "me", "maxResults": max_results}
        if label_ids:
            kwargs["labelIds"] = label_ids
        if query:
            kwargs["q"] = query
        if page_token:
            kwargs["pageToken"] = page_token
        result = _retryable(lambda: self._service.users().messages().list(**kwargs).execute())
        ids = [m["id"] for m in result.get("messages", [])]
        next_token = result.get("nextPageToken")
        return ids, next_token

    def batch_get_metadata(self, message_ids: list[str]) -> list[MessageSummary]:
        if not message_ids:
            return []
        self.ensure_fresh_token()

        collected: list[MessageSummary] = []
        errors: list[Exception] = []

        def callback(request_id, response, exception):
            if exception is not None:
                errors.append(exception)
                return
            collected.append(_parse_metadata(response))

        batch = self._service.new_batch_http_request(callback=callback)
        for mid in message_ids:
            batch.add(
                self._service.users().messages().get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
            )
        _retryable(lambda: batch.execute())
        # Preserve original ordering
        by_id = {m.id: m for m in collected}
        return [by_id[i] for i in message_ids if i in by_id]

    # ---------------- Messages: full ----------------

    def get_message_full(self, message_id: str) -> MessageFull:
        self.ensure_fresh_token()
        raw = _retryable(
            lambda: self._service.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
        )
        return _parse_full(raw)

    def get_thread(self, thread_id: str) -> list[MessageFull]:
        self.ensure_fresh_token()
        raw = _retryable(
            lambda: self._service.users().threads().get(
                userId="me", id=thread_id, format="full"
            ).execute()
        )
        return [_parse_full(m) for m in raw.get("messages", [])]

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        self.ensure_fresh_token()
        raw = _retryable(
            lambda: self._service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            ).execute()
        )
        data = raw.get("data", "")
        return base64.urlsafe_b64decode(data.encode("ascii") + b"==")

    # ---------------- Modify ----------------

    def modify_message(
        self,
        message_id: str,
        add: Optional[list[str]] = None,
        remove: Optional[list[str]] = None,
    ) -> None:
        self.ensure_fresh_token()
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        _retryable(
            lambda: self._service.users().messages().modify(
                userId="me", id=message_id, body=body
            ).execute()
        )

    def batch_modify(
        self,
        message_ids: list[str],
        add: Optional[list[str]] = None,
        remove: Optional[list[str]] = None,
    ) -> None:
        if not message_ids:
            return
        self.ensure_fresh_token()
        body: dict[str, Any] = {"ids": message_ids}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        _retryable(
            lambda: self._service.users().messages().batchModify(
                userId="me", body=body
            ).execute()
        )

    def trash_message(self, message_id: str) -> None:
        self.ensure_fresh_token()
        _retryable(
            lambda: self._service.users().messages().trash(
                userId="me", id=message_id
            ).execute()
        )

    def untrash_message(self, message_id: str) -> None:
        self.ensure_fresh_token()
        _retryable(
            lambda: self._service.users().messages().untrash(
                userId="me", id=message_id
            ).execute()
        )

    def delete_message(self, message_id: str) -> None:
        """Hard-delete. Only call after explicit user confirmation."""
        self.ensure_fresh_token()
        _retryable(
            lambda: self._service.users().messages().delete(
                userId="me", id=message_id
            ).execute()
        )

    # ---------------- Send / Draft ----------------

    def send_raw(
        self,
        mime_message: EmailMessage,
        thread_id: Optional[str] = None,
    ) -> dict:
        self.ensure_fresh_token()
        raw = base64.urlsafe_b64encode(mime_message.as_bytes()).decode("ascii").rstrip("=")
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return _retryable(
            lambda: self._service.users().messages().send(userId="me", body=body).execute()
        )

    def create_draft(
        self,
        mime_message: EmailMessage,
        thread_id: Optional[str] = None,
    ) -> dict:
        self.ensure_fresh_token()
        raw = base64.urlsafe_b64encode(mime_message.as_bytes()).decode("ascii").rstrip("=")
        msg_body: dict[str, Any] = {"raw": raw}
        if thread_id:
            msg_body["threadId"] = thread_id
        body = {"message": msg_body}
        return _retryable(
            lambda: self._service.users().drafts().create(userId="me", body=body).execute()
        )

    # ---------------- History ----------------

    def get_profile_history_id(self) -> str:
        self.ensure_fresh_token()
        profile = _retryable(lambda: self._service.users().getProfile(userId="me").execute())
        return str(profile["historyId"])

    def list_history(
        self,
        start_history_id: str,
        page_token: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str], Optional[str]]:
        self.ensure_fresh_token()
        kwargs: dict[str, Any] = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
        }
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            result = _retryable(lambda: self._service.users().history().list(**kwargs).execute())
        except HttpError as e:
            if getattr(e.resp, "status", 0) == 404:
                raise HistoryGoneError("startHistoryId expired") from e
            raise
        return (
            result.get("history", []),
            result.get("nextPageToken"),
            result.get("historyId"),
        )


# ---------------- Parsers ----------------

def _header_map(payload: dict) -> dict[str, str]:
    headers = payload.get("headers", []) if payload else []
    return {h["name"]: h["value"] for h in headers}


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_metadata(raw: dict) -> MessageSummary:
    payload = raw.get("payload", {}) or {}
    headers = _header_map(payload)
    return MessageSummary(
        id=raw["id"],
        thread_id=raw["threadId"],
        from_addr=headers.get("From", ""),
        subject=headers.get("Subject", ""),
        snippet=raw.get("snippet", ""),
        date=_parse_date(headers.get("Date")),
        labels=list(raw.get("labelIds", [])),
    )


def _walk_parts(payload: dict) -> Iterable[dict]:
    yield payload
    for p in payload.get("parts", []) or []:
        yield from _walk_parts(p)


def _decode_part_body(part: dict) -> str:
    body = part.get("body", {}) or {}
    data = body.get("data")
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data.encode("ascii") + b"==")
    except Exception:
        return ""
    charset = "utf-8"
    for h in part.get("headers", []) or []:
        if h.get("name", "").lower() == "content-type":
            v = h.get("value", "")
            if "charset=" in v.lower():
                charset = v.lower().split("charset=", 1)[1].split(";", 1)[0].strip().strip('"')
            break
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _parse_full(raw: dict) -> MessageFull:
    payload = raw.get("payload", {}) or {}
    headers = _header_map(payload)

    text_body = ""
    html_body = ""
    attachments: list[Attachment] = []

    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        if filename and body.get("attachmentId"):
            attachments.append(
                Attachment(
                    filename=filename,
                    mime_type=mime,
                    size=int(body.get("size", 0)),
                    attachment_id=body["attachmentId"],
                    message_id=raw["id"],
                    part_id=part.get("partId", ""),
                )
            )
            continue
        if mime == "text/plain" and not text_body:
            text_body = _decode_part_body(part)
        elif mime == "text/html" and not html_body:
            html_body = _decode_part_body(part)

    if not text_body and html_body:
        text_body = _html_to_markdown(html_body)

    return MessageFull(
        id=raw["id"],
        thread_id=raw["threadId"],
        headers=headers,
        body_text=text_body,
        body_html=html_body,
        attachments=attachments,
        labels=list(raw.get("labelIds", [])),
        date=_parse_date(headers.get("Date")),
    )


def _html_to_markdown(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0
    h.ignore_images = False
    h.unicode_snob = True
    try:
        return h.handle(html)
    except Exception:
        log.exception("html2text failed")
        return html


# ---------------- Filters ----------------

def split_labels(labels: Iterable[Label]) -> tuple[list[Label], list[Label]]:
    system, user = [], []
    for lbl in labels:
        if lbl.is_system or is_system_label(lbl.id):
            system.append(lbl)
        else:
            user.append(lbl)
    return system, user


# ---------------- Helpers for Reply / Forward ----------------

def build_reply_message(
    original: MessageFull,
    sender: str,
    body: str,
    reply_all: bool = False,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    to_list = [original.from_addr]
    if reply_all:
        extra = [x for x in (original.to_addr.split(",") + original.cc.split(","))
                 if x.strip() and sender.lower() not in x.lower()]
        to_list.extend(x.strip() for x in extra)
    msg["To"] = ", ".join([t for t in to_list if t.strip()])

    subj = original.subject
    if not subj.lower().startswith("re:"):
        subj = f"Re: {subj}"
    msg["Subject"] = subj

    mid = original.message_id_header
    if mid:
        msg["In-Reply-To"] = mid
        refs = original.references
        msg["References"] = (refs + " " + mid).strip() if refs else mid

    quoted = "\n".join(f"> {line}" for line in original.body_text.splitlines())
    full_body = f"{body}\n\nOn {original.headers.get('Date','')}, {original.from_addr} wrote:\n{quoted}\n"
    msg.set_content(full_body)
    return msg


def build_forward_message(
    original: MessageFull,
    sender: str,
    to: str,
    body: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    subj = original.subject
    if not subj.lower().startswith("fwd:"):
        subj = f"Fwd: {subj}"
    msg["Subject"] = subj

    quoted = "\n".join(f"> {line}" for line in original.body_text.splitlines())
    full_body = (
        f"{body}\n\n"
        f"---------- Forwarded message ----------\n"
        f"From: {original.from_addr}\n"
        f"Date: {original.headers.get('Date','')}\n"
        f"Subject: {original.subject}\n"
        f"To: {original.to_addr}\n\n"
        f"{quoted}\n"
    )
    msg.set_content(full_body)
    return msg


def build_new_message(
    sender: str,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    attachments: Optional[list[Path]] = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg.set_content(body or "")
    for p in attachments or []:
        try:
            data = p.read_bytes()
        except OSError:
            log.exception("Attachment read failed %s", p)
            continue
        import mimetypes

        ctype, _ = mimetypes.guess_type(str(p))
        if ctype is None:
            maintype, subtype = "application", "octet-stream"
        else:
            maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=p.name)
    return msg
