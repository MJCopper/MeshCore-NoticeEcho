"""Database-independent contention evidence and safe failure reporting."""
from collections import deque
from contextlib import contextmanager
import logging
from pathlib import Path
import sqlite3
import threading
import time
from functools import wraps

logger = logging.getLogger('wx_echo.database')
BUSY_MILLISECONDS = 500


def locked(exc):
    code = getattr(exc, 'sqlite_errorcode', 0) or 0
    return isinstance(exc, sqlite3.Error) and (code & 255 in (5, 6) or 'locked' in str(exc).lower())


def report_error(db, source, exc):
    """A failed diagnostic write must never replace the original failure."""
    logger.error('%s: %s', source, exc)
    if locked(exc):
        return  # The transaction already captured evidence; do not wait twice.
    try:
        db.add_error(source, str(exc))
    except Exception:
        logger.exception('Could not persist %s error; original failure retained in process log', source)


def defer_locked(function):
    """Retain only delivery bookkeeping; never enqueue a network/radio operation."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        try:
            return function(self, *args, **kwargs)
        except sqlite3.Error as exc:
            if not locked(exc):
                raise
            with self._deferred_lock:
                self._deferred.append((function, args, kwargs))
            logger.warning('Delivery bookkeeping deferred: %s', function.__name__)
            return None
    return wrapped


class Contention:
    def __init__(self):
        self.lock = threading.Lock()
        self.failures = 0
        self.last_recovery = None
        self.pending = False
        self.recent = deque(maxlen=20)
        self.active = None

    @contextmanager
    def operation(self, name):
        started = time.monotonic()
        with self.lock:
            self.active = dict(operation=name, started_at=time.time())
        try:
            yield
        except BaseException as exc:
            if isinstance(exc, sqlite3.Error):
                entry = dict(operation=name, elapsed_seconds=round(time.monotonic()-started, 4),
                             checked_at=time.time(), code=getattr(exc, 'sqlite_errorcode', None),
                             name=getattr(exc, 'sqlite_errorname', type(exc).__name__), locked=locked(exc))
                with self.lock:
                    self.recent.append(entry)
                    if entry['locked']:
                        self.failures += 1
                        self.pending = True
                logger.warning('Database operation failed: %s', entry)
            raise
        else:
            with self.lock:
                if self.pending:
                    self.last_recovery = time.time()
                    self.pending = False
        finally:
            with self.lock:
                self.active = None

    def snapshot(self):
        with self.lock:
            return dict(lock_failures=self.failures, recovering=self.pending,
                        last_recovery=self.last_recovery, recent=list(self.recent), active=self.active)


def filesystem(path):
    """Best-effort Linux mount classification; never infer the remote deployment."""
    if path == ':memory:':
        return 'memory'
    try:
        target = Path(path).resolve()
        matches = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            left, right = line.split(' - ', 1)
            mount = Path(left.split()[4].replace('\\040', ' '))
            if target == mount or mount in target.parents:
                matches.append((len(str(mount)), right.split()[0]))
        return max(matches)[1] if matches else 'unknown'
    except (OSError, ValueError, IndexError):
        return 'unknown'
