"""Jurisdiction, shared closure diagnostics and boundary recovery regressions."""
from dataclasses import replace
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app.db import Database
from app.geography import KEY, configuration, incident_coverage, traffic_coverage, incident_delivery_coverage, traffic_delivery_coverage
from app.jurisdiction import resolve, point_jurisdiction
from app.rfs.feed import parse_incidents
from app.rfs.poller import RFSPoller
from app.traffic.feed import TrafficFeedError
from app.traffic.poller import TrafficPoller
from app.web.geography_routes import saved_decisions
from test_universal_geography import prepare, rfs_item, traffic_item, web_app


@pytest.mark.parametrize('point,status',[
    ((151.2093,-33.8688),'NSW'),
    ((144.9631,-37.8136),'outside'),
    ((153.0251,-27.4698),'outside'),
    ((149.1300,-35.2809),'outside'),
    ((115.8605,-31.9505),'outside'),
    ((None,None),'unknown'),
    ((float('nan'),-33),'unknown'),
])
def test_point_jurisdiction(point,status):
    assert point_jurisdiction(*point)[0]==status


@pytest.mark.parametrize('service',['rfs','traffic'])
@pytest.mark.parametrize('policy',[
    configuration(all_nsw=True,include_uncertain=True),
    configuration(councils=['Central Coast'],location_terms=['Moonbi'],include_uncertain=True),
])
def test_interstate_evidence_cannot_be_rescued_by_any_match(service,policy):
    item=rfs_item() if service=='rfs' else traffic_item()
    candidate=replace(item,state='VIC')
    result=incident_coverage(candidate,{KEY:policy}) if service=='rfs' else traffic_coverage(candidate,'Central Coast',{KEY:policy})
    assert not result.included and result.jurisdiction=='VIC'
    candidate=replace(item,state='',lon=144.9631,lat=-37.8136)
    result=incident_coverage(candidate,{KEY:policy}) if service=='rfs' else traffic_coverage(candidate,'Central Coast',{KEY:policy})
    assert not result.included and result.jurisdiction=='outside'


def test_unknown_jurisdiction_requires_explicit_fallback():
    item=replace(traffic_item(),state='',lon=None,lat=None)
    assert resolve(item)[0]=='unknown'
    for policy in (configuration(all_nsw=True),configuration(location_terms=['Moonbi'])):
        result=traffic_coverage(item,'',{KEY:policy})
        assert not result.included and 'cannot be established' in result.reason
    result=traffic_coverage(item,'',{KEY:configuration(all_nsw=True,include_uncertain=True)})
    assert result.included and result.method=='uncertain geography'


def test_explicit_nsw_and_known_council_establish_jurisdiction_without_coordinates():
    assert resolve(traffic_item())[0]=='NSW'
    assert resolve(rfs_item())[0]=='NSW'


def test_rfs_preserves_state_and_coordinates_without_changing_notice_revision():
    base={'type':'FeatureCollection','features':[{'type':'Feature','geometry':{'type':'Point','coordinates':[144.96,-37.81]},
           'properties':{'guid':'state-check','title':'Moonbi Road','category':'Advice','state':'VIC',
                         'description':'COUNCIL AREA: Central Coast<br />LOCATION: Moonbi Road'}}]}
    parsed=parse_incidents(base)[0]
    assert (parsed.state,parsed.lon,parsed.lat)==('VIC',144.96,-37.81)
    assert parsed.revision==replace(parsed,state='',lon=None,lat=None).revision


@pytest.mark.asyncio
@pytest.mark.parametrize('service',['rfs','traffic'])
async def test_outside_jurisdiction_does_not_queue_even_with_all_nsw(service):
    db,radio=prepare(service,False)
    db.set_setting(KEY,configuration(all_nsw=True,include_uncertain=True))
    class Client:
        async def fetch(self):
            return [replace(rfs_item() if service=='rfs' else traffic_item(),state='VIC')]
        async def boundaries(self): return []
    poller=RFSPoller(db,radio,Client()) if service=='rfs' else TrafficPoller(db,radio,Client())
    await poller.poll_once()
    assert not radio.pending
    assert 'outside NSW' in db.query_service_history(source=service)[0]['detail']
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('service',['rfs','traffic'])
async def test_metadata_only_state_change_invalidates_queued_notice(service):
    db,radio=prepare(service,False)
    original=rfs_item() if service=='rfs' else traffic_item()
    class Client:
        async def fetch(self): return [original]
        async def boundaries(self): return []
    poller=RFSPoller(db,radio,Client()) if service=='rfs' else TrafficPoller(db,radio,Client())
    await poller.poll_once()
    assert radio.pending
    guard=radio.pending[0][2]
    assert guard()
    updated=replace(original,state='VIC')
    assert updated.revision==original.revision
    if service=='rfs': db.rfs_save_incident(updated,updated.revision)
    else: db.traffic_save_item(updated,'',True)
    assert not guard()
    db.close()


@pytest.mark.parametrize('service',['rfs','traffic'])
def test_closure_reason_agrees_in_preview_current_diagnostics_and_history(service):
    db,_=prepare(service,False)
    original=rfs_item() if service=='rfs' else traffic_item()
    notice_id=original.incident_id if service=='rfs' else original.item_id
    db.add_service_history(service,notice_id,'Previously sent','',transmit_status='success')
    db.set_setting(KEY,configuration(location_terms=['Elsewhere']))
    closure=replace(original,status='Out') if service=='rfs' else replace(original,ended=True)
    if service=='rfs':
        db.rfs_save_incident(closure,closure.revision)
        coverage=incident_delivery_coverage(closure,db.all_settings(),db)
        db.rfs_add_history(closure,'Closure message','dry-run')
    else:
        db.traffic_save_item(closure,'',False)
        coverage=traffic_delivery_coverage(closure,'',db.all_settings(),db)
        TrafficPoller(db,SimpleNamespace(message_budget=126))._history(closure,'',text='Closure message',status='dry-run')
    assert coverage.included and coverage.method=='previous transmission'
    decision=saved_decisions(db,db.all_settings(),(service,))[0]['coverage']
    assert decision.reason==coverage.reason
    history=db.latest_service_history(service,notice_id)
    assert coverage.reason==history['detail']
    response=TestClient(web_app(db)).post('/settings/geography',data={'action':'preview','location_terms':'Elsewhere'})
    assert response.status_code==200 and coverage.reason in response.text
    # Explicitly interstate closures cannot override jurisdiction restrictions.
    other=replace(closure,state='VIC')
    result=incident_delivery_coverage(other,db.all_settings(),db) if service=='rfs' else traffic_delivery_coverage(other,'',db.all_settings(),db)
    assert not result.included
    db.close()


@pytest.mark.asyncio
async def test_boundaries_retry_without_restart_or_repeated_errors():
    db,radio=prepare('traffic')
    # NSW is explicit, allowing term matching while council boundaries fail.
    class Client:
        attempts=0
        async def fetch(self): return [traffic_item()]
        async def boundaries(self):
            self.attempts+=1
            if self.attempts<3: raise TrafficFeedError('temporary outage')
            return [{'type':'Feature','properties':{'lganame':'Central Coast'},'geometry':{
                'type':'Polygon','coordinates':[[[150,-34],[152,-34],[152,-32],[150,-32],[150,-34]]]}}]
    client=Client()
    poller=TrafficPoller(db,radio,client)
    await poller.poll_once()
    assert poller._councils is None
    first_errors=db._conn.execute("SELECT COUNT(*) FROM errors WHERE source='traffic'").fetchone()[0]
    await poller.poll_once()
    assert poller._councils is None
    assert db._conn.execute("SELECT COUNT(*) FROM errors WHERE source='traffic'").fetchone()[0]==first_errors
    await poller.poll_once()
    assert poller._councils and client.attempts==3
    assert not poller._boundary_error
    assert 'recovered' in db._conn.execute("SELECT message FROM events ORDER BY id DESC LIMIT 1").fetchone()[0].lower()
    await poller.poll_once()
    assert client.attempts==3
    db.close()


def test_traffic_parser_preserves_interstate_coordinates_and_provider_state():
    import time
    from app.traffic.feed import parse_feed
    payload={'type':'FeatureCollection','lastPublished':int(time.time()*1000),'features':[
        {'id':1,'geometry':{'type':'Point','coordinates':[115.86,-31.95]},
         'properties':{'stateCode':'WA','mainCategory':'HAZARD','roads':[{'mainStreet':'Moonbi Road'}]}}]}
    item=parse_feed('incident',payload)[0]
    assert (item.lon,item.lat,item.state)==(115.86,-31.95,'WA')
    assert not traffic_coverage(item,'',{KEY:configuration(all_nsw=True,include_uncertain=True)}).included


def test_bom_cancellation_diagnostics_follow_referenced_sent_warning():
    from app.models import Alert
    from app.bom_area import CouncilMatch
    from app.geography import bom_delivery_coverage
    db=Database(':memory:')
    db.upsert_state(alert_id='previous',event='Flood Warning',headline='',expires='',msg_hash='old',
                    disposition='sent',sent_ts='2026-10-05T00:00:00Z')
    item=Alert('cancel','Flood Warning','','Hunter','','','Cancel',references=['previous'])
    result=bom_delivery_coverage(item,CouncilMatch('unknown'),{KEY:configuration(location_terms=['Elsewhere'])},db)
    assert result.included and result.method=='previous transmission'
    assert 'cancellation' in result.reason
    db.close()


@pytest.mark.asyncio
async def test_ended_traffic_closure_is_available_in_troubleshooting_preview():
    from fastapi import FastAPI
    from app.troubleshooting import Troubleshooting
    db,radio=prepare('traffic',False)
    closure=replace(traffic_item(),ended=True)
    db.add_service_history('traffic',closure.item_id,'Previously sent','',transmit_status='success')
    db.traffic_save_item(closure,'',False)
    db.set_setting(KEY,configuration(location_terms=['Elsewhere']))
    app=FastAPI()
    app.state.db,app.state.tx=db,radio
    app.state.traffic_poller=TrafficPoller(db,radio)
    app.state.traffic_poller._councils=[]
    preview=await Troubleshooting(app).preview('traffic','resend')
    assert not preview['errors'] and preview['notices']==1
    assert 'ENDED' in preview['details'][0]['text']
    assert not radio.pending
    db.close()
