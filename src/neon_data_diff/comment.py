"""Create or update one sticky pull-request comment (found by a hidden marker)."""
from __future__ import annotations

import json
import re
import urllib.request

from .render import MARKER

API = "https://api.github.com"


def _request(method: str, url: str, token: str, data: dict | None = None):
    req = urllib.request.Request(
        url,
        method=method,
        data=None if data is None else json.dumps(data).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"null")


def pr_number_from_ref(ref: str | None) -> int | None:
    m = re.fullmatch(r"refs/pull/(\d+)/(?:merge|head)", ref or "")
    return int(m.group(1)) if m else None


def upsert_comment(repo: str, pr: int, body: str, token: str, request=_request) -> str:
    """Update the existing marker comment if there is one, else create it. Returns 'updated' or 'created'."""
    page = 1
    while True:
        items = request("GET", f"{API}/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}", token)
        for c in items:
            if MARKER in (c.get("body") or ""):
                request("PATCH", f"{API}/repos/{repo}/issues/comments/{c['id']}", token, {"body": body})
                return "updated"
        if len(items) < 100:
            break
        page += 1
    request("POST", f"{API}/repos/{repo}/issues/{pr}/comments", token, {"body": body})
    return "created"
