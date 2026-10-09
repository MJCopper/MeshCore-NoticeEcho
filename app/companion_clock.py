"""Verified UTC companion clock operations and monotonic scheduling."""
import asyncio
from datetime import datetime, timezone
import math
import time


def stamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat(timespec='seconds')


class ClockSync:
    READ_TIMEOUT = 2.0
    WRITE_TIMEOUT = 2.0
    def __init__(self, db):
        self.db = db
        saved = db.get_setting('meshcore_clock_state', {}) or {}
        saved = saved if isinstance(saved, dict) else {}
        self.state = dict(saved, status='stale' if saved else 'not checked', error='')
        self.pending = True
        self.generation = 0
        self.due = time.monotonic()
        self.failures = 0
        self.anchor = (time.time(), time.monotonic())

    def policy(self):
        def number(key, default, low, high):
            try:
                value = float(self.db.get_setting(key, default))
                return min(high, max(low, value)) if math.isfinite(value) else default
            except (TypeError, ValueError):
                return default
        return dict(enabled=bool(self.db.get_setting('meshcore_clock_auto_sync', True)),
                    interval=number('meshcore_clock_interval_minutes', 60, 1, 1440) * 60,
                    tolerance=number('meshcore_clock_tolerance_seconds', 5, 1, 300))

    def request(self, reason='connection changed'):
        self.generation += 1
        self.pending = True
        self.due = time.monotonic()
        self.state.update(status='stale', trigger=reason)

    def ready(self):
        wall, mono = time.time(), time.monotonic()
        old_wall, old_mono = self.anchor
        if abs((wall-old_wall) - (mono-old_mono)) > 2:
            self.request('server clock adjusted')
        self.anchor = wall, mono
        return self.policy()['enabled'] and (self.pending or mono >= self.due)

    def status(self, connected):
        result = dict(self.state)
        result['automatic'] = self.policy()['enabled']
        result['server_time'] = stamp()
        if not connected:
            result['status'] = 'offline'
        elif not self.policy()['enabled']:
            result['automatic_status'] = 'disabled'
        elif self.pending or time.monotonic() >= self.due:
            result['status'] = 'overdue' if result.get('last_check') else result.get('status', 'not checked')
        result['next_check'] = stamp(time.time()+max(0, self.due-time.monotonic())) if result['automatic'] and connected else ''
        return result

    async def read(self, radio):
        from meshcore import EventType
        commands = radio._mc.commands
        method = getattr(commands, 'get_time', None)
        if not callable(method):
            raise RuntimeError('unsupported: SDK cannot read companion time')
        wall, mono = time.time(), time.monotonic()
        response = await asyncio.wait_for(method(), self.READ_TIMEOUT)
        end_wall, end_mono = time.time(), time.monotonic()
        if response is None:
            raise RuntimeError('Companion clock returned no response')
        if response.type == EventType.ERROR:
            code = (response.payload or {}).get('error_code')
            raise RuntimeError('unsupported: companion clock read' if code == 1 else f'Companion clock read rejected (code {code})')
        value = (response.payload or {}).get('time')
        if response.type != EventType.CURRENT_TIME or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xffffffff:
            raise RuntimeError('Malformed companion clock response')
        duration = end_mono-mono
        return dict(companion_epoch=value, companion_time=stamp(value), drift_seconds=value-(wall+end_wall)/2,
                    uncertainty_seconds=duration/2+1.0, request_seconds=duration, midpoint_mono=(mono+end_mono)/2,
                    measured_at=stamp(end_wall), reliable=duration <= 1.5 and abs((end_wall-wall)-duration) <= 0.5)

    async def check(self, radio, force=False, target=None, read_only=False):
        """Call only while holding the manager's connection lock."""
        from meshcore import EventType
        generation = self.generation
        prior_status = self.state.get('status')
        policy = self.policy()
        synced = False
        try:
            sample = await self.read(radio)
            self.state.update(sample)
            self.state['last_check'] = stamp()
            if not sample['reliable'] and target is None:
                raise RuntimeError('inconclusive: clock measurement too slow or server time changed')
            outside = abs(sample['drift_seconds']) > policy['tolerance'] + sample['uncertainty_seconds']
            if not read_only and (force or target is not None or outside):
                method = getattr(radio._mc.commands, 'set_time', None)
                if not callable(method):
                    raise RuntimeError('unsupported: SDK cannot set companion time')
                requested = int(time.time()) if target is None else target
                if isinstance(requested, bool) or not isinstance(requested, int) or not 0 <= requested <= 0xffffffff:
                    raise ValueError('Time must be a Unix timestamp between 0 and 4294967295')
                written_at = time.monotonic()
                interrupted = False
                try:
                    response = await asyncio.wait_for(method(requested), self.WRITE_TIMEOUT)
                except (TimeoutError, OSError):
                    response, interrupted = None, True
                if response is not None and response.type == EventType.ERROR:
                    code = (response.payload or {}).get('error_code')
                    if code != 6:
                        raise RuntimeError('unsupported: companion clock write' if code == 1 else f'Companion clock write rejected (code {code})')
                elif response is not None and response.type != EventType.OK:
                    raise RuntimeError('Unexpected companion clock write response')
                verified = await self.read(radio)
                self.state.update(verified)
                self.state['last_check'] = stamp()
                error = abs(verified['companion_epoch']-(requested+verified['midpoint_mono']-written_at))
                if not verified['reliable'] or error > policy['tolerance'] + verified['uncertainty_seconds'] or (target is None and abs(verified['drift_seconds']) > policy['tolerance'] + verified['uncertainty_seconds']):
                    raise RuntimeError('Clock write could not be verified; refresh before retrying')
                self.state.update(last_verified_write=stamp(), requested_epoch=requested,
                                  write_response='verified after interrupted/missing reply' if interrupted or response is None else 'verified')
                if target is None:
                    self.state['last_sync'] = stamp()
                synced = True
            self.state.update(status='verified explicit time' if synced and target is not None else 'verified sync' if synced else 'drift detected' if outside else 'within tolerance', error='')
            self.failures = 0
            delay = 0 if read_only and outside and policy['enabled'] else min(60, policy['interval']) if target is not None else policy['interval']
            self.due = time.monotonic() + delay
        except ValueError:
            raise
        except (RuntimeError, TimeoutError, OSError, AttributeError) as exc:
            error = str(exc) or 'Companion clock operation timed out'
            self.failures += 1
            self.state.update(status='unsupported' if error.startswith('unsupported:') else 'inconclusive' if error.startswith('inconclusive:') else 'failed', error=error,
                              last_check=stamp(), consecutive_failures=self.failures)
            self.due = time.monotonic() + (policy['interval'] if self.state['status']=='unsupported' else min(policy['interval'], 30 * 2**min(self.failures-1, 5)))
        self.pending = generation != self.generation
        if self.pending:
            self.due = time.monotonic()
        self.state['consecutive_failures'] = self.failures
        self.db.set_setting('meshcore_clock_state', self.state)
        if prior_status != self.state['status'] and hasattr(self.db, 'add_event'):
            self.db.add_event('WARN' if self.failures else 'INFO', 'Companion clock: '+self.state['status'] + (' — '+self.state['error'] if self.state['error'] else ''))
        return dict(self.state)
