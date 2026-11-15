"""Evidence-based local BOM scope, lifecycle and conservative content comparison.

Events retain announcement provenance. Delivery state is read from the shared
announcement ledger, so queue admission never invents successful introduction.
"""
from dataclasses import asdict, replace
import hashlib
import json
import re
import time

from .geography import KEY, active, contains, normalise, location_matches
from .bom_geography import cancellation_only
from .bom_episodes import full_cancellation
from .transmission import COMPLETED, NOTICE_COMPLETED
from . import bom_information

VERSION = 1
# Only recognized, independently named NSW marine zones support proving a
# section unrelated without additional polygon/identifier evidence.
MARINE_COASTS = ('Byron Coast','Coffs Coast','Macquarie Coast','Hunter Coast',
                 'Sydney Coast','Illawarra Coast','Batemans Coast','Eden Coast')
SCHEMA = """
CREATE TABLE IF NOT EXISTS bom_scope_events (
 id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL, mode TEXT NOT NULL,
 destination TEXT NOT NULL, kind TEXT NOT NULL, identity TEXT NOT NULL,
 payload TEXT NOT NULL, history_id INTEGER UNIQUE,
 UNIQUE(episode_id,mode,destination,kind,identity)
);
CREATE INDEX IF NOT EXISTS idx_bom_scope_lane ON bom_scope_events(episode_id,mode,destination,id);
"""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def names(text):
    # "and" is part of names such as North West Slopes and Plains.
    return list(dict.fromkeys(x.strip() for x in str(text or '').split(',') if x.strip()))


def scope(kind, label, aliases=()):
    return dict(key=kind+':'+normalise(label), kind=kind, label=label,
                aliases=sorted(set([label, *aliases])))


def selected(item, settings):
    if active(settings):
        policy = settings[KEY]
        if policy['all_nsw']:
            return True
        values = policy['councils'] if item['kind']=='council' else policy['location_terms']
        return any(normalise(x)==normalise(item['label']) for x in values)
    if settings.get('bom_all_councils', True):
        return True
    values = settings.get('bom_councils', []) if item['kind']=='council' else settings.get('bom_districts', [])
    return any(normalise(x)==normalise(item['label']) for x in values)


def active_scopes(alert, coverage, settings):
    if not coverage.included:
        return []
    result = [scope('council', x) for x in coverage.councils]
    result += [scope('district', x) for x in coverage.districts]
    result += [scope('term', x[0]) for x in getattr(coverage, 'term_matches', ())]
    if not result:
        all_selected = (settings.get(KEY) or {}).get('all_nsw') if active(settings) else settings.get('bom_all_councils', True)
        if all_selected:
            values = [s.areas for s in alert.warning_sections if not cancelled(s)]
            values = [x for v in values for x in (marine_zones(v) or names(v))] or names(alert.specific_locations or alert.area_desc)
            result = [scope('provider', x) for x in values]
        else:
            # Unknown geography can admit an active update, but cannot authorize a scoped cancellation.
            result = [scope('uncertain', 'Geographic scope not established')]
    for item in result:
        item['aliases'] = sorted(set(item['aliases'] + [x for s in alert.warning_sections if not cancelled(s)
                                                       for x in names(s.areas) if contains(x, item['label'])]))
        item['provider_ids']=sorted({a['type']+':'+a['code'] for a in alert.raw.get('provider_areas',[])
                                     if any(contains(a.get('name',''),alias) for alias in item['aliases'])})
    return sorted({x['key']:x for x in result}.values(), key=lambda x:x['key'])


def cancelled(section):
    return section.phase == 'CAN' or section.phenomenon.casefold() == 'cancellation'


def marine_zones(value):
    remainder=normalise(value)
    zones=[]
    for label in MARINE_COASTS:
        if contains(remainder,label):
            zones.append(label)
            remainder=re.sub(r'(?<!\w)'+re.escape(normalise(label))+r'(?!\w)',' ',remainder)
    return zones if zones and not remainder.replace('and','').strip() else []


def strip_cancellations(alert):
    def clean(value):
        return ' '.join(s for s in re.split(r'(?<=[.!?])\s+', value or '')
                        if not re.search(r'\b(?:the|this) warning(?: for .+?)? (?:is|has been) cancelled\b', s, re.I))
    return replace(alert, warning_sections=tuple(s for s in alert.warning_sections if not cancelled(s)),
                   warning_summary=clean(alert.warning_summary), detail=clean(alert.detail))


def legacy_payload(row):
    meta = json.loads(row['metadata'] or '{}')
    saved = meta.get('bom_scope')
    if saved:
        return saved
    episode = meta.get('bom_episode') or {}
    if row['action'] == 'CANCELLED' or episode.get('scope') in ('full warning','selected areas'):
        return dict(version=VERSION, legacy_close=episode.get('scope')=='full warning', active=[], cancel=[])
    g = meta.get('geographic_coverage') or {}
    values = [scope('council', x) for x in g.get('councils', meta.get('matched_councils', []))]
    values += [scope('term', x[0]) for x in g.get('term_matches', [])]
    values += [scope('district', x) for x in g.get('districts', meta.get('matched_districts', []))]
    # A missing historical selection policy is not proof that every provider
    # area was selected. Preserve its dedupe anchor without inventing scope.
    policy = meta.get('geographic_policy') or {}
    if not values and (policy.get('all_nsw') or (not policy.get('active') and meta.get('all_councils') is True)):
        values = [scope('provider', x) for x in names(row['area'])]
    return dict(version=VERSION, active=values, cancel=[], provenance='legacy-evidence', fingerprint='')


def part_evidence(payload, item, row):
    if not row['introduced']:
        return False
    if row['state'] in NOTICE_COMPLETED | {'dry-run'}:
        return True
    parts=json.loads(row['delivery_parts'] or '[]')
    required=payload.get('parts',{}).get(item['key'],list(range(len(parts))))
    return bool(required) and all(i<len(parts) and (parts[i].get('status') in COMPLETED | {'submitting'}
                                                  or parts[i].get('may_have_transmitted')) for i in required)


def attach_parts(evidence, parts):
    evidence['parts']={}
    for item in (*evidence.get('active',[]),*evidence.get('cancel',[])):
        indices=[i for i,text in enumerate(parts) if any(contains(text,a) for a in item['aliases'])]
        evidence['parts'][item['key']]=sorted(set([0,*indices])) if indices else list(range(len(parts)))


class ScopeStore:
    def __init__(self, db):
        self.db = db

    def ensure(self, episode, mode, audience):
        """Idempotent lazy backfill; recorded messages and outcomes stay untouched."""
        with self.db.transaction('BOM scope evidence upgrade') as conn:
            rows = conn.execute('SELECT a.*,h.metadata,h.area FROM bom_announcements a JOIN service_history h ON h.id=a.history_id '
                                "WHERE a.episode_id=? AND a.mode=? AND a.destination=? AND a.provenance!='legacy-unknown' "
                                'AND NOT EXISTS (SELECT 1 FROM bom_scope_events e WHERE e.history_id=h.id) ORDER BY h.id',
                                (episode['id'],mode,audience)).fetchall()
            for row in rows:
                conn.execute("INSERT OR IGNORE INTO bom_scope_events(episode_id,mode,destination,kind,identity,payload,history_id) VALUES (?,?,?,'announcement',?,?,?)",
                             (episode['id'],mode,audience,str(row['history_id']),json.dumps(legacy_payload(row)),row['history_id']))

    def state(self, episode, mode, audience, *, ensure=True):
        if ensure:
            self.ensure(episode,mode,audience)
        with self.db._lock:
            rows = self.db._conn.execute('SELECT e.*,a.introduced,a.state,h.delivery_parts,h.transmitted_text FROM bom_scope_events e LEFT JOIN bom_announcements a ON a.history_id=e.history_id LEFT JOIN service_history h ON h.id=e.history_id '
                                        'WHERE e.episode_id=? AND e.mode=? AND e.destination=? ORDER BY e.id', (episode['id'],mode,audience)).fetchall()
        known, closed, notified, pending, last_closed, suppressed = {}, {}, set(), set(), {}, set()
        last_fingerprint = ''; observed = {}; incomplete = False; recovering_cancel=set(); ever_introduced=False
        information = {}; baseline = {}; recovery = None
        for row in rows:
            p = json.loads(row['payload'])
            if row['kind']=='observation':
                observed = p
                for key, token in p.get('close', {}).items():
                    if key in known and known[key]['token']==token:
                        closed[key]=known.pop(key)
                        last_closed[key]=token
                        information.pop(key, None); baseline.pop(key, None)
                    if p.get('suppressed') or token in p.get('retired',[]):
                        notified.add(token)
                        suppressed.add(token)
                continue
            introduced = bool(row['introduced'])
            reserved = row['state'] in ('queued','submitting')
            if p.get('active'):
                recovery = p if row['state'] in ('queued','submitting','failed','interrupted','deferred') else None
            if p.get('legacy_close') and introduced:
                for key,item in list(known.items()):
                    closed[key]=known.pop(key);last_closed[key]=item['token'];notified.add(item['token']);suppressed.add(item['token'])
                    information.pop(key, None); baseline.pop(key, None)
            if introduced:
                for item in p.get('active', []):
                    if not part_evidence(p,item,row):
                        continue
                    ever_introduced=True
                    key=item['key']
                    if key not in known:
                        known[key]=dict(item,token=f"{row['id']}:{key}")
                    parts=json.loads(row['delivery_parts'] or '[]')
                    complete = row['state'] in NOTICE_COMPLETED | {'dry-run'} or (bool(parts) and all(
                        part.get('status') in COMPLETED | {'submitting'} or part.get('may_have_transmitted') for part in parts))
                    saved = p.get('information', bom_information.legacy(p, row))
                    if complete and key in saved:
                        information[key]=saved[key]; baseline[key]=row['history_id']
                if p.get('active'):
                    last_fingerprint=p.get('active_fingerprint',p.get('fingerprint',''))
                    incomplete=row['state'] in ('queued','submitting','failed','interrupted','deferred')
            for item in p.get('cancel', []):
                token=item['token'];key=item['key']
                if row['state'] in ('failed','interrupted','deferred'):
                    recovering_cancel.add(token)
                if part_evidence(p,item,row):
                    notified.add(token)
                    if key in known and known[key]['token']==token:
                        closed[key]=known.pop(key);last_closed[key]=token
                elif reserved:
                    pending.add(token)
        return dict(active=known, closed=closed, notified=notified, pending=pending,
                    last_closed=last_closed, fingerprint=last_fingerprint, observed=observed, incomplete=incomplete,
                    recovering_cancel=recovering_cancel,ever_introduced=ever_introduced,suppressed=suppressed,
                    information=information,baseline=baseline,recovery=recovery)

    def plan(self, episode, mode, audience, alert, coverage, settings, *, observe=True, force=False, readonly=False,
             budget=126, tz_name='Australia/Sydney'):
        state=self.state(episode,mode,audience,ensure=not readonly)
        enabled=bool(settings.get('bom_send_cancellations',True))
        whole=full_cancellation(alert)
        expired=episode.get('expires_at') is not None and episode['expires_at']<=time.time()
        global_cancel=alert.message_type=='Cancel' and not alert.warning_sections
        active_items=[] if whole else active_scopes(alert,coverage,settings)
        active_keys={x['key'] for x in active_items}
        cancellation=cancellation_only(alert)
        explicit=cancellation.area_desc if cancellation else ''
        candidates={**state['closed'],**state['active']}
        targets=[]
        for key,item in candidates.items():
            if item['kind']=='uncertain':
                continue
            exclusions=(settings.get(KEY) or {}).get('location_exclusions',[]) if active(settings) else []
            matches=location_matches((('cancelled area',explicit),),item['aliases'],exclusions)[0]
            if global_cancel or (key not in active_keys and matches):
                targets.append(item)
        closing={x['key']:x['token'] for x in targets}
        retired=[x['token'] for x in targets if not selected(x,settings)]
        if expired:
            targets=[]
            closing={key:item['token'] for key,item in state['active'].items()}
            retired=list(closing.values())
        if whole and not global_cancel:
            # All-CAN section lists end the product episode, but do not prove
            # cancellation of other coasts which may simply have expired.
            for key,item in state['active'].items():
                if key not in closing:
                    closing[key]=item['token'];retired.append(item['token'])
        observation=dict(version=VERSION, provider_hash=alert.content_hash(), active=sorted(active_keys),
                         close=closing, suppressed=not enabled, retired=retired)
        available=[x for x in targets if selected(x,settings) and
                   (force or (x['token'] not in state['suppressed'] and
                              (x['token'] not in state['notified'] or x['token'] in state['recovering_cancel'])))]
        blocked=any(x['token'] in state['pending'] for x in available)
        notify=available if enabled and not blocked else []
        render=strip_cancellations(alert)
        if notify and not active_items:
            labels=', '.join(dict.fromkeys(x['label'] for x in notify))
            render=replace(render,message_type='Cancel',area_desc=labels,specific_locations=labels,
                           warning_sections=(),warning_summary='',detail='')
        elif notify:
            if alert.warning_sections:
                from .bom_enricher import WarningSection
                render=replace(render,warning_sections=(*render.warning_sections,WarningSection('Cancellation',', '.join(x['label'] for x in notify),'CAN')))
            else:
                render=replace(render,warning_summary=(render.warning_summary or render.detail)+
                               ' The warning for '+', '.join(x['label'] for x in notify)+' is CANCELLED.')
        active_render=strip_cancellations(render)
        active_render=replace(active_render,headline=re.sub(r'^\d{1,2}/\d{2}:\d{2}\s+(?:AEST|AEDT|EST|EDT|UTC)\s+', '', active_render.headline, flags=re.I))
        shared=[active_render.event,active_render.headline,active_render.area_desc,active_render.expires,active_render.ends,active_render.onset,
                active_render.detail,active_render.warning_summary,
                {k:(alert.raw.get('_enrichment') or {}).get(k,alert.raw.get(k)) for k in ('severity','urgency','certainty')},
                [(x['key'],state['last_closed'].get(x['key'],'')) for x in active_items]]
        relevant=[];partitioned=False
        if render.warning_sections and active_items:
            sections=[s for s in render.warning_sections if not cancelled(s)]
            relevant=[s for s in sections if any(contains(s.areas,alias) for x in active_items for alias in x['aliases'])]
            # Named provider sections must cover every selected scope; otherwise
            # a council footprint or free text cannot prove other sections unrelated.
            partitioned=bool(relevant) and all(any(contains(s.areas,a) for s in relevant for a in x['aliases']) for x in active_items)
            partitioned=partitioned and alert.event=='Marine Wind Warning' and all(marine_zones(s.areas) for s in sections)
        material=[VERSION,shared,[asdict(s) for s in relevant]] if partitioned else [VERSION,active_render.content_hash(),shared]
        active_fingerprint=digest(material)
        fingerprint=digest([material,[(x['key'],x['token']) for x in notify]])
        reason=('Expired episode retired without a cancellation notification' if expired else
                'Cancellation notifications disabled' if not enabled and (whole or cancellation) else
                'Cancellation already queued' if blocked else
                ('Cancellation of previously announced selected areas' if whole else 'Cancellation-only update for previously announced selected areas') if notify and not active_items else
                'No previously announced selected scope matches this cancellation' if whole or cancellation else
                'Selected scope or shared warning content changed')
        evidence=dict(version=VERSION,active=active_items,cancel=notify,fingerprint=fingerprint,
                      active_fingerprint=active_fingerprint,
                      forced=force,
                      provider_hash=alert.content_hash(),comparison=('announced selected scope' if not active_items else
                          'structured selected sections' if partitioned else 'conservative whole warning'),
                      uncertain=not partitioned,reason=reason,
                      activation=digest([(x['key'],state['last_closed'].get(x['key'],'')) for x in active_items])[:12])
        unchanged = bool(active_items and not notify and not state['incomplete'] and active_fingerprint==state['fingerprint'])
        if active_items:
            from .brief import brief_bom_parts, NoticeTooLong
            rendered = {}
            try:
                brief_bom_parts(render, tz_name, 'UPDATE' if state['ever_introduced'] else 'NEW', budget,
                                geographic_settings=settings if partitioned else None,
                                geographic_coverage=coverage, scope_evidence=rendered,
                                episode_reference=episode['reference'])
            except NoticeTooLong:
                # Recover an immutable prepared message before trying to format
                # it at a changed budget. Recovery itself validates its size.
                recovery = state['recovery']
                if (recovery and not notify and recovery.get('provider_hash')==alert.content_hash()
                        and recovery.get('activation')==evidence['activation']):
                    evidence.update(recovery)
                # Otherwise the ordinary formatting-blocked path records it.
            else:
                information = bom_information.conveyed(rendered, active_items,
                                                       routine_expiry=not bool(alert.raw.get('ends')), alert=render)
                for key, value in information.items():
                    if not value['facts'] and key in state['information']:
                        # Omitted local detail is not an explicit all-clear.
                        value['facts'] = state['information'][key]['facts']
                changes = [x['key'] for x in active_items if state['information'].get(x['key']) != information[x['key']]]
                unchanged = bool(not changes and not notify and not state['incomplete'])
                evidence.update(information_version=bom_information.VERSION, information=information,
                                baseline=state['baseline'], changed_scopes=changes,
                                comparison='information conveyed for selected locations',
                                reason=('Suppressed: no additional information for selected locations' if unchanged else
                                        'New or changed information for selected locations' if changes else reason))
                # The comparison identity is independent of unrelated provider
                # revisions; the transition anchor permits genuine reversions.
                evidence['active_fingerprint']=digest(information)
                evidence['fingerprint']=digest([information, state['baseline'],
                                               [(x['key'],x['token']) for x in notify], evidence['activation']])
                observation['information']=information
        if observe:
            observation['active_fingerprint']=evidence['active_fingerprint']
            with self.db.transaction('BOM scope observation') as conn:
                conn.execute("INSERT OR IGNORE INTO bom_scope_events(episode_id,mode,destination,kind,identity,payload) VALUES (?,?,?,'observation',?,?)",
                             (episode['id'],mode,audience,digest(observation),json.dumps(observation)))
        return dict(render=render,evidence=evidence,cancel=bool(notify and not active_items),blocked=blocked,
                    unchanged=unchanged,
                    terminal=bool(whole or (cancellation and not active_items)),state=state)

    def valid(self, evidence, episode, mode, audience, settings):
        if evidence.get('cancel') and not settings.get('bom_send_cancellations',True):
            return False
        if any(not selected(x,settings) for x in evidence.get('cancel',[])):
            return False
        state=self.state(episode,mode,audience)
        if not evidence.get('forced') and any(x['token'] in state['suppressed'] for x in evidence.get('cancel',[])):
            return False
        observed=state['observed']
        if observed:
            if any(x['key'] in observed.get('active',[]) for x in evidence.get('cancel',[])):
                return False
            if evidence.get('active'):
                if evidence.get('information') is not None and observed.get('information') is not None:
                    if any(observed['information'].get(key)!=value for key,value in evidence['information'].items()):
                        return False
                elif observed.get('active_fingerprint')!=evidence['active_fingerprint']:
                    return False
        return True


def bootstrap(db):
    with db.transaction('BOM scope migration') as conn:
        if conn.execute("SELECT 1 FROM history_migrations WHERE name='bom_scope_v1'").fetchone():
            return
        rows=conn.execute("SELECT a.*,h.metadata,h.area FROM bom_announcements a JOIN service_history h ON h.id=a.history_id WHERE a.provenance!='legacy-unknown' ORDER BY h.id").fetchall()
        for row in rows:
            conn.execute("INSERT OR IGNORE INTO bom_scope_events(episode_id,mode,destination,kind,identity,payload,history_id) VALUES (?,?,?,'announcement',?,?,?)",
                         (row['episode_id'],row['mode'],row['destination'],str(row['history_id']),json.dumps(legacy_payload(row)),row['history_id']))
        conn.execute("INSERT INTO history_migrations VALUES ('bom_scope_v1')")
