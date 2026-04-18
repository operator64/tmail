# Developing — Gmail TUI

## Architecture overview

```
┌────────────────────────────────────────────────────┐
│ Textual UI (event loop, async)                     │
│ ┌─────────┐ ┌───────────────┐ ┌────────────────┐   │
│ │ Sidebar │ │ MessageList   │ │ Preview        │   │
│ └────┬────┘ └───────┬───────┘ └────────┬───────┘   │
│      │ events       │ events           │ events    │
│      └──────────┬───┴───────────┬──────┘           │
│                 ▼               ▼                  │
│            app.py (GmailTUIApp)                    │
│                 │                                  │
│ ┌───────────────┴───────────────┐ run_worker(...)  │
│ ▼                               ▼                  │
│ Cache (SQLite, WAL)             GmailClient        │
│   - messages                      - blocking API   │
│   - bodies                        - retry/backoff  │
│   - labels                        - auth refresh   │
│   - pending_actions               │                │
│                                   ▼                │
│                            Google Gmail API        │
└────────────────────────────────────────────────────┘
```

### Threading model

The Gmail API wrapper (`gmail_client.GmailClient`) is purely blocking. Every call path from a UI event goes through `self.run_worker(func, thread=True)` so the Textual event loop never stalls. Workers are grouped (`group="list"`, `group="modify"`, etc.) so that opening a new folder cancels a stale list fetch.

Callbacks into the UI use `self.call_from_thread(...)` to marshal back onto the main thread.

The SQLite cache opens **one connection per thread** via `threading.local`. WAL mode allows concurrent readers while a writer holds the WAL lock.

### Modules

| File | Responsibility |
|---|---|
| `__main__.py` | Entry point; enables logging, warns if not in Windows Terminal, launches the app. |
| `auth.py` | OAuth flow, keyring storage of refresh token, credential refresh. |
| `gmail_client.py` | Typed wrapper around the Gmail REST API with retry/backoff, MIME builders for reply/forward/new, HTML→text conversion. |
| `cache.py` | SQLite schema (WAL), upserts, offline action queue, local drafts, sync state. |
| `models.py` | Dataclasses: `Label`, `MessageSummary`, `MessageFull`, `Attachment`, `PendingAction`. |
| `app.py` | Textual App class; owns the client, cache, and widget lifecycle; dispatches widget events to workers. |
| `widgets/sidebar.py` | Tree of system folders + user labels (hierarchical on `/`). |
| `widgets/message_list.py` | DataTable of summaries; multi-select with `Space`; emits `MessageOpened`, `LoadMoreRequested`, `MessageContextMenuRequested`. |
| `widgets/preview.py` | Markdown body + headers + attachment list. |
| `widgets/compose.py` | ModalScreen for new/reply/forward. |
| `widgets/label_picker.py` | ModalScreen with fuzzy filter (`rapidfuzz`). |
| `widgets/context_menu.py` | ModalScreen positioned at mouse coords. |
| `widgets/help.py` | Keybinding overlay. |

## Gmail API gotchas (things to preserve when refactoring)

- **Labels are the truth.** Star = add `STARRED` label. Archive = remove `INBOX`. Unread = `UNREAD` label. Never use `messages.delete` unless the user explicitly confirms — use `messages.trash`.
- **Reply threading.** `threadId` in the request body plus correct `In-Reply-To` and `References` headers, otherwise Gmail creates a new thread. `Re:` prefix on subject only if not already present.
- **Base64 URL-safe for `raw`.** `base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii').rstrip('=')`. Using normal `b64encode` will silently corrupt the message.
- **History 404.** `startHistoryId` older than ~7 days returns 404 — we wipe the cache and full-resync.
- **Rate limits.** Use `messages.batchModify` for bulk operations. Exponential backoff on 429/503.

## Adding a feature

1. If it involves a new API call, extend `GmailClient` with a blocking method. Wrap the google-api-python-client call in `_retryable(lambda: ...)`.
2. If it needs to persist across restarts, add a column to `cache.SCHEMA` and an upsert method.
3. If it's a user-visible surface, create or extend a widget in `widgets/`. Widgets should emit `Message` subclasses and never call the Gmail API themselves.
4. In `app.py`, handle the widget event and dispatch to a worker (`run_worker(...thread=True)`). Wrap optimistic UI updates around the API call so the list reflects state instantly.

## Running & debugging

```powershell
# verbose logs are in %APPDATA%\gmail-tui\log.txt
Get-Content "$env:APPDATA\gmail-tui\log.txt" -Wait

# force re-auth (clears refresh token + account file)
python -c "from gmail_tui import auth; auth.reset_auth()"
```

## Testing manually against the 10 acceptance criteria

1. **Fresh auth**: delete `account.json` and credential store entry, run `python -m gmail_tui`.
2. **Inbox speed**: measure first paint vs. second paint.
3. **History polling**: send yourself a mail in the web UI, wait up to 90 s.
4. **Cross-client modify**: star/archive here, verify on gmail.com.
5. **Reply threading**: reply, open the thread on gmail.com.
6. **Mouse + keyboard**: right-click a message, also press `m`.
7. **Offline**: disable WiFi, modify a label, re-enable — check `pending_actions` flushes.
8. **Non-blocking**: open a folder with 500+ messages; UI must remain responsive.
9. **Bulk modify**: `Space` on 50 rows, `u` → single `batchModify` request (grep log).
10. **History expiry**: set `last_history_id` to `1` in sqlite, restart — must resync, not crash.

## Known limitations

- No native Windows file picker for attachments (Textual cannot). Paste absolute paths.
- Panel boundaries are not mouse-draggable — use `[`, `]`, `{`, `}`.
- Single account. No multi-account switcher.
- Drafts persist locally (`local_drafts` table) but are not synced to Gmail's server drafts until send — the `drafts.create` path exists in `gmail_client.py` but is not currently wired into the compose UI.
