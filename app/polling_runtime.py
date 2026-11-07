"""Supervised source workers with bounded backoff and no radio replay logic."""
import asyncio
from datetime import datetime, timezone
import logging
import time

from .config import polling_seconds
from .database_runtime import report_error

logger = logging.getLogger('wx_echo.polling')
RETRY_DELAYS = (5, 15, 30, 60)


class PollWorker:
    def __init__(self, source, db, owner, wake):
        self.source, self.db, self.owner, self.wake = source, db, owner, wake
        self.task = None
        self.stopping = False
        self.state = 'not started'
        self.restarts = 0
        self.failures = 0
        self.last_error = ''
        self.last_recovery = None
        self.next_attempt = None
        self.recovered_queue = False

    def start(self, run):
        if self.task is not None and not self.task.done():
            return self.task
        self.stopping = False
        self.state = 'starting'
        self.task = asyncio.create_task(self._supervise(run), name=self.source+'-poller')
        return self.task

    async def _supervise(self, run):
        crashes = 0
        try:
            while not self.stopping:
                started = time.monotonic()
                try:
                    await run()
                    if self.stopping:
                        break
                    raise RuntimeError('Polling loop exited unexpectedly')
                except asyncio.CancelledError:
                    if self.stopping:
                        raise
                    self.last_error = 'Polling loop cancelled unexpectedly'
                except Exception as exc:
                    self.last_error = str(exc)
                    logger.exception('%s polling loop exited; restarting', self.source)
                self.restarts += 1
                crashes = 0 if time.monotonic()-started >= 300 else crashes
                delay = RETRY_DELAYS[min(crashes, len(RETRY_DELAYS)-1)]
                crashes += 1
                self.state = 'restarting'
                # A manual poke cannot turn repeated crashes into a tight loop.
                await asyncio.sleep(delay)
        finally:
            self.state = 'stopped'

    async def loop(self, poll, generation, recover=None, timeout=None):
        interval = 300
        while not self.stopping:
            current = generation()
            failed = False
            try:
                if hasattr(self.db, 'flush_deferred'):
                    self.db.flush_deferred()
                if not self.recovered_queue:
                    if recover is not None:
                        recover()
                    self.recovered_queue = True
                interval = polling_seconds(self.db.get_setting(self.source+'_poll_minutes', 5), 5)
                self.state = 'polling'
                if timeout is None:
                    await poll()
                else:
                    await asyncio.wait_for(poll(), timeout)
                if self.failures:
                    self.last_recovery = time.time()
                self.failures = 0
                self.last_error = ''
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                self.failures += 1
                self.last_error = str(exc) or type(exc).__name__
                stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                if self.source == 'bom':
                    self.owner.status.last_poll_time = stamp
                    self.owner.status.last_poll_result = 'error: '+self.last_error
                else:
                    self.owner.last_poll = stamp
                    self.owner.last_result = 'error: '+self.last_error
                report_error(self.db, self.source, exc)
            delay = RETRY_DELAYS[min(self.failures-1, len(RETRY_DELAYS)-1)] if failed else interval
            self.state = 'backoff' if failed else 'waiting'
            self.next_attempt = time.time()+delay
            self.owner.next_poll_at = datetime.fromtimestamp(self.next_attempt, timezone.utc).isoformat()
            if not failed and generation() != current:
                continue
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.state = 'stopped'

    def snapshot(self):
        alive = self.task is not None and not self.task.done() and not self.stopping
        return dict(source=self.source, state=self.state, alive=alive,
                    ready=alive and self.state not in ('backoff', 'restarting'),
                    restarts=self.restarts, consecutive_failures=self.failures,
                    last_error=self.last_error, last_recovery=self.last_recovery, next_attempt=self.next_attempt)
