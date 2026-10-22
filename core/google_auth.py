"""
core/google_auth.py
====================
Google sign-in for Vision Insights -- the same OAuth flow, scopes, @esga.co
rule and credential handling as Workload Manager's core/google_auth.py, so
signing in feels identical in both apps.

* Scopes: Sheets read/write (the shared tracking sheet) plus the identity
  scopes that read back the signed-in email. Nothing else (no Drive, Gmail,
  Calendar or Contacts).
* The OAuth *client* (the "app" Google names on the consent screen) is
  bundled as google_oauth_client.json; it identifies the application, it is
  not a user credential.
* The refresh token never touches settings.json. It lives in the OS
  credential store (`keyring`); only if that is unavailable does a locked-down
  fallback file in the app-data folder hold it.
* Only @esga.co accounts are accepted, checked in code against what Google's
  own userinfo endpoint returned for the credential just granted.

Everything is import-light: the Google packages are only imported inside
functions, so the rest of the app still starts if they are missing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Optional

from core.appdata import data_dir as user_data_dir

# Audit is intentionally imported lazily in helpers below so authentication
# remains usable during very early application startup.

# ---------------------------------------------------------------------
# Scopes -- Sheets read/write plus the minimal identity scope needed to
# show which Google account is connected.
# ---------------------------------------------------------------------
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

KEYRING_SERVICE = "MessageHours-GoogleOAuth"
KEYRING_USERNAME = "refresh_token"

_ACCOUNT_META_FILENAME = "google_account.json"
_FALLBACK_TOKEN_FILENAME = "google_refresh_token.fallback"


class GoogleAuthError(Exception):
    """Raised for any part of the connect/refresh/reset flow that the
    caller should surface to the person as a plain-language message."""


class OAuthCancelled(GoogleAuthError):
    """Raised when the person closes/cancels the consent screen. Callers
    must treat this as a no-op: no partial state is written."""


@dataclass
class ConnectedAccount:
    email: str
    domain: str
    name: str = ""
    connected_at: str = ""   # UTC "YYYY-MM-DD HH:MM:SS" of the sign-in (Data Health shows "signed in N days ago"); older files have none

    def to_dict(self) -> dict:
        return asdict(self)


def _account_meta_path() -> str:
    return os.path.join(user_data_dir(), _ACCOUNT_META_FILENAME)


def _load_account_meta() -> Optional[ConnectedAccount]:
    path = _account_meta_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ConnectedAccount(email=data.get("email", ""), domain=data.get("domain", ""), name=data.get("name", ""),
                                connected_at=data.get("connected_at", "") or "")
    except (json.JSONDecodeError, OSError):
        return None


def _save_account_meta(account: ConnectedAccount) -> None:
    # Account metadata (email/domain) is not a secret -- it is fine to
    # live in a small plain file alongside settings.json. The refresh
    # token itself never goes here; it lives only in `keyring`.
    path = _account_meta_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(account.to_dict(), f, indent=2)


def _clear_account_meta() -> None:
    path = _account_meta_path()
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _audit_dev_event(action: str, detail: str, old=None, new=None) -> None:
    """Best-effort developer note; never logs credential material."""
    import logging
    logging.getLogger("message_hours.auth").warning("%s: %s", action, detail)


def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        _audit_dev_event(
            "Keyring unavailable",
            "The keyring package could not be imported; Google OAuth is using the degraded local fallback credential store. Review packaging/dependencies before release.",
        )
        return None


def _fallback_token_path() -> str:
    return os.path.join(user_data_dir(), _FALLBACK_TOKEN_FILENAME)


def _write_fallback_token(refresh_token: str) -> None:
    path = _fallback_token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(refresh_token)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _audit_dev_event(
        "Google OAuth credential fallback activated",
        "OS keyring storage was unavailable, so the refresh token was stored in the local protected fallback file. DEV ACTION REQUIRED: verify keyring packaging/backend before production release. Credential value intentionally excluded from audit.",
    )


def _read_fallback_token() -> Optional[str]:
    path = _fallback_token_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except OSError:
        return None


def _delete_fallback_token() -> None:
    try:
        os.remove(_fallback_token_path())
    except OSError:
        pass


def _store_refresh_token(refresh_token: str) -> None:
    keyring = _keyring()
    if keyring is not None:
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, refresh_token)
            _delete_fallback_token()
            return
        except Exception as exc:
            _audit_dev_event(
                "Keyring backend failure",
                f"OS credential-store write failed ({type(exc).__name__}); using the degraded local fallback credential store. DEV ACTION REQUIRED: inspect the packaged keyring backend.",
            )
    _write_fallback_token(refresh_token)


def _load_refresh_token() -> Optional[str]:
    keyring = _keyring()
    if keyring is not None:
        try:
            token = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
            if token:
                return token
        except Exception as exc:
            _audit_dev_event(
                "Keyring backend read failure",
                f"OS credential-store read failed ({type(exc).__name__}); checking the degraded local fallback credential store.",
            )
    return _read_fallback_token()


def _delete_refresh_token() -> None:
    keyring = _keyring()
    if keyring is not None:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception:
            pass
    _delete_fallback_token()


def _oauth_client_config() -> dict:
    """The bundled OAuth client ("app") configuration. This identifies
    the application to Google; it is not the same thing as a user's
    refresh token and is safe to ship inside the packaged app.

    Looked up, in order: an env var (useful for dev/CI), then a
    `google_oauth_client.json` file shipped next to the app. Either must
    contain the standard Google "Desktop app" client JSON shape.
    """
    import sys

    env_path = os.environ.get("MESSAGE_HOURS_GOOGLE_CLIENT_JSON")
    candidates = []
    if env_path:
        candidates.append(env_path)
    # Main Settings can point the app at a different OAuth client JSON. This
    # is deliberately a path, not the JSON contents, so changing accounts or
    # client configurations is a one-click settings operation.
    try:
        from core import settings_store
        configured = settings_store.get_settings().get("google_account", {}).get("client_config_path", "")
        if configured:
            if not os.path.exists(configured):
                raise GoogleAuthError(f"The saved Google OAuth client JSON no longer exists: {configured}")
            candidates.append(configured)
    except Exception:
        pass
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates.append(os.path.join(base, "google_oauth_client.json"))

    for path in candidates:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

    raise GoogleAuthError(
        "No Google OAuth client configuration was found. This should be bundled "
        "with the application as 'google_oauth_client.json' (a Desktop-app OAuth "
        "client downloaded from Google Cloud Console and configured for external "
        "use), or pointed to via the MESSAGE_HOURS_GOOGLE_CLIENT_JSON "
        "environment variable for local development."
    )



def validate_client_config(path: str) -> dict:
    """Validate a user-selected Google OAuth client JSON without connecting."""
    path = str(path or "").strip()
    if not path or not os.path.exists(path):
        raise GoogleAuthError("Google OAuth client JSON was not found.")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise GoogleAuthError(f"Couldn't read the Google OAuth client JSON: {exc}") from exc
    block = data.get("installed") or data.get("web")
    if not isinstance(block, dict) or not block.get("client_id") or not block.get("auth_uri") or not block.get("token_uri"):
        raise GoogleAuthError("That JSON does not look like a valid Google OAuth Desktop/Web client configuration.")
    return {"ok": True, "path": path, "client_id": block.get("client_id", "")}

REQUIRED_EMAIL_DOMAIN = "esga.co"


def status() -> dict:
    """What Settings -> Google Account shows. Never touches the network."""
    account = _load_account_meta()
    has_token = _load_refresh_token() is not None
    connected = bool(account and has_token)
    keyring_module = _keyring()
    storage_backend = "keyring" if keyring_module is not None else "fallback"
    if storage_backend == "fallback":
        _audit_dev_event(
            "Google OAuth fallback active",
            "Google account status is backed by the degraded local credential fallback. DEV ACTION REQUIRED: restore working keyring packaging/backend before production release.",
        )
    return {
        "connected": connected,
        "email": account.email if (connected and account) else "",
        "name": account.name if (connected and account) else "",
        "required_domain": REQUIRED_EMAIL_DOMAIN,
        "storage_backend": storage_backend,
        "degraded_storage": storage_backend == "fallback",
    }


def connect() -> dict:
    """Run the interactive OAuth flow: opens the system browser, listens on
    a local loopback port for the redirect, verifies the resulting account
    account email, and stores the refresh token in the OS
    credential store. Returns {"ok": True, "email": ...} on success.

    This is a blocking call (it waits for the person to finish in the
    browser) and should be run off the UI thread by the caller.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    client_config = _oauth_client_config()
    flow = InstalledAppFlow.from_client_config(client_config, scopes=GOOGLE_SCOPES)

    try:
        creds = flow.run_local_server(
            port=0,
            open_browser=True,
            authorization_prompt_message="",
            # Always request an offline grant and force the consent step.
            # Google may otherwise omit refresh_token when this account has
            # already authorized the OAuth client, which makes the browser
            # callback look successful while Vision Insights remains
            # signed out.
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
            success_message="Google sign-in received. Return to Vision Insights - you are signed in.",
        )
    except Exception as exc:
        message = str(exc).lower()
        if "access_denied" in message or "cancel" in message or "closed" in message:
            raise OAuthCancelled("Google sign-in was cancelled. Nothing was changed.") from exc
        raise GoogleAuthError(f"Google sign-in failed: {exc}") from exc

    if not creds:
        raise OAuthCancelled("Google sign-in was cancelled. Nothing was changed.")

    if not creds.refresh_token:
        raise GoogleAuthError(
            "Google sign-in completed, but no refresh token was returned. "
            "Please click Sign in with Google again and complete the consent screen."
        )

    email, domain, name = _verify_account(creds)

    # Hard requirement: only @esga.co accounts may connect. Checked here,
    # in code, against what Google's own userinfo endpoint returned for
    # the credential that was just granted -- not against anything the
    # client claims -- so this can't be bypassed by editing local state.
    if domain.lower() != REQUIRED_EMAIL_DOMAIN:
        raise GoogleAuthError(
            f"Only @{REQUIRED_EMAIL_DOMAIN} email addresses can sign in to Vision Insights. "
            f"\u201c{email}\u201d isn't an @{REQUIRED_EMAIL_DOMAIN} account, so nothing was connected."
        )

    _store_refresh_token(creds.refresh_token)
    from datetime import datetime, timezone
    _save_account_meta(ConnectedAccount(email=email, domain=domain, name=name,
                                        connected_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    return {"ok": True, "email": email, "name": name}


def _verify_account(creds) -> tuple:
    """Calls Google's tokeninfo/userinfo endpoint to confirm the email and
    display name associated with the credentials that were just granted,
    rather than trusting anything client-side."""
    import requests as _requests  # google-auth-oauthlib already depends on requests transitively
    from google.auth.transport.requests import Request as GoogleRequest

    if not creds.valid:
        creds.refresh(GoogleRequest())

    resp = _requests.get(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        raise GoogleAuthError(f"Could not verify the connected Google account ({resp.status_code}).")
    info = resp.json()
    email = info.get("email", "")
    if not email or "@" not in email:
        raise GoogleAuthError("Google did not return an account email to verify.")
    domain = email.split("@", 1)[1]
    # Prefer the full display name; fall back to given_name, then to the
    # part of the email before the @ (title-cased) if Google returns
    # neither -- the welcome animation and audit log always need *some*
    # name to show.
    name = (info.get("name") or info.get("given_name") or email.split("@", 1)[0].replace(".", " ").title()).strip()
    return email, domain, name


def _credentials_for(refresh_token: str):
    """Credentials object for the saved refresh token (nothing is sent to Google until it is refreshed)."""
    from google.oauth2.credentials import Credentials
    client_config = _oauth_client_config()
    installed = client_config.get("installed") or client_config.get("web") or {}
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=installed.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=installed.get("client_id"),
        client_secret=installed.get("client_secret"),
        scopes=GOOGLE_SCOPES,
    )


def get_credentials():
    """Returns valid google.oauth2.credentials.Credentials, refreshing the
    access token automatically if needed. Raises GoogleAuthError (asking
    the person to reconnect) if there is no stored connection or the
    refresh token has been revoked."""
    from google.auth.transport.requests import Request as GoogleRequest
    from google.auth.exceptions import RefreshError

    refresh_token = _load_refresh_token()
    account = _load_account_meta()
    if not refresh_token or not account:
        raise GoogleAuthError(
            "Not connected to Google on this PC, so changes are kept here until someone signs in with an @esga.co account (optional for counting)."
        )

    creds = _credentials_for(refresh_token)
    try:
        creds.refresh(GoogleRequest())
    except RefreshError as exc:
        # The refresh token was revoked (e.g. from the person's Google
        # Account security page) or expired. Clear local state cleanly
        # and require a fresh reconnect rather than looping on a dead
        # token.
        reset()
        _log_token_problem("revoked", account.email)
        raise GoogleAuthError(
            "Your Google sign-in was revoked or expired. Please sign in again."
        ) from exc
    return creds


def _log_token_problem(state: str, email: str = "") -> None:
    """Put a revoked / expired sign-in in the Data Health conflict log (the local sign-in is cleared right after, so the
    live check alone could no longer say WHY the person is signed out)."""
    try:
        from core import conflicts
        if state == "revoked":
            conflicts.record(
                "token_revoked", "Sign-in & shared sheet", "high", "Your Google sign-in was revoked or has expired",
                "Google no longer accepts the saved sign-in" + (f" for {email}" if email else "") + ". Sign in again to use Morning Update "
                "and to refresh the Roles list. Comparing files, asset counts and history are not affected.",
                parts=[email], source="google", actions=[conflicts.action("sign-in", "Sign in again")])
    except Exception:  # noqa: BLE001
        pass


def check_token() -> dict:
    """Ask Google whether the saved sign-in still works, by refreshing it once. This is the only way to know: Google does
    not tell the app when a sign-in will stop working. Read-only - nothing is changed or cleared (unlike get_credentials,
    which signs the person out when Google refuses). Runs only when the person asks (Data Health -> Check now).

    Returns {"state": "valid" | "revoked" | "unreachable" | "not_signed_in" | "error", "message": ..., "expires_at": ...}."""
    refresh_token = _load_refresh_token()
    account = _load_account_meta()
    if not refresh_token or not account:
        return {"state": "not_signed_in", "message": "Not signed in on this PC (signing in is optional)."}
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.auth.exceptions import RefreshError
        creds = _credentials_for(refresh_token)
    except GoogleAuthError as exc:
        return {"state": "error", "message": str(exc)}
    except ImportError:
        return {"state": "error", "message": "The Google sign-in packages are not installed on this PC."}
    try:
        creds.refresh(GoogleRequest())
    except RefreshError as exc:
        _log_token_problem("revoked", account.email)
        return {"state": "revoked", "message": "Google no longer accepts this sign-in (revoked or expired). Sign in again.", "technical": str(exc)}
    except Exception as exc:  # noqa: BLE001 - no network, proxy, DNS ...
        return {"state": "unreachable", "message": f"Couldn't reach Google to check the sign-in: {exc}"}
    return {"state": "valid", "message": "Google accepts this sign-in.", "expires_at": creds.expiry.isoformat() if getattr(creds, "expiry", None) else ""}


def connected_at() -> str:
    """When this PC's sign-in was made (UTC), or "" for a sign-in made before this was recorded."""
    account = _load_account_meta()
    return account.connected_at if account else ""


def reset() -> dict:
    """Clears the local connection and best-effort revokes it with
    Google, so the credential can't silently keep working, then leaves
    the machine ready for a different ESGA user to connect."""
    refresh_token = _load_refresh_token()
    if refresh_token:
        try:
            import requests as _requests
            _requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": refresh_token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        except Exception:
            pass  # best-effort -- local state is cleared regardless
    _delete_refresh_token()
    _clear_account_meta()
    return {"ok": True}


# ---------------------------------------------------------------- which OAuth client: bundled, or one of your own
# Same idea as Workload Manager's Settings > Google OAuth Client: the app ships with a bundled client and works
# out of the box; "Change" points it at a different client JSON (the path is stored, never the contents).
def client_status() -> dict:
    import sys
    from core import settings_store
    configured = (settings_store.get_settings().get("google_account", {}) or {}).get("client_config_path", "") or ""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    bundled_ok = os.path.exists(os.path.join(base, "google_oauth_client.json"))
    env = os.environ.get("MESSAGE_HOURS_GOOGLE_CLIENT_JSON", "")
    out = {"mode": "custom" if configured else "bundled", "path": configured, "filename": os.path.basename(configured) if configured else "",
           "exists": os.path.exists(configured) if configured else True, "bundled_present": bundled_ok, "overridden_by_env": bool(env)}
    return out


def client_set(path: str) -> dict:
    """Use a different client JSON. Validated first; the current Google connection is reset so the next
    sign-in uses the new client."""
    from core import settings_store
    info = validate_client_config(path)
    s = settings_store.get_settings()
    s.setdefault("google_account", {})["client_config_path"] = info["path"]
    settings_store.save_settings(s)
    reset()
    return {**client_status(), "ok": True}


def client_clear() -> dict:
    """Back to the client bundled with the app (and reset the connection, so the next sign-in uses it)."""
    from core import settings_store
    s = settings_store.get_settings()
    s.setdefault("google_account", {})["client_config_path"] = ""
    settings_store.save_settings(s)
    reset()
    return {**client_status(), "ok": True}
