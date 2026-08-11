from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

from uk_rent_agent.web.conversation_store import ConversationStore


logger = logging.getLogger(__name__)


class OutboxWorker:
    """Supervised in-process consumer for the durable conversation outbox.

    The thread itself may die with the process; the work cannot. Claims are leased in
    SQLite and are reclaimed after expiry. ``start`` is idempotent and restarts a dead
    worker, while ``wake`` avoids polling latency after a request commits new jobs.
    """

    def __init__(
        self,
        store: ConversationStore,
        process: Callable[[dict, str], None],
        *,
        poll_seconds: float = 2.0,
        lease_seconds: int = 5 * 60,
        max_attempts: int = 5,
    ):
        self.store = store
        self.process = process
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.lease_seconds = max(1, int(lease_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.worker_id = f"outbox-{uuid.uuid4().hex}"
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> "OutboxWorker":
        with self._lock:
            if self.alive:
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="rentcompass-outbox",
                daemon=True,
            )
            self._thread.start()
        return self

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def run_once(self) -> bool:
        job = self.store.claim_background_job(
            self.worker_id, lease_seconds=self.lease_seconds
        )
        if job is None:
            return False
        try:
            self.process(job, self.worker_id)
            self.store.complete_background_job(job["id"], self.worker_id)
        except Exception as exc:  # the durable row owns retry/error state
            status = self.store.retry_background_job(
                job["id"], self.worker_id, f"{type(exc).__name__}: {exc}",
                max_attempts=self.max_attempts,
            )
            logger.error(
                "background_job.failed",
                extra={
                    "job_id": job.get("id"),
                    "job_kind": job.get("kind"),
                    "attempt": job.get("attempts"),
                    "status": status,
                    "error_type": type(exc).__name__,
                },
            )
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.run_once():
                    continue
            except Exception as exc:  # keep the supervisor alive on store-level faults
                logger.exception(
                    "background_worker.loop_failed",
                    extra={"error_type": type(exc).__name__},
                )
            self._wake.wait(self.poll_seconds)
            self._wake.clear()
