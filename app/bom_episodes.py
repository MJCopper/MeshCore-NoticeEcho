"""Persistent BOM product episodes and mode/destination announcement identities."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit

from .catalogue import epoch

SCHEMA = """
CREATE TABLE IF NOT EXISTS bom_products (
 product_key TEXT PRIMARY KEY, prefix TEXT UNIQUE NOT NULL, next_sequence INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bom_product_aliases (
 provider_id TEXT PRIMARY KEY, product_key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bom_episodes (
 id INTEGER PRIMARY KEY, product_key TEXT NOT NULL, sequence INTEGER NOT NULL,
 reference TEXT UNIQUE NOT NULL, provider_id TEXT NOT NULL, opened_at TEXT NOT NULL,
 last_observed TEXT NOT NULL, expires_at REAL, state TEXT NOT NULL DEFAULT 'open',
 closed_at TEXT, closure_reason TEXT NOT NULL DEFAULT '', missing_polls INTEGER NOT NULL DEFAULT 0,
 snapshot_id TEXT NOT NULL DEFAULT '', UNIQUE(product_key,sequence)
);
CREATE INDEX IF NOT EXISTS idx_bom_episode_product ON bom_episodes(product_key,id DESC);
CREATE TABLE IF NOT EXISTS bom_announcements (
 id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL, mode TEXT NOT NULL, destination TEXT NOT NULL,
 revision_hash TEXT NOT NULL, material_hash TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, operation TEXT NOT NULL DEFAULT 'normal',
 history_id INTEGER UNIQUE, state TEXT NOT NULL, introduced INTEGER NOT NULL DEFAULT 0,
 created TEXT NOT NULL, provenance TEXT NOT NULL DEFAULT 'observed'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bom_announcement_normal
 ON bom_announcements(episode_id,mode,destination,revision_hash) WHERE operation='normal';
CREATE INDEX IF NOT EXISTS idx_bom_announcement_audience ON bom_announcements(episode_id,mode,destination,id DESC);
CREATE TRIGGER IF NOT EXISTS bom_announcement_delivery AFTER UPDATE OF transmit_status,delivery_parts ON service_history
BEGIN
 UPDATE bom_announcements SET state=COALESCE(NEW.transmit_status,'unknown'),
 introduced=CASE WHEN mode='dry-run' AND NEW.transmit_status='dry-run' THEN 1
 WHEN NEW.transmit_status IN ('success','unconfirmed','repeat_confirmed') THEN 1
 WHEN EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(NEW.delivery_parts) THEN NEW.delivery_parts ELSE '[]' END) p
 WHERE json_extract(p.value,'$.status') IN ('submitting','transmitted','local_confirmed','repeat_confirmed','unconfirmed')
 OR json_extract(p.value,'$.may_have_transmitted')=1) THEN 1 ELSE 0 END
 WHERE history_id=NEW.id AND provenance!='legacy-unknown';
END;
"""


class AnnouncementExists(Exception):
    """Another operation already reserved this episode announcement."""


def product_key(provider_id):
    value = str(provider_id).strip()
    try:
        url = urlsplit(value)
        hostname = url.hostname
    except ValueError:
        return value
    if hostname in ('bom.gov.au', 'www.bom.gov.au', 'reg.bom.gov.au'):
        match = re.fullmatch(r'/products/(ID[A-Z0-9]+)\.shtml', url.path, re.I)
        if match:
            return 'BOM:' + match[1].upper()
        return 'BOM:' + url.path.rstrip('/')
    return value


def suffix(number):
    result = ''
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def destination(settings, channel):
    connection = settings.get('meshcore_conn', 'serial')
    target = settings.get('meshcore_host' if connection == 'tcp' else 'meshcore_port', '')
    channels = settings.get('meshcore_channels', [])
    cached_target = settings.get('meshcore_channels_target')
    if cached_target is not None and cached_target != {'conn': connection, 'target': target}:
        channels = []
    channel_info = next((entry for entry in channels if isinstance(entry, dict)
                         and entry.get('index', entry.get('channel_idx')) == channel), {}) if isinstance(channels, list) else {}
    identity = ['meshcore', connection, target, channel,
                channel_info.get('identity', '')]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def full_cancellation(alert):
    if alert.message_type == 'Cancel':
        return True
    sections = alert.warning_sections
    return bool(sections) and all(section.phase == 'CAN' or section.phenomenon.casefold() == 'cancellation'
                                  for section in sections)


def saved_alert(raw):
    from .models import Alert
    from .bom_enricher import WarningSection
    alert = Alert.from_bom(raw)
    alert.specific_locations = raw.get('specific_locations', '')
    alert.warning_summary = raw.get('warning_summary', '')
    alert.warning_sections = tuple(WarningSection(**section) for section in raw.get('warning_sections', []))
    return alert


class Episodes:
    def __init__(self, db):
        self.db = db

    @contextmanager
    def transaction(self):
        with self.db._lock:
            self.db._conn.execute('BEGIN IMMEDIATE')
            try:
                yield self.db._conn
                self.db._conn.commit()
            except BaseException:
                self.db._conn.rollback()
                raise

    def _resolve(self, conn, alert, now):
        cancelled = full_cancellation(alert)
        key = product_key(alert.alert_id)
        alias = conn.execute('SELECT product_key FROM bom_product_aliases WHERE provider_id=?', (alert.alert_id,)).fetchone()
        key = alias[0] if alias else key
        known = conn.execute('SELECT 1 FROM bom_products WHERE product_key=?', (key,)).fetchone()
        if cancelled and not known:
            keys = set()
            for reference in alert.references:
                row = conn.execute('SELECT product_key FROM bom_product_aliases WHERE provider_id=?', (reference,)).fetchone()
                candidate = row[0] if row else product_key(reference)
                if conn.execute('SELECT 1 FROM bom_products WHERE product_key=?', (candidate,)).fetchone():
                    keys.add(candidate)
            if len(keys) != 1:
                return None
            key = keys.pop()
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        previous = conn.execute('SELECT * FROM bom_episodes WHERE product_key=? ORDER BY id DESC LIMIT 1', (key,)).fetchone()
        if previous and previous['state'] == 'open' and previous['expires_at'] is not None and previous['expires_at'] <= now:
            conn.execute("UPDATE bom_episodes SET state='closed',closed_at=?,closure_reason='expired' WHERE id=?", (stamp, previous['id']))
            previous = conn.execute('SELECT * FROM bom_episodes WHERE id=?', (previous['id'],)).fetchone()
        if cancelled:
            if not previous:
                return None
            conn.execute('INSERT OR IGNORE INTO bom_product_aliases VALUES (?,?)', (alert.alert_id, key))
            conn.execute("UPDATE bom_episodes SET state='closed',closed_at=COALESCE(closed_at,?),closure_reason='cancelled',last_observed=?,missing_polls=0 WHERE id=?",
                         (stamp, stamp, previous['id']))
            return dict(conn.execute('SELECT * FROM bom_episodes WHERE id=?', (previous['id'],)).fetchone())
        expiry = epoch(alert.expires)
        if expiry is not None and expiry <= now:
            if previous:
                conn.execute('UPDATE bom_episodes SET last_observed=?,missing_polls=0 WHERE id=?', (stamp, previous['id']))
            return dict(previous) if previous else None
        if not previous or previous['state'] != 'open':
            product = conn.execute('SELECT * FROM bom_products WHERE product_key=?', (key,)).fetchone()
            if not product:
                digest = hashlib.sha256(alert.alert_id.encode()).hexdigest().upper()
                width = 4
                prefix = digest[:width]
                while conn.execute('SELECT 1 FROM bom_products WHERE prefix=?', (prefix,)).fetchone():
                    width += 1
                    if width > len(digest):
                        raise ValueError('BOM product reference collision cannot be resolved')
                    prefix = digest[:width]
                conn.execute('INSERT INTO bom_products(product_key,prefix) VALUES (?,?)', (key, prefix))
                product = conn.execute('SELECT * FROM bom_products WHERE product_key=?', (key,)).fetchone()
            number = product['next_sequence']
            conn.execute('UPDATE bom_products SET next_sequence=next_sequence+1 WHERE product_key=?', (key,))
            cursor = conn.execute('INSERT INTO bom_episodes(product_key,sequence,reference,provider_id,opened_at,last_observed,expires_at) VALUES (?,?,?,?,?,?,?)',
                                  (key, number, product['prefix'] + '-' + suffix(number), alert.alert_id, stamp, stamp, expiry))
            identity = cursor.lastrowid
        else:
            identity = previous['id']
            conn.execute('UPDATE bom_episodes SET last_observed=?,expires_at=?,missing_polls=0 WHERE id=?', (stamp, expiry, identity))
        conn.execute('INSERT OR IGNORE INTO bom_product_aliases VALUES (?,?)', (alert.alert_id, key))
        return dict(conn.execute('SELECT * FROM bom_episodes WHERE id=?', (identity,)).fetchone())

    def observe(self, alert, *, now=None):
        with self.transaction() as conn:
            return self._resolve(conn, alert, datetime.now(timezone.utc).timestamp() if now is None else now)

    def lookup(self, alert):
        with self.db._lock:
            alias = self.db._conn.execute('SELECT product_key FROM bom_product_aliases WHERE provider_id=?', (alert.alert_id,)).fetchone()
            primary = alias[0] if alias else product_key(alert.alert_id)
            own = self.db._conn.execute('SELECT * FROM bom_episodes WHERE product_key=? ORDER BY id DESC LIMIT 1', (primary,)).fetchone()
            if own:
                return dict(own)
            keys = [product_key(ref) for ref in alert.references] if full_cancellation(alert) else []
            rows = []
            for key in set(keys):
                row = self.db._conn.execute('SELECT * FROM bom_episodes WHERE product_key=? ORDER BY id DESC LIMIT 1', (key,)).fetchone()
                if row:
                    rows.append(dict(row))
            return rows[0] if len(rows) == 1 else None

    def observe_snapshot(self, conn, items, stamp):
        """Called inside the native/catalogue snapshot transaction, never by replay."""
        import uuid
        now = epoch(stamp) or datetime.now(timezone.utc).timestamp()
        for raw in items:
            self._resolve(conn, saved_alert(raw), now)
        self._snapshot(conn, {product_key(raw['id']) for raw in items}, uuid.uuid4().hex, now)

    def snapshot(self, provider_ids, snapshot_id, *, now=None):
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        present = {product_key(value) for value in provider_ids}
        with self.transaction() as conn:
            self._snapshot(conn, present, snapshot_id, now)

    def _snapshot(self, conn, present, snapshot_id, now):
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        for episode in conn.execute("SELECT * FROM bom_episodes WHERE state='open'").fetchall():
            if episode['snapshot_id'] == snapshot_id:
                continue
            missing = 0 if episode['product_key'] in present else episode['missing_polls'] + 1
            reason = 'expired' if episode['expires_at'] is not None and episode['expires_at'] <= now else 'no longer observed' if missing >= 2 else ''
            conn.execute('UPDATE bom_episodes SET missing_polls=?,snapshot_id=?,state=?,closed_at=?,closure_reason=? WHERE id=?',
                         (missing, snapshot_id, 'closed' if reason else 'open', stamp if reason else None, reason, episode['id']))

    def announcement(self, episode, mode, audience, revision, *, allow_content_match=True, material_hash=""):
        if not episode:
            return None, False
        with self.db._lock:
            row = self.db._conn.execute('SELECT a.*,h.transmitted_text,h.delivery_parts,h.detail,h.disposition,h.metadata,h.revision_hash AS history_revision '
                                       'FROM bom_announcements a LEFT JOIN service_history h ON h.id=a.history_id '
                                       "WHERE a.episode_id=? AND a.mode=? AND a.destination=? AND a.revision_hash=? ORDER BY a.id DESC LIMIT 1",
                                       (episode['id'], mode, audience, revision)).fetchone()
            if row is None and allow_content_match:
                # Coverage-setting changes do not resend an unchanged provider
                # revision already introduced to this same audience and mode.
                row = self.db._conn.execute('SELECT a.*,h.transmitted_text,h.delivery_parts,h.detail,h.disposition,h.metadata,h.revision_hash AS history_revision '
                                           'FROM bom_announcements a LEFT JOIN service_history h ON h.id=a.history_id '
                                           "WHERE a.episode_id=? AND a.mode=? AND a.destination=? AND "
                                           "substr(a.revision_hash,1,instr(a.revision_hash||':',':')-1)=? AND "
                                           "(a.introduced=1 OR a.provenance='legacy-unknown') "
                                           "AND a.state IN ('queued','submitting','success','unconfirmed','repeat_confirmed','dry-run') ORDER BY a.id DESC LIMIT 1",
                                           (episode['id'], mode, audience, revision.partition(':')[0])).fetchone()
            if row is None and allow_content_match and material_hash:
                # Compare the last introduced/in-flight content, not every old
                # content hash: a real content reversion must still be an update.
                candidate = self.db._conn.execute('SELECT a.*,h.transmitted_text,h.delivery_parts,h.detail,h.disposition,h.metadata,h.revision_hash AS history_revision '
                                                  'FROM bom_announcements a LEFT JOIN service_history h ON h.id=a.history_id '
                                                  "WHERE a.episode_id=? AND a.mode=? AND a.destination=? "
                                                  "AND (a.introduced=1 OR a.state IN ('queued','submitting') OR a.provenance='legacy-unknown') ORDER BY a.id DESC LIMIT 1",
                                                  (episode['id'], mode, audience)).fetchone()
                if candidate and candidate['material_hash'] == material_hash:
                    row = candidate
            introduced = self.db._conn.execute('SELECT 1 FROM bom_announcements WHERE episode_id=? AND mode=? AND destination=? AND introduced=1 LIMIT 1',
                                              (episode['id'], mode, audience)).fetchone()
        return dict(row) if row else None, bool(introduced)

    def register(self, episode, mode, audience, revision, action, history_id, *, force=False, material_hash="", allow_content_match=True):
        """Reserve an announcement identity before queue admission; retry the same row."""
        with self.transaction() as conn:
            return self._register(conn, episode, mode, audience, revision, action, history_id, force=force, material_hash=material_hash, allow_content_match=allow_content_match)

    def _register(self, conn, episode, mode, audience, revision, action, history_id, *, force=False, material_hash="", allow_content_match=True):
        pending = conn.execute("SELECT history_id FROM bom_announcements WHERE episode_id=? AND mode=? AND destination=? "
                               "AND (revision_hash=? OR (?!='' AND material_hash=?)) AND state IN ('queued','submitting') ORDER BY id DESC LIMIT 1",
                               (episode['id'], mode, audience, revision, material_hash, material_hash)).fetchone()
        if pending and pending['history_id'] != history_id:
            return False
        operation = 'resend' if force else 'normal'
        row = conn.execute('SELECT transmit_status,delivery_parts FROM service_history WHERE id=?', (history_id,)).fetchone()
        state = row['transmit_status'] or 'unknown'
        try:
            parts = json.loads(row['delivery_parts'] or '[]')
        except ValueError:
            parts = []
        introduced = state in ('dry-run', 'success', 'repeat_confirmed', 'unconfirmed') or any(
            part.get('status') in ('transmitted', 'local_confirmed', 'repeat_confirmed', 'unconfirmed', 'submitting') or part.get('may_have_transmitted') for part in parts)
        linked = conn.execute('SELECT * FROM bom_announcements WHERE history_id=?', (history_id,)).fetchone()
        if linked:
            if (linked['episode_id'], linked['mode'], linked['destination']) != (episode['id'], mode, audience):
                return False
            if linked['provenance'] == 'legacy-unknown':
                conn.execute("UPDATE bom_announcements SET operation='normal',provenance='recovered-legacy',state=?,introduced=?,action=? WHERE id=?",
                             (state, int(introduced), action, linked['id']))
            return True
        if not force:
            old = conn.execute("SELECT * FROM bom_announcements WHERE episode_id=? AND mode=? AND destination=? AND revision_hash=? AND operation='normal'",
                               (episode['id'], mode, audience, revision)).fetchone()
            if old:
                return old['history_id'] == history_id
            if material_hash and allow_content_match:
                previous = conn.execute('SELECT material_hash FROM bom_announcements WHERE episode_id=? AND mode=? AND destination=? AND introduced=1 ORDER BY id DESC LIMIT 1',
                                        (episode['id'], mode, audience)).fetchone()
                if previous and previous['material_hash'] == material_hash:
                    return False
        cursor = conn.execute('INSERT OR IGNORE INTO bom_announcements(episode_id,mode,destination,revision_hash,material_hash,action,operation,history_id,state,introduced,created) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (episode['id'], mode, audience, revision, material_hash, action, operation, history_id, state, int(introduced), datetime.now(timezone.utc).isoformat()))
        return cursor.rowcount == 1


    def bootstrap(self):
        """Keep upgrade suppression anchors without inventing historical delivery proof."""
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM history_migrations WHERE name='bom_episodes_v1'").fetchone():
                return
            settings = {row['key']: json.loads(row['value']) for row in conn.execute('SELECT * FROM settings')}
            audience = destination(settings, settings.get('meshcore_channel', 0))
            now = datetime.now(timezone.utc).timestamp()
            for saved in conn.execute("SELECT * FROM bom_current WHERE region='NSW'").fetchall():
                raw = json.loads(saved['raw_data'] or '{}')
                if not raw:
                    continue
                alert = saved_alert(raw)
                episode = self._resolve(conn, alert, now)
                if not episode:
                    continue
                history = conn.execute("SELECT * FROM service_history WHERE source='bom' AND external_id=? AND transmitted_text!='' ORDER BY id DESC LIMIT 1", (alert.alert_id,)).fetchone()
                if history and history['transmit_status'] in ('dry-run','success','repeat_confirmed','unconfirmed','queued','interrupted','failed','deferred'):
                    mode = 'dry-run' if history['transmit_status'] == 'dry-run' else 'live'
                    # Current provider content is not evidence of what an older
                    # History revision contained. Only attach a matching hash.
                    material = alert.content_hash() if history['revision_hash'].partition(':')[0] == alert.revision_hash() else ''
                    conn.execute("INSERT INTO bom_announcements(episode_id,mode,destination,revision_hash,material_hash,action,operation,history_id,state,created,provenance) VALUES (?,?,?,?,?,?,'legacy-anchor',?,?,?,'legacy-unknown')",
                                 (episode['id'], mode, audience, history['revision_hash'], material, 'UNKNOWN', history['id'], history['transmit_status'], history['ts']))
            conn.execute("INSERT INTO history_migrations VALUES ('bom_episodes_v1')")
