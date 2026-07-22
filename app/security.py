from __future__ import annotations

import ipaddress
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit


@dataclass
class SessionRecord:
    token: str
    created_at: float
    last_seen_at: float
    connections: int = 0


class SessionCapacityError(RuntimeError):
    pass


class SessionStore:
    def __init__(
        self,
        idle_ttl: int,
        absolute_ttl: int,
        max_sessions: int,
        max_connections: int,
        clock=time.time,
        on_remove: Callable[[str], None] | None = None,
    ) -> None:
        self.idle_ttl = idle_ttl
        self.absolute_ttl = absolute_ttl
        self.max_sessions = max_sessions
        self.max_connections = max_connections
        self.clock = clock
        self.on_remove = on_remove
        self._records: dict[str, SessionRecord] = {}

    def _remove(self, token: str) -> None:
        if self._records.pop(token, None) is not None and self.on_remove is not None:
            self.on_remove(token)

    def prune(self) -> None:
        now = self.clock()
        expired = [token for token, record in self._records.items() if self._expired(record, now)]
        for token in expired:
            self._remove(token)

    def _expired(self, record: SessionRecord, now: float) -> bool:
        return (
            now - record.last_seen_at > self.idle_ttl
            or now - record.created_at > self.absolute_ttl
        )

    def create(self) -> str:
        self.prune()
        while len(self._records) >= self.max_sessions:
            inactive = [record for record in self._records.values() if record.connections == 0]
            if not inactive:
                raise SessionCapacityError("all session slots are active")
            oldest = min(inactive, key=lambda item: item.last_seen_at)
            self._remove(oldest.token)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self._records[token] = SessionRecord(token, now, now)
        return token

    def get(self, token: str | None, touch: bool = True) -> SessionRecord | None:
        if not token:
            return None
        record = self._records.get(token)
        if record is None:
            return None
        now = self.clock()
        if self._expired(record, now):
            self._remove(token)
            return None
        if touch:
            record.last_seen_at = now
        return record

    def revoke(self, token: str | None) -> None:
        if token:
            self._remove(token)

    def acquire_connection(self, token: str) -> bool:
        record = self.get(token)
        if record is None or record.connections >= self.max_connections:
            return False
        record.connections += 1
        return True

    def release_connection(self, token: str) -> None:
        record = self.get(token, touch=False)
        if record is not None:
            record.connections = max(0, record.connections - 1)


class LoginLimiter:
    def __init__(
        self,
        attempts: int,
        window: int,
        lockout: int,
        clock=time.monotonic,
        max_identities: int = 4096,
    ) -> None:
        self.attempts = attempts
        self.window = window
        self.lockout = lockout
        self.clock = clock
        self.max_identities = max(64, max_identities)
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    def _cleanup(self, now: float) -> None:
        for identity, failures in list(self._failures.items()):
            while failures and now - failures[0] > self.window:
                failures.popleft()
            if not failures:
                self._failures.pop(identity, None)
        for identity, until in list(self._blocked_until.items()):
            if until <= now:
                self._blocked_until.pop(identity, None)
        identities = set(self._failures) | set(self._blocked_until)
        if len(identities) <= self.max_identities:
            return
        ranked = sorted(
            identities,
            key=lambda item: max(
                self._blocked_until.get(item, 0),
                self._failures.get(item, deque([0]))[-1] if self._failures.get(item) else 0,
            ),
        )
        for identity in ranked[: len(identities) - self.max_identities]:
            self._failures.pop(identity, None)
            self._blocked_until.pop(identity, None)

    def retry_after(self, identity: str) -> int:
        now = self.clock()
        self._cleanup(now)
        until = self._blocked_until.get(identity, 0)
        if until <= now:
            self._blocked_until.pop(identity, None)
            return 0
        return max(1, int(until - now) + 1)

    def failure(self, identity: str) -> int:
        now = self.clock()
        self._cleanup(now)
        if identity not in self._failures and identity not in self._blocked_until:
            while len(set(self._failures) | set(self._blocked_until)) >= self.max_identities:
                oldest = min(
                    set(self._failures) | set(self._blocked_until),
                    key=lambda item: self._failures[item][-1] if self._failures.get(item) else self._blocked_until[item],
                )
                self._failures.pop(oldest, None)
                self._blocked_until.pop(oldest, None)
        failures = self._failures[identity]
        while failures and now - failures[0] > self.window:
            failures.popleft()
        failures.append(now)
        if len(failures) >= self.attempts:
            self._blocked_until[identity] = now + self.lockout
            failures.clear()
        return self.retry_after(identity)

    def success(self, identity: str) -> None:
        self._failures.pop(identity, None)
        self._blocked_until.pop(identity, None)


def is_loopback_address(value: str | None) -> bool:
    if not value:
        return False
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def forwarded_client_ip(
    peer: str | None,
    cf_connecting_ip: str | None,
    x_forwarded_for: str | None,
) -> str:
    if not is_loopback_address(peer):
        return peer or "unknown"
    candidate = cf_connecting_ip or (x_forwarded_for.split(",", 1)[0] if x_forwarded_for else None)
    if candidate:
        try:
            return str(ipaddress.ip_address(candidate.strip().split("%", 1)[0]))
        except ValueError:
            pass
    return peer or "unknown"


def host_allowed(host: str, allowed: list[str]) -> bool:
    try:
        parsed = urlsplit(f"//{host}")
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if not hostname or parsed.username or parsed.password:
        return False
    candidate = hostname.lower().rstrip(".")
    configured = {str(item).lower().rstrip(".") for item in allowed}
    if candidate in configured or host.lower() in configured:
        return True
    if candidate == "localhost" or candidate.endswith(".localhost"):
        return True
    if candidate.endswith(".trycloudflare.com"):
        return True
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def effective_scheme(request_scheme: str, forwarded_proto: str | None, peer: str | None) -> str:
    if is_loopback_address(peer) and forwarded_proto:
        candidate = forwarded_proto.split(",", 1)[0].strip().lower()
        if candidate in {"http", "https"}:
            return candidate
    return request_scheme.lower()


def normalize_origin(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def origin_matches(origin: str | None, scheme: str, host: str, allowed: list[str]) -> bool:
    normalized = normalize_origin(origin or "")
    if normalized is None:
        return False
    expected = f"{scheme.lower()}://{host.lower()}"
    permitted = {normalize_origin(item) for item in allowed}
    permitted.discard(None)
    return normalized == expected or normalized in permitted