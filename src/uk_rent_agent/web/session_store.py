from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable


def default_persistent_state() -> dict:
    return {
        "user_preferences": {
            "hard_preferences": [], "soft_preferences": [], "excluded_areas": [],
            "required_amenities": [], "safety_concerns": [],
        },
        "accumulated_search_criteria": {
            "destination": None, "max_budget": None, "max_travel_time": None,
            "property_features": [], "soft_preferences": [], "amenities_of_interest": [],
        },
        "extracted_context": {},
    }


@dataclass
class UserSession:
    persistent_state: dict = field(default_factory=default_persistent_state)
    history: list = field(default_factory=list)
    last_results: list = field(default_factory=list)
    # `favorites` is legacy: favorites now persist to the sqlite ConversationStore
    # (per-USER, not per-conversation). Kept as an unused slot for backward compat.
    favorites: dict = field(default_factory=dict)
    # True once this hot-cache slice has been rehydrated from sqlite on a cache miss.
    rehydrated: bool = False
    touched_at: float = field(default_factory=time.monotonic)


class SessionStore:
    """In-memory hot cache of per-(user_id, conversation_id) conversational state.

    LRU (max_users) + TTL eviction applies to this cache only; the durable copy lives
    in the sqlite ConversationStore and is rehydrated on a miss. Backward compatible with
    the old single-axis API: ``get(user_id)`` / ``clear(user_id)`` still work (they use a
    default conversation slice), so the legacy tests keep passing.
    """

    def __init__(
        self,
        max_users: int = 10_000,
        ttl_seconds: int = 7 * 24 * 3600,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_users < 1 or ttl_seconds < 1:
            raise ValueError("max_users and ttl_seconds must be positive")
        self._max = max_users
        self._ttl = ttl_seconds
        self._clock = clock
        self._data: OrderedDict[tuple[str, str], UserSession] = OrderedDict()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(user_id, conversation_id="default") -> tuple[str, str]:
        return (str(user_id or "default"), str(conversation_id or "default"))

    def get(self, user_id, conversation_id="default") -> UserSession:
        key = self._key(user_id, conversation_id)
        now = self._clock()
        with self._lock:
            self._expire(now)
            value = self._data.pop(key, None)
            if value is None:
                value = UserSession()
            value.touched_at = now
            self._data[key] = value
            # Evict cold cache slices only. The per-key turn locks are intentionally
            # NOT evicted here so an in-flight turn's lock is never silently replaced.
            while len(self._data) > self._max:
                self._data.popitem(last=False)
            return value

    def turn_lock(self, user_id, conversation_id="default") -> threading.Lock:
        """Return a stable per-(user_id, conversation_id) lock used to make a turn's
        read-modify-write of the conversational state atomic across concurrent requests."""
        key = self._key(user_id, conversation_id)
        with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def clear(self, user_id, conversation_id=None) -> None:
        """Drop one conversation slice, or (conversation_id=None) all of a user's slices."""
        if conversation_id is None:
            self.clear_user(user_id)
            return
        key = self._key(user_id, conversation_id)
        with self._lock:
            self._data.pop(key, None)
            self._locks.pop(key, None)

    def clear_user(self, user_id) -> None:
        uid = str(user_id or "default")
        with self._lock:
            for key in [k for k in self._data if k[0] == uid]:
                self._data.pop(key, None)
            for key in [k for k in self._locks if k[0] == uid]:
                self._locks.pop(key, None)

    def _expire(self, now: float) -> None:
        expired = [key for key, value in self._data.items() if now - value.touched_at >= self._ttl]
        for key in expired:
            self._data.pop(key, None)

    def snapshot(self, user_id, conversation_id="default") -> UserSession:
        with self._lock:
            return copy.deepcopy(self.get(user_id, conversation_id))
