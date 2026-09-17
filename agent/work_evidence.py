"""Capture source references, not source access or proof of work. No network I/O."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


def source_references(text: str) -> list[dict[str, str]]:
    found = {}
    for raw in re.findall(r"https://[^\s<>|]+", text):
        try:
            parsed = urlsplit(raw.rstrip(".,);]"))
            if parsed.username or parsed.password or parsed.port not in {None, 443}:
                continue
        except ValueError:
            continue
        host, path = (parsed.hostname or "").lower(), parsed.path
        source = ""
        if host in {"drive.google.com", "docs.google.com"}:
            source = "drive"
        elif host == "github.com":
            source = "github"
        elif host == "cad.onshape.com":
            source = "onshape"
        elif host == "chatgpt.com":
            source = "chatgpt"
        if not source or not path.strip("/"):
            continue
        # Do not persist query tokens as integration credentials. Canonical path
        # is only a reference: Google folder ownership and source ACL are unknown.
        url = urlunsplit(("https", host, path, "", ""))
        found[url] = {"source": source, "url": url, "source_id": path,
                      "retrieval_status": "not_verified"}
        if len(found) >= 5:
            break
    return list(found.values())


COMPANY_HANDOFF = (
    "Use your Cislune ChatGPT/Codex workspace and company Drive, GitHub, OnShape or server folder. "
    "Save the useful result there, then share its link and what changed or what is blocked. "
    "For shop work or meetings, a concrete outcome is enough. "
    "Company membership does not make every private chat/file visible; check access. "
    "Do not paste credentials or personnel/payroll records into project updates."
)
