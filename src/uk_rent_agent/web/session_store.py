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
    favorites: dict = field(default_factory=dict)
    search_history: list = field(default_factory=list)
    touched_at: float = field(default_factory=time.monotonic)


class SessionStore:
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
        self._data: OrderedDict[str, UserSession] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, user_id: str) -> UserSession:
        key = str(user_id or "default")
        now = self._clock()
        with self._lock:
            self._expire(now)
            value = self._data.pop(key, None)
            if value is None:
                value = UserSession()
            value.touched_at = now
            self._data[key] = value
            while len(self._data) > self._max:
                self._data.popitem(last=False)
            return value

    def clear(self, user_id: str) -> None:
        with self._lock:
            self._data.pop(str(user_id or "default"), None)

    def _expire(self, now: float) -> None:
        expired = [key for key, value in self._data.items() if now - value.touched_at >= self._ttl]
        for key in expired:
            self._data.pop(key, None)

    def snapshot(self, user_id: str) -> UserSession:
        with self._lock:
            return copy.deepcopy(self.get(user_id))
