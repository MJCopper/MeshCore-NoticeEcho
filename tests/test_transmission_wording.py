"""Radio classifications are presentation only; recovery retains recorded parts."""
import json
from dataclasses import replace
import pytest
from app.brief import transmission_label, transmission_topic, without_classification_prefix, brief_bom_parts, NoticeTooLong
from app.delivery import recovery_parts, remaining_parts
from app.models import Alert
from app.bom_enricher import WarningSection
from app.rfs.feed import Incident
from app.rfs.poller import format_incident
from app.traffic.poller import format_item
from app.notice_selection import evaluate, grouped_policy, proposal
from test_traffic import item
from test_troubleshooting import environment, seed, rows
from test_brief_notices import readable

@pytest.mark.parametrize('value',['', 'Not Applicable', ' NOT  APPLICABLE: ', 'not-applicable', 'N/A', 'Unrecognized', 'Unrecognised.', 'Not supplied', 'UNKNOWN', 'Other', 'Notice type not supplied'])
def test_only_complete_placeholder_classifications_are_suppressed(value):
    assert transmission_label(value)==''

@pytest.mark.parametrize('value',['Emergency Warning','Watch and Act','Advice','Planned Burn','Grass Fire','Other hazard warning','Unknown Road','Unrecognized chemical spill','Road reopening unconfirmed','Window unknown'])
def test_meaningful_provider_wording_is_preserved(value):
    assert transmission_label(value)==value

@pytest.mark.parametrize('title,label,expected',[
    ('BREAKDOWN: B-double','Breakdown','B-double'),
    ('Grass-Fire: Road','Grass Fire','Road'),
    ('Crashing vehicle','CRASH','Crashing vehicle'),
    ('Grass Fire','Fire','Grass Fire'),
    ('Fireball','Fire','Fireball'),
])
def test_duplicate_prefix_requires_equivalent_words_and_boundary(title,label,expected):
    assert without_classification_prefix(title,label)==expected


def incident(**changes):
    return replace(Incident('wording','NEW ENGLAND HWY, KENTUCKY','Not Applicable','Tamworth','NEW ENGLAND HWY, KENTUCKY 2354','Under control','Grass Fire','',''),**changes)

@pytest.mark.parametrize('level',['Not Applicable','Unknown','Unrecognized','Not supplied','Other',''])
def test_rfs_promotes_cause_without_repeating_it(level):
    event=incident(level=level);revision=event.revision
    parts=format_incident(event,126);text=readable(parts)
    assert 'NSW RFS NEW Grass Fire:' in text
    assert text.count('Grass Fire')==1
    assert 'KENTUCKY 2354' in text and 'Under control' in text
    assert event.revision==revision and len(parts)<=2
    assert all(len(p.encode())<=126 for p in parts)

@pytest.mark.parametrize('level',['Emergency Warning','Watch and Act','Advice','Planned Burn','Novel evacuation level'])
def test_rfs_keeps_meaningful_levels_and_cause(level):
    text=readable(format_incident(incident(level=level),126))
    assert level in text and 'Grass Fire' in text


def test_rfs_neutral_fallback_and_locations_are_not_cleaned():
    text=readable(format_incident(incident(level='Other',kind='Unknown',name='Unknown Road',location='Unknown Road, Other Town'),126))
    assert 'NSW RFS NEW Incident:' in text
    assert 'Unknown Road, Other Town' in text


@pytest.mark.parametrize('category,title,expected',[
    ('Other','Fallen tree','Fallen tree'),
    ('Unrecognized','Chemical spill','Chemical spill'),
    ('Unknown','Unknown','Traffic notice'),
    ('Not supplied','','Traffic notice'),
    ('Other','Other: Fallen tree','Fallen tree'),
    ('Novel hazard','Novel hazard: avoid area','Novel hazard'),
])
def test_traffic_prefers_useful_provider_category_or_title(category,title,expected):
    event=item(category=category,raw_category=category,title=title,road='Unknown Road',suburb='Other Town')
    revision=event.revision
    text=readable(format_item(event,'',126))
    assert f'{expected}:' in text and 'Live Traffic NSW NEW' in text
    assert text.count(expected)==1 and 'Unknown Road' in text and 'Other Town' in text
    assert event.revision==revision


def test_traffic_missing_raw_category_does_not_transmit_parser_placeholder():
    text=readable(format_item(item(category='Incident',raw_category='',title='Fallen tree'),'',126))
    assert 'NEW Fallen tree:' in text


def test_traffic_material_uncertainty_survives():
    parts=format_item(item(feed='roadwork',category='Other',title='Road work'),'',126,action='ENDED')
    assert 'ENDED' in parts[0] and 'road reopening unconfirmed' in readable(parts)
    text=readable(format_item(item(feed='roadwork',category='Other',title='Road work'),'',126))
    assert 'closure unconfirmed' in text and 'Window unknown' in text

@pytest.mark.parametrize('event',['','Not supplied','Unrecognized','Unknown','Other','Not Applicable'])
def test_bom_neutral_fallback_retains_locations_and_cause(event):
    alert=Alert('wording',event,'','Unknown Road, Other Town','','','Alert',warning_summary='Risk of damaging winds.')
    revision=alert.revision_hash()
    parts=brief_bom_parts(alert,'Australia/Sydney','NEW',126)
    assert 'BOM NEW Weather notice' in readable(parts)
    assert all(v in readable(parts) for v in ('damaging winds','Unknown Road','Other Town'))
    assert alert.revision_hash()==revision


def test_bom_useful_unknown_event_and_section_classifications():
    alert=Alert('wording','Novel Weather Warning','','','','','Alert',warning_sections=(WarningSection('Unknown','Town'),WarningSection('Storm surge','Coast')))
    text=readable(brief_bom_parts(alert,'','NEW',126))
    assert 'Novel Weather Warning' in text and 'Unknown' not in text
    assert 'Town' in text and 'Storm surge' in text and 'Coast' in text
    alert.warning_sections=(WarningSection('Unknown','Town','CAN'),)
    parts=brief_bom_parts(alert,'','CANCELLED',126)
    assert 'CANCELLED' in parts[0] and 'Town' in readable(parts)

@pytest.mark.parametrize('status',['failed','interrupted','deferred'])
def test_recovery_keeps_recorded_parts_after_wording_change(status):
    old=['1/2 NSW RFS NEW #AAAA Not Applicable: Road','2/2 Grass Fire; Under control; check rfs.nsw.gov.au']
    row={'revision_hash':'same','transmit_status':status,'transmitted_text':' || '.join(old),'delivery_parts':json.dumps([{'status':'local_confirmed'},{'status':'failed'}])}
    assert recovery_parts(row,'same',126)==old
    assert remaining_parts(row,' || '.join(old),old)==[1]
    assert recovery_parts(row,'changed',126) is None
    assert recovery_parts(row,'same',126,force=True)==(old if status=='deferred' else None)
    with pytest.raises(NoticeTooLong):recovery_parts(row,'same',20)

@pytest.mark.parametrize('source',['bom','rfs','traffic'])
@pytest.mark.asyncio
async def test_cleaned_wording_in_preview_resend_and_unchanged_filtering(environment,source):
    app=environment;db=app.state.db;event=seed(app,source)
    policy=grouped_policy(source,proposal(source,db.all_settings()))
    for dim in policy['dimensions'].values():
        for group in dim['groups'].values():group['mode']='all'
        dim['special']=['unrecognized','not-supplied']
    db.set_setting(source+'_notice_selection',policy)
    if source=='rfs':
        event=replace(event,level='Not Applicable');db.rfs_save_incident(event,event.revision)
        values={'level':event.level,'kind':event.kind}
    elif source=='traffic':
        event=replace(event,category='Other',raw_category='Other',title='Fallen tree');db.traffic_save_item(event,'Central Coast',True)
        values={'category':'Other','feed':'incident'}
    else:
        event=dict(event,event='Not supplied');db.replace_bom_current([event],{'NSW'},'2026-10-10T00:00:00+00:00')
        values={'type':'Not supplied'}
    before=evaluate(source,values,db.all_settings()).metadata()
    history=rows(db,'SELECT * FROM service_history')
    preview=await app.state.troubleshooting.preview(source,'resend')
    assert preview['notices']==1 and not preview['errors'] and not app.state.tx.sent
    preview_text=preview['details'][0]['text']
    assert 'Not Applicable' not in preview_text and 'Not supplied' not in preview_text and 'Unrecognized' not in preview_text
    assert rows(db,'SELECT * FROM service_history')==history
    app.state.troubleshooting.start(preview['token']);await app.state.troubleshooting.task
    assert len(app.state.tx.sent)==1
    text=readable([part[0] for part in app.state.tx.sent[0]])
    assert 'Not Applicable' not in text and 'Not supplied' not in text and 'Unrecognized' not in text
    assert evaluate(source,values,db.all_settings()).metadata()==before
    meta=json.loads(rows(db,'SELECT * FROM service_history ORDER BY id DESC LIMIT 1')[0]['metadata'])['notice_selection']
    assert meta['dimensions']==before['dimensions']


@pytest.mark.parametrize('source',['bom','rfs','traffic'])
@pytest.mark.parametrize('shrink_budget',[False,True])
@pytest.mark.asyncio
async def test_partial_delivery_resumes_original_parts_after_upgrade(environment,monkeypatch,source,shrink_budget):
    import importlib
    from app.filters import FilterRules
    app=environment;db=app.state.db;event=seed(app,source)
    if source=='rfs':db.rfs_mark_sent(event.incident_id,'')
    if source=='traffic':db.traffic_mark_sent(event.item_id,'')
    module=importlib.import_module({'bom':'app.poller','rfs':'app.rfs.poller','traffic':'app.traffic.poller'}[source])
    formatter={'bom':'brief_bom_parts','rfs':'format_incident','traffic':'format_item'}[source]
    old=['1/2 '+source.upper()+' NEW #ABCD Not Applicable: Example Road','2/2 Grass Fire; Under control; check provider']
    original=getattr(module,formatter)
    monkeypatch.setattr(module,formatter,lambda *a,**kw:old)
    app.state.tx.pause=True
    async def process():
        if source=='bom':
            settings=db.all_settings()
            await app.state.poller._process(dict(event),FilterRules.from_settings(settings),'Australia/Sydney',0,False,settings)
        else:
            poller=getattr(app.state,source+'_poller')
            class Client:
                async def fetch(self):return [event]
                async def boundaries(self):return []
            poller.client=Client();await poller.poll_once()
    await process()
    parts,callback,_=app.state.tx.pending[-1]
    assert [p[0] for p in parts]==old
    callback(0,True,'');callback(1,False,'link down')
    monkeypatch.setattr(module,formatter,original)
    app.state.tx.sent.clear();app.state.tx.pause=False
    if shrink_budget:
        row=rows(db,'SELECT * FROM service_history ORDER BY id DESC LIMIT 1')[0]
        app.state.tx.message_budget=20
        await process()
        assert not app.state.tx.sent
        retained=rows(db,'SELECT * FROM service_history ORDER BY id DESC LIMIT 1')[0]
        assert retained['id']==row['id'] and retained['transmitted_text']==row['transmitted_text']
        assert retained['delivery_parts']==row['delivery_parts']
        app.state.tx.message_budget=126
    await process()
    assert len(app.state.tx.sent)==1
    assert [p[0] for p in app.state.tx.sent[0]]==[old[1]]
    record=rows(db,'SELECT * FROM service_history ORDER BY id DESC LIMIT 1')[0]
    assert record['transmitted_text']==' || '.join(old) and record['transmit_status']=='success'
    app.state.tx.sent.clear();await process()
    assert not app.state.tx.sent


def test_rfs_equivalent_level_and_kind_are_included_once():
    text=readable(format_incident(incident(level='Planned Burn',kind='Planned-Burn'),126))
    assert 'Planned Burn' in text and 'Planned-Burn' not in text


def test_location_exception_and_numbering_with_cleaned_classifications():
    locations=', '.join(f'District {i:02} Valley' for i in range(12))
    alert=Alert('wording','Not supplied','',locations,'','','Alert',warning_summary='Damaging winds.')
    parts=brief_bom_parts(alert,'','NEW',126)
    assert len(parts)==3
    assert all(p.startswith(f'{i}/3 ') and len(p.encode())<=126 for i,p in enumerate(parts,1))
    text=readable(parts)
    assert 'Not supplied' not in text and text.count('BOM NEW')==1
    assert all(f'District {i:02} Valley' in text for i in range(12))


@pytest.mark.parametrize('source',['bom','rfs','traffic'])
def test_empty_classifications_and_details_have_clean_source_only_body(source):
    if source=='bom':parts=brief_bom_parts(Alert('empty','','','','','','Alert'),'','NEW',126)
    elif source=='rfs':parts=format_incident(incident(name='',location='',level='',kind='',status='',council=''),126)
    else:parts=format_item(item(category='',raw_category='',title='',road='',suburb='',direction='',impact='',advice=''),'',126)
    text=readable(parts)
    assert ': .' not in text and '::' not in text and '; ;' not in text
    assert text.count('check ')==1
    assert len(parts)==1 and len(parts[0].encode())<=126


def test_unicode_and_cancellation_with_placeholder_heading():
    alert=Alert('unicode','Not Applicable','','Café Valley, Other Town','','','Cancel')
    parts=brief_bom_parts(alert,'','CANCELLED',126)
    assert 'CANCELLED' in parts[0] and 'Café Valley' in readable(parts)
    assert 'Not Applicable' not in readable(parts)
    assert all(len(p.encode())<=126 for p in parts)
