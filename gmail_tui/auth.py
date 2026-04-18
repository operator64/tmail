from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import keyring
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .logging_setup import app_data_dir

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
KEYRING_SERVICE = "gmail-tui"
ACCOUNT_FILE_NAME = "account.json"


class AuthError(Exception):
    pass


class CredentialsMissingError(AuthError):
    pass


def credentials_file_path() -> Path:
    return app_data_dir() / "credentials.json"


def account_file_path() -> Path:
    return app_data_dir() / ACCOUNT_FILE_NAME


def _load_account_email() -> Optional[str]:
    p = account_file_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("email")
    except Exception:
        log.exception("Failed to read account file")
        return None


def _save_account_email(email: str) -> None:
    account_file_path().write_text(
        json.dumps({"email": email}), encoding="utf-8"
    )


def _load_refresh_token(email: str) -> Optional[str]:
    try:
        return keyring.get_password(KEYRING_SERVICE, email)
    except Exception:
        log.exception("keyring read failed")
        return None


def _save_refresh_token(email: str, refresh_token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, email, refresh_token)


def _delete_refresh_token(email: str) -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, email)
    except Exception:
        log.exception("keyring delete failed")


def _load_client_config() -> dict:
    p = credentials_file_path()
    if not p.exists():
        raise CredentialsMissingError(
            f"credentials.json missing at {p}. "
            "Download an OAuth Desktop client JSON from Google Cloud Console."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def _client_config_dict() -> dict:
    return _load_client_config()


def _client_id_secret() -> tuple[str, str, str]:
    cfg = _load_client_config()
    inner = cfg.get("installed") or cfg.get("web")
    if not inner:
        raise AuthError("credentials.json has neither 'installed' nor 'web' key")
    return (
        inner["client_id"],
        inner["client_secret"],
        inner.get("token_uri", "https://oauth2.googleapis.com/token"),
    )


def load_credentials() -> Optional[Credentials]:
    email = _load_account_email()
    if not email:
        return None
    refresh = _load_refresh_token(email)
    if not refresh:
        return None
    client_id, client_secret, token_uri = _client_id_secret()
    creds = Credentials(
        token=None,
        refresh_token=refresh,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    return creds


def refresh_if_needed(creds: Credentials) -> Credentials:
    if creds.expired or not creds.token:
        creds.refresh(Request())
    return creds


def perform_oauth_flow() -> tuple[Credentials, str]:
    cfg = _client_config_dict()
    flow = InstalledAppFlow.from_client_config(cfg, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    email = _fetch_email_for_creds(creds)
    if not creds.refresh_token:
        raise AuthError(
            "Google returned no refresh token. "
            "Revoke the app in your Google account and re-authorize."
        )
    _save_refresh_token(email, creds.refresh_token)
    _save_account_email(email)
    return creds, email


def _fetch_email_for_creds(creds: Credentials) -> str:
    from googleapiclient.discovery import build

    svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = svc.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def get_or_create_credentials() -> tuple[Credentials, str]:
    creds = load_credentials()
    email = _load_account_email()
    if creds and email:
        try:
            creds = refresh_if_needed(creds)
            return creds, email
        except Exception:
            log.exception("Refresh failed, re-authorizing")
            _delete_refresh_token(email)
    return perform_oauth_flow()


def reset_auth() -> None:
    email = _load_account_email()
    if email:
        _delete_refresh_token(email)
    try:
        account_file_path().unlink()
    except FileNotFoundError:
        pass
