# Gmail TUI (Windows)

A text-based Gmail client for Windows Terminal. Mouse and keyboard. Single account.

## Requirements

- Windows 10/11
- **Windows Terminal** (cmd.exe is not supported)
- Python 3.11+

## Google Cloud setup (one-time)

1. Go to https://console.cloud.google.com/ and create or select a project.
2. Enable the **Gmail API** (APIs & Services → Library → "Gmail API" → Enable).
3. Configure an OAuth consent screen (External, testing mode is fine).
4. Create an OAuth client:
   - APIs & Services → Credentials → *Create Credentials* → *OAuth client ID*.
   - Application type: **Desktop app**.
   - Download the JSON.
5. Save the JSON to:

   ```
   %APPDATA%\gmail-tui\credentials.json
   ```

   (The folder will be created on first run of the app; you can create it manually too.)

## Install

```powershell
cd C:\path\to\tmail
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```powershell
python -m gmail_tui
```

On first launch:
1. A browser window opens with Google's consent screen.
2. After authorizing, control returns to the app.
3. The refresh token is written to the **Windows Credential Manager** under the service name `gmail-tui`.
4. Your account email is stored at `%APPDATA%\gmail-tui\account.json`.

Subsequent launches use the stored credentials and do not open a browser.

### Windows Firewall

During OAuth the app starts a one-shot localhost listener on a random free port. Windows may show a firewall prompt — allow it on **private networks only**.

## Keybindings

See `?` in-app, or:

| Key | Action |
|---|---|
| `j` / `k` | Next / previous message |
| `Enter` | Open message |
| `Tab` | Cycle pane focus |
| `c` | Compose |
| `r` / `R` | Reply / Reply all |
| `f` | Forward |
| `s` | Toggle star |
| `e` | Archive |
| `#` | Move to Trash |
| `u` | Toggle read/unread |
| `l` | Label picker |
| `m` | Context menu |
| `/` | Search (Gmail query syntax) |
| `Ctrl+R` | Refresh current folder |
| `[` / `]` | Shrink / grow sidebar |
| `{` / `}` | Grow / shrink preview |
| `Space` | Multi-select toggle |
| `?` | Help overlay |
| `q` | Quit |

## Storage layout

| Path | Purpose |
|---|---|
| `%APPDATA%\gmail-tui\credentials.json` | OAuth client (provided by you) |
| `%APPDATA%\gmail-tui\account.json` | Stored account email |
| `%APPDATA%\gmail-tui\cache.db` | SQLite message cache |
| `%APPDATA%\gmail-tui\log.txt` | Rotating log (1 MB × 3) |
| Windows Credential Manager → `gmail-tui` | OAuth refresh token |

## Troubleshooting

### "credentials.json missing"

Complete the **Google Cloud setup** section.

### "Session expired — please re-run"

The refresh token was revoked or rotated out. Delete the `gmail-tui` entry from Windows Credential Manager, then restart.

### Slow on first run

The first inbox load fetches metadata for up to 50 messages. Subsequent loads are instant because of the SQLite cache.

### UI looks broken

Confirm you are in Windows Terminal (`WT_SESSION` env var is set). The app prints a warning on stderr when run elsewhere.

## Scope

- Single account. Multi-account is not supported.
- Read-modify scope (`gmail.modify`). Cannot permanently delete without confirmation; Trash-only by default.
