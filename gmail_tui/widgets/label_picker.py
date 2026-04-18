from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView

from ..models import Label as LabelModel, is_system_label


@dataclass
class LabelPickResult:
    add: list[str]
    remove: list[str]
    create_name: Optional[str] = None


class LabelPickResultMessage(Message):
    def __init__(self, result: Optional[LabelPickResult]) -> None:
        super().__init__()
        self.result = result


class LabelPicker(ModalScreen[None]):
    DEFAULT_CSS = """
    LabelPicker { align: center middle; }
    #lp-box { width: 60; height: 24; background: $surface; border: round $accent; padding: 1 2; }
    #lp-list { height: 1fr; }
    #lp-actions { height: 3; align: right middle; }
    #lp-actions Button { margin-left: 1; }
    .label-item.selected { background: $success 30%; }
    """

    BINDINGS = [
        Binding("enter", "toggle_current", "Toggle", show=True),
        Binding("space", "toggle_current", "Toggle", show=False),
        Binding("ctrl+s", "apply", "Apply", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        all_labels: list[LabelModel],
        current_labels: list[str],
    ) -> None:
        super().__init__()
        self._all = [
            lbl for lbl in all_labels
            if not (lbl.is_system or is_system_label(lbl.id))
        ]
        self._initial = set(current_labels)
        self._selected = set(current_labels) & {lbl.id for lbl in self._all}
        self._filtered: list[LabelModel] = list(self._all)

    def compose(self) -> ComposeResult:
        with Vertical(id="lp-box"):
            yield Label("Assign labels — Space/Enter toggles, Ctrl+S applies")
            yield Input(placeholder="filter…", id="lp-input")
            yield ListView(id="lp-list")
            with Vertical(id="lp-actions"):
                yield Button("Apply (Ctrl+S)", id="lp-apply", variant="primary")
                yield Button("New label from filter (Ctrl+N)", id="lp-create")

    def on_mount(self) -> None:
        self._rebuild_list()
        self.query_one("#lp-input", Input).focus()

    def _rebuild_list(self) -> None:
        lv = self.query_one("#lp-list", ListView)
        lv.clear()
        for lbl in self._filtered:
            marker = "[x]" if lbl.id in self._selected else "[ ]"
            item = ListItem(Label(f"{marker} {lbl.name}"))
            item.label_id = lbl.id  # type: ignore[attr-defined]
            item.label_name = lbl.name  # type: ignore[attr-defined]
            lv.append(item)

    def on_input_changed(self, event: Input.Changed) -> None:
        q = (event.value or "").strip()
        if not q:
            self._filtered = list(self._all)
        else:
            scored = [
                (fuzz.partial_ratio(q.lower(), lbl.name.lower()), lbl)
                for lbl in self._all
            ]
            self._filtered = [lbl for score, lbl in sorted(
                scored, key=lambda x: x[0], reverse=True
            ) if score >= 60]
        self._rebuild_list()

    def action_toggle_current(self) -> None:
        lv = self.query_one("#lp-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(self._filtered):
            return
        lbl = self._filtered[idx]
        if lbl.id in self._selected:
            self._selected.remove(lbl.id)
        else:
            self._selected.add(lbl.id)
        self._rebuild_list()
        lv.index = idx

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_toggle_current()

    def action_apply(self) -> None:
        add = sorted(self._selected - self._initial)
        remove = sorted(self._initial - self._selected)
        inp = self.query_one("#lp-input", Input).value.strip()
        create_name = None
        if inp and not any(l.name.lower() == inp.lower() for l in self._all):
            # user didn't select; don't auto-create unless they hit the button
            pass
        self.app.post_message(LabelPickResultMessage(
            LabelPickResult(add=add, remove=remove, create_name=create_name)
        ))
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.app.post_message(LabelPickResultMessage(None))
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "lp-apply":
            self.action_apply()
            return
        if event.button.id == "lp-create":
            inp = self.query_one("#lp-input", Input).value.strip()
            if inp:
                self.app.post_message(LabelPickResultMessage(
                    LabelPickResult(add=[], remove=[], create_name=inp)
                ))
                self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "ctrl+n":
            inp = self.query_one("#lp-input", Input).value.strip()
            if inp:
                self.app.post_message(LabelPickResultMessage(
                    LabelPickResult(add=[], remove=[], create_name=inp)
                ))
                self.dismiss(None)
                event.stop()
