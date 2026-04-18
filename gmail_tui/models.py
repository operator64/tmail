from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

SYSTEM_LABEL_IDS = {
    "INBOX",
    "STARRED",
    "SENT",
    "DRAFT",
    "TRASH",
    "SPAM",
    "UNREAD",
    "IMPORTANT",
    "CHAT",
}


def is_system_label(label_id: str) -> bool:
    if label_id in SYSTEM_LABEL_IDS:
        return True
    if label_id.startswith("CATEGORY_"):
        return True
    return False


@dataclass
class Label:
    id: str
    name: str
    type: str  # "system" or "user"
    messages_unread: int = 0

    @property
    def is_system(self) -> bool:
        return self.type == "system"

    @property
    def display_name(self) -> str:
        mapping = {
            "INBOX": "Inbox",
            "STARRED": "Starred",
            "SENT": "Sent",
            "DRAFT": "Drafts",
            "TRASH": "Trash",
            "SPAM": "Spam",
            "IMPORTANT": "Important",
        }
        if self.is_system:
            return mapping.get(self.id, self.name.title())
        return self.name


@dataclass
class MessageSummary:
    id: str
    thread_id: str
    from_addr: str
    subject: str
    snippet: str
    date: Optional[datetime]
    labels: list[str] = field(default_factory=list)
    has_attachment: bool = False

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.labels

    @property
    def is_starred(self) -> bool:
        return "STARRED" in self.labels


@dataclass
class Attachment:
    filename: str
    mime_type: str
    size: int
    attachment_id: str
    message_id: str
    part_id: str = ""


@dataclass
class MessageFull:
    id: str
    thread_id: str
    headers: dict[str, str]
    body_text: str
    body_html: str
    attachments: list[Attachment] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    date: Optional[datetime] = None

    @property
    def subject(self) -> str:
        return self.headers.get("Subject", "")

    @property
    def from_addr(self) -> str:
        return self.headers.get("From", "")

    @property
    def to_addr(self) -> str:
        return self.headers.get("To", "")

    @property
    def cc(self) -> str:
        return self.headers.get("Cc", "")

    @property
    def message_id_header(self) -> str:
        return self.headers.get("Message-ID", self.headers.get("Message-Id", ""))

    @property
    def references(self) -> str:
        return self.headers.get("References", "")


@dataclass
class Thread:
    id: str
    messages: list[MessageFull] = field(default_factory=list)


@dataclass
class PendingAction:
    id: int
    action_type: str  # "modify" | "trash" | "send" | "draft" | "delete"
    payload: dict
    created_at: datetime
    attempts: int = 0
