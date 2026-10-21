"""Optional update check against GitHub releases (metadata only, never auto-installs)."""
from __future__ import annotations

import http.client
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("shds.updater")

from . import __version__ as APP_VERSION  # single source of truth (scanner/__init__.py)
DEFAULT_RELEASES_URL = ("https://api.github.com/repos/your-github-user/"
                        "smart-hardware-diagnostic-scanner/releases/latest")  # placeholder; documented in README

USER_AGENT = f"SmartHardwareDiagnosticScanner/{APP_VERSION}"


@dataclass
class UpdateCheck:
    """Result of one release check; failures degrade to status='unavailable'."""

    status: str = "unavailable"   # "up_to_date" | "update_available" | "unavailable"
    latest: str = ""              # latest version string ("1.2.0")
    current: str = APP_VERSION
    detail: str = ""              # human explanation (error reason etc.)


def _unavailable(detail: str) -> UpdateCheck:
    """Build the failure result; checking never raises and never touches files."""
    return UpdateCheck(status="unavailable", latest="", current=APP_VERSION, detail=detail)


def parse_tag(tag: str | None) -> str:
    """Strip a leading "v"/"V" and whitespace; non-string input yields ""."""
    if not isinstance(tag, str):
        return ""
    text = tag.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text


def compare_versions(a: str, b: str) -> int:
    """Compare dotted numeric versions: -1 (a<b), 0 (equal or undecidable), 1 (a>b).

    Missing components are padded with 0 ("1.2" == "1.2.0"). Any non-numeric
    component makes the outcome undecidable, so 0 is returned.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return 0
    parts_a = a.strip().split(".")
    parts_b = b.strip().split(".")
    width = max(len(parts_a), len(parts_b))
    parts_a += ["0"] * (width - len(parts_a))
    parts_b += ["0"] * (width - len(parts_b))
    result = 0
    for text_a, text_b in zip(parts_a, parts_b):
        try:
            num_a = int(text_a.strip())
            num_b = int(text_b.strip())
        except ValueError:
            return 0  # non-numeric component: cannot decide, treat as equal
        if result == 0 and num_a != num_b:
            result = -1 if num_a < num_b else 1
    return result


def check_for_update(url: str | None = None, timeout: float = 4.0) -> UpdateCheck:
    """Check the latest GitHub release. Metadata only: never raises, never installs.

    Any failure (offline, HTTP error, timeout, bad JSON, missing tag) degrades to
    status="unavailable" with a human explanation in `detail`.
    """
    target = (url or "").strip() or DEFAULT_RELEASES_URL
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        # A malformed URL must degrade like any other failure, not raise out
        # of a function that promises it never does.
        return _unavailable(f"unsupported update URL: {target!r}")
    try:
        request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read(1_000_000)
    except urllib.error.HTTPError as exc:
        log.info("update check: HTTP %s from GitHub", exc.code)
        return _unavailable(f"GitHub returned HTTP {exc.code} ({exc.reason}).")
    except urllib.error.URLError as exc:
        log.info("update check: URLError %s", exc.reason)
        return _unavailable(f"no internet connection or GitHub unreachable (URLError: {exc.reason}).")
    except socket.timeout as exc:  # alias of TimeoutError since Python 3.10
        log.info("update check timed out after %ss", timeout)
        return _unavailable(f"update check timed out after {timeout}s ({exc}).")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # ValueError covers a bad URL/port, http.client.HTTPException covers
        # InvalidURL and friends — none of them are OSError subclasses.
        log.info("update check: request failed %s", exc)
        return _unavailable(f"update check failed: {exc}.")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are ValueError
        return _unavailable(f"could not parse GitHub response as JSON ({exc}).")
    if not isinstance(payload, dict):
        return _unavailable("GitHub response was not a JSON object.")
    latest = parse_tag(payload.get("tag_name"))
    if not latest:
        return _unavailable("GitHub response contained no usable tag_name.")
    result = UpdateCheck(latest=latest)
    order = compare_versions(APP_VERSION, latest)
    if order < 0:
        result.status = "update_available"
        result.detail = f"update available: {latest} (current {APP_VERSION})."
        return result
    result.status = "up_to_date"
    if order > 0:
        result.detail = "local version is newer (pre-release?)"
    else:
        result.detail = f"running the latest release ({APP_VERSION})."
    return result
