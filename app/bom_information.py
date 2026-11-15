"""Compare required radio content, without letting unrelated place lists trigger updates."""
import re

from .geography import contains, normalise

VERSION = 1
RISKS = {'damaging winds', 'destructive winds', 'large hail', 'giant hail', 'hail',
         'heavy rain', 'flash flooding', 'flooding', 'storm surge', 'hazardous surf',
         'abnormally high tides', 'tornadoes', 'blizzard', 'snow', 'heatwave'}


def wording(value):
    # Punctuation is generally cosmetic; signs and decimal points in measured
    # values carry information and must survive normalization.
    value = re.sub(r'(?<!\w)[−-](?=\d)', 'minus ', value)
    value = re.sub(r'(?<=\d)\.(?=\d)', ' point ', value)
    return normalise(value)


def facts(core):
    """Order independent positive risks; preserve qualifications and negation verbatim."""
    result = set()
    risks = set()
    for clause in core.split(';'):
        clause = re.sub(r'^ACTIVE\s*:?\s*', '', clause.strip(), flags=re.I)
        if clause and all(normalise(x) in RISKS for x in clause.split(',')):
            clause = 'Risk: ' + clause
        if clause.casefold().startswith('risk:'):
            risks.update(normalise(x) for x in clause[5:].split(',') if normalise(x))
        elif wording(clause):
            result.add(wording(clause))
    if {'large hail', 'giant hail'} & risks:
        risks.discard('hail')
    result.update('risk: ' + x for x in risks)
    return sorted(result)


def conveyed(evidence, scopes, *, routine_expiry=False, alert=None):
    """Sections come from a successful renderer; required content is never truncated."""
    from .bom_scope import marine_zones
    sections = evidence.get('conveyed_sections', [])
    result = {}
    for item in scopes:
        relevant = []
        for core, areas in sections:
            if 'CANCELLED' in core.upper():
                continue
            matches = any(contains(areas, a) for a in item['aliases'])
            # Independent marine sections have reliable attribution. Unknown
            # broad sections retain shared warning context rather than guessing.
            if matches or not marine_zones(areas):
                if routine_expiry:
                    core = re.sub(r'\buntil\s+[^;]+', '', core, flags=re.I)
                relevant.extend(facts(core))
        if alert is not None and not alert.warning_sections:
            from .brief import bom_risk_clauses, compact_text
            summary = compact_text(alert.warning_summary or alert.detail)
            supported = set(); excluded = set()
            for sentence in re.split(r'(?<=[.!?])\s+', summary):
                localised = re.search(r'\b(?:reported|observed|measured|recorded|near|forecast to affect|detected)\b', sentence, re.I)
                local = any(contains(sentence, alias) for alias in item['aliases'])
                shared = re.search(r'\b(?:across|throughout) (?:the )?(?:entire )?warning area\b', sentence, re.I)
                if not localised or local or shared or item['kind']=='provider':
                    supported.update(facts('; '.join(bom_risk_clauses(sentence))))
                else:
                    excluded.update(facts('; '.join(bom_risk_clauses(sentence))))
            # Reports/forecasts explicitly tied to other places must not create
            # a new local risk merely because the brief formatter lists it.
            relevant = [value for value in relevant if
                        (value in supported if value.startswith('risk: ') else value not in excluded or value in supported)]
        result[item['key']] = dict(topic=normalise(evidence.get('conveyed_topic', '')),
                                   facts=sorted(set(relevant)))
    return result


def legacy(payload, row):
    """Recover flat brief messages only; never invent missing historical detail."""
    text = ' '.join(re.sub(r'^\d+/\d+\s+', '', part.strip())
                    for part in (row['transmitted_text'] or '').split(' || '))
    text = re.sub(r'^BOM (?:NEW|UPDATE|RESEND) #[\w-]+\s+', '', text)
    topic, sep, body = text.partition(': ')
    core, separator, _ = body.partition(' for ')
    if not sep or not separator or 'CANCELLED' in text:
        return {}
    return {x['key']:dict(topic=normalise(topic), facts=facts(core)) for x in payload.get('active', [])}
