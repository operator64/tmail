from __future__ import annotations

from typing import Optional

from textual.message import Message
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from ..models import Label, is_system_label


SYSTEM_ORDER = ["INBOX", "STARRED", "SENT", "DRAFT", "ALL_MAIL", "SPAM", "TRASH"]
SYSTEM_ICONS = {
    "INBOX": "📥",
    "STARRED": "⭐",
    "SENT": "📤",
    "DRAFT": "📝",
    "ALL_MAIL": "📦",
    "SPAM": "🚫",
    "TRASH": "🗑",
}
SYSTEM_TITLES = {
    "INBOX": "Inbox",
    "STARRED": "Starred",
    "SENT": "Sent",
    "DRAFT": "Drafts",
    "ALL_MAIL": "All Mail",
    "SPAM": "Spam",
    "TRASH": "Trash",
}


class LabelSelected(Message):
    def __init__(self, label_id: Optional[str], query: Optional[str] = None) -> None:
        super().__init__()
        self.label_id = label_id
        self.query = query


class Sidebar(Tree):
    """Tree showing system folders + user labels."""

    DEFAULT_CSS = """
    Sidebar { padding: 0; }
    Sidebar > .tree--cursor { background: $boost; }
    """

    def __init__(self) -> None:
        super().__init__(label="Mail")
        self.show_root = False
        self.guide_depth = 2
        self._labels: list[Label] = []

    def on_mount(self) -> None:
        self._rebuild()

    def set_labels(self, labels: list[Label]) -> None:
        self._labels = labels
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear()
        root = self.root

        by_id = {lbl.id: lbl for lbl in self._labels}
        # System folders
        for sys_id in SYSTEM_ORDER:
            unread = 0
            title = SYSTEM_TITLES[sys_id]
            icon = SYSTEM_ICONS[sys_id]
            if sys_id == "ALL_MAIL":
                # pseudo — no label id
                node = root.add_leaf(f"{icon} {title}")
                node.data = {"kind": "query", "query": "in:anywhere"}
                continue
            lbl = by_id.get(sys_id)
            if lbl:
                unread = lbl.messages_unread
            label_str = f"{icon} {title}"
            if unread:
                label_str += f"  {unread}"
            node = root.add_leaf(label_str)
            node.data = {"kind": "label", "label_id": sys_id}

        # separator
        sep = root.add_leaf("─── Labels ───")
        sep.data = {"kind": "separator"}

        # user labels — hierarchical via "/"
        user_labels = sorted(
            [lbl for lbl in self._labels if not (lbl.is_system or is_system_label(lbl.id))],
            key=lambda l: l.name.lower(),
        )
        tree_index: dict[str, TreeNode] = {}
        for lbl in user_labels:
            parts = lbl.name.split("/")
            parent = root
            for i, part in enumerate(parts):
                path_key = "/".join(parts[: i + 1])
                existing = tree_index.get(path_key)
                is_last = i == len(parts) - 1
                if existing is None:
                    display = f"🏷 {part}"
                    if is_last and lbl.messages_unread:
                        display += f"  {lbl.messages_unread}"
                    if is_last:
                        node = parent.add_leaf(display)
                        node.data = {"kind": "label", "label_id": lbl.id}
                    else:
                        node = parent.add(display, expand=True)
                        node.data = {"kind": "group"}
                    tree_index[path_key] = node
                    parent = node
                else:
                    parent = existing
        self.root.expand()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data or {}
        kind = data.get("kind")
        if kind == "label":
            self.post_message(LabelSelected(label_id=data["label_id"]))
        elif kind == "query":
            self.post_message(LabelSelected(label_id=None, query=data["query"]))
        # separator/group — no-op
