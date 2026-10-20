"""Identified, robots-compatible OnTheMarket access and shared refusal backoff.

The allowlist reflects robots.txt verified on 2026-10-19. It deliberately admits
only plain area pages, their page-number pagination and listing details. Search
facets are applied locally. No proxy, browser impersonation or challenge solving
is used. Access state is shared by searches/details and by worker processes.
"""
from __future__ import annotations

import math
import fcntl
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

DEFAULT_USER_AGENT = "RentCompassBot/1.0"
MIN_INTERVAL_SECONDS = 1.2
DEFAULT_BLOCK_SECONDS = 3600
DEFAULT_RATE_LIMIT_SECONDS = 300
ROBOTS_VERIFIED_AT = "2026-10-19T00:00:00+00:00"
ROBOTS_POLICY_TTL_SECONDS = 7 * 24 * 3600
_ROOT = Path(__file__).resolve().parents[3]


class OTMSourceError(RuntimeError):
    """Safe, serializable upstream failure; never contains response bodies/URLs."""

    def __init__(self, source_status: str, reason: str, *, status_code=None,
                 retry_after_seconds=None, partial_rows=None):
        self.source_status = source_status
        self.reason = reason
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.partial_rows = list(partial_rows or [])
        super().__init__(f"OnTheMarket {source_status}: {reason}")

    def as_dict(self) -> dict:
        result = {"reason": self.reason}
        if self.status_code is not None:
            result["status_code"] = int(self.status_code)
        if self.retry_after_seconds is not None:
            result["retry_after_seconds"] = max(0, int(math.ceil(self.retry_after_seconds)))
        return result


def user_agent() -> str:
    value = os.getenv("OTM_USER_AGENT", DEFAULT_USER_AGENT).strip()
    # One stable programme identity, never an accidental browser disguise.
    if (not value or len(value) > 256 or "\r" in value or "\n" in value
            or re.search(r"mozilla|chrome/|safari/|firefox/", value, re.I)):
        raise OTMSourceError("unavailable", "invalid_program_user_agent")
    return value


def validate_url(url: str, params=None) -> None:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "www.onthemarket.com"
            or parts.fragment or parts.username or parts.password):
        raise OTMSourceError("unavailable", "url_not_allowed")
    area = bool(re.fullmatch(r"/to-rent/property/[a-z0-9]+(?:-[a-z0-9]+)*/", parts.path))
    detail = bool(re.fullmatch(r"/details/[a-zA-Z0-9_-]+/", parts.path))
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    pairs.extend((params or {}).items())
    if not (area or detail) or (detail and pairs):
        raise OTMSourceError("unavailable", "robots_path_not_allowed")
    if (len(pairs) > 1 or any(key != "page-number" or not re.fullmatch(r"[1-9][0-9]*", str(value))
                              for key, value in pairs)):
        raise OTMSourceError("unavailable", "robots_query_not_allowed")


def _state_path() -> Path:
    return Path(os.getenv("OTM_ACCESS_STATE_PATH", str(_ROOT / ".runtime" / "otm_access.sqlite3")))


@contextmanager
def _connect():
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS source_access (id INTEGER PRIMARY KEY CHECK(id=1), "
                   "next_request REAL NOT NULL DEFAULT 0, blocked_until REAL NOT NULL DEFAULT 0, "
                   "source_status TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', status_code INTEGER)")
        db.execute("INSERT OR IGNORE INTO source_access(id) VALUES (1)")
        db.execute("CREATE TABLE IF NOT EXISTS robots_cache "
                   "(id INTEGER PRIMARY KEY CHECK(id=1), verified REAL NOT NULL, body TEXT NOT NULL)")
        db.commit()
        with db:
            yield db
    finally:
        db.close()


def wait_for_slot(*, budget_s=None) -> None:
    deadline = None if budget_s is None else time.monotonic() + max(0, float(budget_s))
    while True:
        try:
            with _connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT next_request, blocked_until, source_status, reason, status_code "
                                 "FROM source_access WHERE id=1").fetchone()
                now = time.time()
                if row[1] > now:
                    raise OTMSourceError(row[2] or "blocked", row[3] or "source_cooldown",
                                         status_code=row[4], retry_after_seconds=row[1] - now)
                delay = max(0, row[0] - now)
                if delay == 0:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise OTMSourceError("unavailable", "request_budget_exhausted")
                    db.execute("UPDATE source_access SET next_request=? WHERE id=1",
                               (now + MIN_INTERVAL_SECONDS,))
                    return
        except (OSError, sqlite3.Error) as exc:
            raise OTMSourceError("unavailable", "access_state_unavailable") from exc
        if deadline is not None and delay >= deadline - time.monotonic():
            raise OTMSourceError("unavailable", "request_budget_exhausted")
        time.sleep(delay)


def _retry_after(value, default: int) -> int:
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return max(1, int(math.ceil(seconds)))
    except (TypeError, ValueError):
        try:
            dt = parsedate_to_datetime(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(1, int(math.ceil((dt - datetime.now(timezone.utc)).total_seconds())))
        except (TypeError, ValueError, OverflowError):
            pass
    return default


def _pause(error: OTMSourceError) -> None:
    try:
        with _connect() as db:
            db.execute("UPDATE source_access SET blocked_until=MAX(blocked_until, ?), "
                       "source_status=?, reason=?, status_code=? WHERE id=1",
                       (time.time() + error.retry_after_seconds, error.source_status,
                        error.reason, error.status_code))
    except (OSError, sqlite3.Error) as exc:
        raise OTMSourceError("unavailable", "access_state_unavailable") from exc


def _request(session, url: str, *, params=None, timeout=25):
    start = time.monotonic()
    wait_for_slot(budget_s=timeout)
    remaining = float(timeout) - (time.monotonic() - start)
    if remaining <= 0:
        raise OTMSourceError("unavailable", "request_budget_exhausted")
    try:
        response = session.get(url, params=params or {}, timeout=remaining, allow_redirects=False)
    except requests.RequestException as exc:
        raise OTMSourceError("unavailable", "transport_error") from exc
    code = int(getattr(response, "status_code", 200))
    headers = getattr(response, "headers", {}) or {}
    if code in (401, 403, 429):
        limited = code == 429
        error = OTMSourceError("rate_limited" if limited else "blocked",
                               "http_rate_limited" if limited else "http_access_denied",
                               status_code=code, retry_after_seconds=_retry_after(
                                   headers.get("Retry-After"), DEFAULT_RATE_LIMIT_SECONDS if limited else DEFAULT_BLOCK_SECONDS))
        _pause(error)
        raise error
    if code != 200:
        raise OTMSourceError("unavailable", "http_redirect" if 300 <= code < 400 else "http_error",
                             status_code=code)
    # A 200 challenge is an access denial, not a schema change or empty result.
    text = str(getattr(response, "text", ""))
    if not re.search(r"\bid=[\"']__NEXT_DATA__[\"']", text, re.I) and re.search(
            r"verify (?:that )?you are human|checking your browser|cf-chl-|captcha|access denied|just a moment", text, re.I):
        error = OTMSourceError("blocked", "challenge_response", status_code=code,
                               retry_after_seconds=DEFAULT_BLOCK_SECONDS)
        _pause(error)
        raise error
    return response


def _robot_groups(body: str):
    groups, agents, rules = [], [], []
    has_directive = False
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if has_directive:
                groups.append((agents, rules))
                agents, rules, has_directive = [], [], False
            if value:
                agents.append(value.lower())
        elif agents:
            has_directive = True
            if key in ("allow", "disallow") and value:
                rules.append((key, value))
    if agents:
        groups.append((agents, rules))
    return groups


def _robots_allow(body: str, target: str) -> bool:
    groups = _robot_groups(body)
    if not groups:
        raise OTMSourceError("unavailable", "robots_invalid_response")
    agent = user_agent().split("/", 1)[0].lower()
    selected, best_agent = [], -1
    for agents, rules in groups:
        score = max((0 if item == "*" else len(item) for item in agents
                     if item == "*" or item in agent), default=-1)
        if score < 0:
            continue
        if score > best_agent:
            selected, best_agent = list(rules), score
        elif score == best_agent:
            selected.extend(rules)
    best_rule, allowed = -1, True
    for kind, pattern in selected:
        end_anchor = pattern.endswith("$")
        expression = re.escape(pattern[:-1] if end_anchor else pattern).replace(r"\*", ".*")
        if re.match(expression + ("$" if end_anchor else ""), target):
            specificity = len(pattern.replace("*", "").rstrip("$"))
            if specificity > best_rule or (specificity == best_rule and kind == "allow"):
                best_rule, allowed = specificity, kind == "allow"
    return allowed


def _cached_robots():
    with _connect() as db:
        row = db.execute("SELECT verified, body FROM robots_cache WHERE id=1").fetchone()
    now = time.time()
    if row and 0 <= now - row[0] <= ROBOTS_POLICY_TTL_SECONDS:
        return row[1]
    verified = datetime.fromisoformat(ROBOTS_VERIFIED_AT).timestamp()
    if 0 <= now - verified <= ROBOTS_POLICY_TTL_SECONDS:
        # None means the dated, built-in narrow allowlist is still current.
        return None
    return False


def _ensure_robots(session, *, timeout):
    try:
        cached = _cached_robots()
        if cached is not False:
            return cached
        start = time.monotonic()
        path = _state_path().with_suffix(".robots.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() - start >= timeout:
                        raise OTMSourceError("unavailable", "robots_verification_in_progress")
                    time.sleep(min(0.05, timeout))
            try:
                cached = _cached_robots()
                if cached is not False:
                    return cached
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    raise OTMSourceError("unavailable", "request_budget_exhausted")
                response = _request(session, "https://www.onthemarket.com/robots.txt", timeout=remaining)
                body = str(response.text)
                # HTML and malformed responses cannot grant crawler permission.
                if len(body) > 512_000 or "<html" in body.lower() or not _robot_groups(body):
                    raise OTMSourceError("unavailable", "robots_invalid_response")
                with _connect() as db:
                    db.execute("INSERT INTO robots_cache(id, verified, body) VALUES (1, ?, ?) "
                               "ON CONFLICT(id) DO UPDATE SET verified=excluded.verified, body=excluded.body",
                               (time.time(), body))
                return body
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except OTMSourceError as exc:
        if exc.retry_after_seconds is None and exc.reason not in {
                "robots_verification_in_progress", "request_budget_exhausted"}:
            # A broken robots endpoint is not retried once for every user/area.
            exc.retry_after_seconds = 60
            _pause(exc)
        raise
    except (OSError, sqlite3.Error) as exc:
        raise OTMSourceError("unavailable", "access_state_unavailable") from exc


def get(session, url: str, *, params=None, timeout=25):
    """One permitted GET; robots refresh is shared, cached and fail-closed."""
    validate_url(url, params)
    start = time.monotonic()
    body = _ensure_robots(session, timeout=float(timeout))
    parsed = urlsplit(url)
    query = parsed.query or urlencode(params or {})
    target = parsed.path + ("?" + query if query else "")
    if body is not None and not _robots_allow(body, target):
        raise OTMSourceError("blocked", "robots_disallowed")
    remaining = float(timeout) - (time.monotonic() - start)
    if remaining <= 0:
        raise OTMSourceError("unavailable", "request_budget_exhausted")
    return _request(session, url, params=params, timeout=remaining)
