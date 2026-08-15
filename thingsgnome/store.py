"""Local persistence: credentials (keyring) + a cache of replayed entities.

Credentials go into the system keyring (GNOME Keyring via libsecret) using the
`keyring` package when available, so the password never sits in plaintext. If
`keyring` is missing we fall back to a 0600 file and flag it, so you can choose to
install keyring for proper security.

The entity cache lets the app paint your tasks instantly on launch, then sync in the
background and refresh.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict
from pathlib import Path

from .sync.client import Entity

APP_ID = "com.idleendeavour.ThingsGNOME"
KEYRING_SERVICE = APP_ID


def _xdg(env: str, default: str) -> Path:
    base = os.environ.get(env) or os.path.expanduser(default)
    p = Path(base) / APP_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", "~/.config")


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", "~/.local/share")


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
class Credentials:
    """Stores email in a settings file; password in the keyring (or fallback file)."""

    def __init__(self) -> None:
        self._settings_path = config_dir() / "settings.json"
        self._fallback_path = config_dir() / "credentials.json"
        self.using_fallback = False

    # settings (non-secret) ---------------------------------------------------
    def load_settings(self) -> dict:
        if self._settings_path.exists():
            try:
                return json.loads(self._settings_path.read_text())
            except (ValueError, OSError):
                return {}
        return {}

    def save_settings(self, settings: dict) -> None:
        self._settings_path.write_text(json.dumps(settings, indent=2))

    # secrets -----------------------------------------------------------------
    def get_email(self) -> str | None:
        return self.load_settings().get("email")

    def get_password(self) -> str | None:
        email = self.get_email()
        if not email:
            return None
        try:
            import keyring

            pw = keyring.get_password(KEYRING_SERVICE, email)
            if pw is not None:
                return pw
        except Exception:
            pass
        # fallback file
        if self._fallback_path.exists():
            self.using_fallback = True
            try:
                return json.loads(self._fallback_path.read_text()).get("password")
            except (ValueError, OSError):
                return None
        return None

    def save(self, email: str, password: str) -> None:
        settings = self.load_settings()
        settings["email"] = email
        self.save_settings(settings)
        try:
            import keyring

            keyring.set_password(KEYRING_SERVICE, email, password)
            self.using_fallback = False
            # If we previously wrote a fallback, remove it.
            if self._fallback_path.exists():
                self._fallback_path.unlink()
            return
        except Exception:
            pass
        # fallback: 0600 file
        self.using_fallback = True
        self._fallback_path.write_text(json.dumps({"password": password}))
        os.chmod(self._fallback_path, stat.S_IRUSR | stat.S_IWUSR)

    def clear(self) -> None:
        email = self.get_email()
        if email:
            try:
                import keyring

                keyring.delete_password(KEYRING_SERVICE, email)
            except Exception:
                pass
        for p in (self._settings_path, self._fallback_path):
            if p.exists():
                p.unlink()


# --------------------------------------------------------------------------- #
# entity cache
# --------------------------------------------------------------------------- #
class Cache:
    # Bump this whenever the replay/model logic changes in a way that makes an
    # existing cache stale. A mismatch makes load() return empty, which forces a
    # full re-sync from index 0 -- so logic fixes apply without a manual resync.
    REPLAY_VERSION = 4

    def __init__(self) -> None:
        self._path = data_dir() / "cache.json"

    def load(self) -> tuple[dict[str, Entity], int]:
        if not self._path.exists():
            return {}, 0
        try:
            blob = json.loads(self._path.read_text())
        except (ValueError, OSError):
            return {}, 0
        if blob.get("replay_version") != self.REPLAY_VERSION:
            # logic changed since this cache was written -> rebuild from scratch
            return {}, 0
        entities = {
            uuid: Entity(uuid=uuid, entity_type=e["entity_type"], fields=e["fields"])
            for uuid, e in blob.get("entities", {}).items()
        }
        return entities, blob.get("head_index", 0)

    def save(self, entities: dict[str, Entity], head_index: int) -> None:
        blob = {
            "replay_version": self.REPLAY_VERSION,
            "head_index": head_index,
            "entities": {
                uuid: {"entity_type": e.entity_type, "fields": e.fields}
                for uuid, e in entities.items()
            },
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob))
        tmp.replace(self._path)

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()
