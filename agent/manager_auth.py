"""Manager-only local credential. Remote access uses an authenticated SSH tunnel."""
from __future__ import annotations

import base64
import hmac
import os
from pathlib import Path
from urllib.parse import urlsplit

KEY_PATH = Path("secrets/manager-web.key")


def load_key(path: Path = KEY_PATH) -> str | None:
    try:
        if path.is_symlink() or path.parent.is_symlink():
            return None
        for target in (path, path.parent):
            st = target.stat()
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                return None
        key = path.read_text().strip()
        return key if 24 <= len(key) <= 256 and key.isascii() and not any(c.isspace() for c in key) else None
    except OSError:
        return None


def auth_header(key: str) -> str:
    return "Basic " + base64.b64encode(("manager:" + key).encode()).decode()


def authorized(header: str, key: str | None) -> bool:
    if not key or len(header) > 1024:
        return False
    return hmac.compare_digest(header.encode(), auth_header(key).encode())


def local_headers(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.port != 8765 or parsed.username or parsed.password):
        return {}
    key = load_key()
    return {"Authorization": auth_header(key)} if key else {}
