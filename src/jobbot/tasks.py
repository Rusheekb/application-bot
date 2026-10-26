"""Background work inside the web app: the scan scheduler, manual scans and the apply manager."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select

from jobbot.apply.manager import ApplyManager
from jobbot.config import Env
from jobbot.db import Database, get_settings
from jobbot.models import Company, ScanRun, SearchQuery, utcnow
from jobbot.pipeline import Scanner

log = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, db: Database, env: Env, apply: ApplyManager | None = None):
        self.db = db
        self.env = env
        self.apply = apply or ApplyManager(db, env)
        self.current_scan_id: int | None = None
        self.next_scan_at: datetime | None = None
        self._scan_task: asyncio.Task | None = None
        self._scheduler_task: asyncio.Task | None = None

    @property
    def scan_running(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    def recover_after_restart(self) -> None:
        with self.db.session() as s:
            for scan in s.scalars(select(ScanRun).where(ScanRun.status == "running")):
                scan.status = "error"
                scan.finished_at = scan.finished_at or utcnow()
                scan.progress = None
                scan.errors = [
                    *(scan.errors or []),
                    {"source": "scan", "error": "Interrupted because jobbot restarted"},
                ]
        self.apply.recover_after_restart()

    def start_scan(self, trigger: str = "manual") -> int:
        """Start a scan in the background, or return the id of the one already running."""
        if self.scan_running and self.current_scan_id is not None:
            return self.current_scan_id
        scanner = Scanner(self.db, self.env, on_auto_apply=self._auto_apply)
        scan_id = scanner.start(trigger)
        self.current_scan_id = scan_id
        self._scan_task = asyncio.create_task(scanner.run(trigger, scan_id))
        return scan_id

    async def _auto_apply(self, job_ids: list[int]) -> None:
        for job_id in job_ids:
            await self.apply.enqueue(job_id, trigger="auto")

    def start_scheduler(self) -> None:
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("Scheduler tick failed")
            await asyncio.sleep(30)

    def _tick(self) -> None:
        with self.db.session() as s:
            interval = get_settings(s).scan_interval_hours
            last = s.scalars(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(1)).first()
            last_start = last.started_at if last else None
            configured = s.scalar(select(func.count(Company.id)).where(Company.enabled)) or s.scalar(
                select(func.count(SearchQuery.id)).where(SearchQuery.enabled)
            )
        if not interval or interval <= 0 or not configured:
            self.next_scan_at = None
            return
        due_at = (last_start + timedelta(hours=interval)) if last_start else utcnow()
        self.next_scan_at = due_at
        if utcnow() >= due_at and not self.scan_running:
            log.info("Starting scheduled scan")
            self.start_scan("schedule")

    async def shutdown(self) -> None:
        if self._scheduler_task:
            self._scheduler_task.cancel()
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
        await self.apply.shutdown()
