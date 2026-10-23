"""Configurable minimum safety-warning interval, shared by every service."""
from datetime import datetime,timedelta,timezone
import importlib
import pytest
from app.config import verification_interval_seconds
from app.db import Database
from app.poller import BomPoller
from app.rfs.poller import RFSPoller
from app.traffic.poller import TrafficPoller
from app.transmit import TransmitManager
from test_web_radio_channels import _client

NOW=datetime(2026,10,10,12,0,tzinfo=timezone.utc)

class Clock(datetime):
    @classmethod
    def now(cls,tz=None):return NOW if tz else NOW.replace(tzinfo=None)

class Tx:
    message_budget=126
    def __init__(self):self.calls=[]
    def enqueue_verification(self,text,on_result=None,allow_new=True):
        self.calls.append((text,on_result,allow_new));return allow_new


def queue(source,db,tx,monkeypatch,dry=False,channel=0):
    module=importlib.import_module({'bom':'app.poller','rfs':'app.rfs.poller','traffic':'app.traffic.poller'}[source])
    monkeypatch.setattr(module,'datetime',Clock)
    if source=='bom':BomPoller(db,tx)._queue_verification(channel,dry)
    elif source=='rfs':RFSPoller(db,tx)._queue_verification(dry)
    else:TrafficPoller(db,tx)._queue_verification()

@pytest.mark.parametrize('minutes',[1,5,60,1440])
@pytest.mark.parametrize('offset',[-1,0,1])
@pytest.mark.parametrize('source',['bom','rfs','traffic'])
def test_every_service_respects_exact_minute_boundary(minutes,offset,source,monkeypatch):
    db=Database(':memory:');db.set_setting('safety_warning_interval_minutes',minutes)
    db.set_setting('verification_live_last_ts',{'0':(NOW-timedelta(seconds=minutes*60+offset)).isoformat()})
    tx=Tx();queue(source,db,tx,monkeypatch)
    assert tx.calls[-1][2] is (offset>=0)
    db.close()

@pytest.mark.parametrize('value',[None,'invalid','10',0,-1,1441,True,1.5])
def test_invalid_stored_intervals_use_180_minutes(value):
    assert verification_interval_seconds(value)==180*60

@pytest.mark.parametrize('source',['bom','rfs','traffic'])
def test_changes_take_effect_immediately_and_failure_does_not_start_cooldown(source,monkeypatch):
    db=Database(':memory:');db.set_setting('verification_live_last_ts',{'0':(NOW-timedelta(minutes=10)).isoformat()})
    db.set_setting('safety_warning_interval_minutes',20);tx=Tx()
    queue(source,db,tx,monkeypatch);assert not tx.calls[-1][2]
    db.set_setting('safety_warning_interval_minutes',2)
    queue(source,db,tx,monkeypatch);assert tx.calls[-1][2]
    before=db.get_setting('verification_live_last_ts')
    tx.calls[-1][1](False,'link down')
    assert db.get_setting('verification_live_last_ts')==before
    tx.calls[-1][1](True,'')
    queue(source,db,tx,monkeypatch);assert not tx.calls[-1][2]
    db.close()


def test_all_services_share_one_queued_warning_and_channel_cooldown(monkeypatch):
    db=Database(':memory:');db.set_setting('safety_warning_interval_minutes',15)
    tx=TransmitManager(db)
    for source in ('bom','rfs','traffic'):queue(source,db,tx,monkeypatch)
    assert len(tx._queue)==1 and tx._queue[0].verification
    warning=tx._queue.pop();warning.on_result(True,'')
    for source in ('traffic','rfs','bom'):queue(source,db,tx,monkeypatch)
    assert not tx._queue
    db.set_setting('meshcore_channel',1)
    queue('rfs',db,tx,monkeypatch)
    assert len(tx._queue)==1
    db.close()


def test_custom_dry_run_interval_has_independent_timestamp(monkeypatch):
    db=Database(':memory:');db.set_setting('safety_warning_interval_minutes',30)
    old=(NOW-timedelta(minutes=10)).isoformat()
    db.set_setting('verification_live_last_ts',{'0':old})
    db.set_setting('verification_dry_run_last_ts',{'0':old})
    tx=Tx();queue('bom',db,tx,monkeypatch,dry=True)
    assert not db.recent_events() and not tx.calls
    db.set_setting('safety_warning_interval_minutes',5)
    queue('bom',db,tx,monkeypatch,dry=True)
    assert len(db.recent_events())==1
    assert db.get_setting('verification_live_last_ts')['0']==old
    queue('bom',db,tx,monkeypatch,dry=True)
    assert len(db.recent_events())==1
    queue('bom',db,tx,monkeypatch)
    assert tx.calls[-1][2]
    db.close()

@pytest.mark.parametrize('minutes',[1,15,1440])
def test_general_setting_saves_and_renders(minutes):
    client,db,tx=_client({'dry_run':True,'display_timezone':'Australia/Sydney'})
    page=client.get('/settings/general').text
    assert 'name="safety_warning_interval_minutes"' in page
    assert 'value="180"' in page and 'not sent during idle periods' in page
    response=client.post('/settings/general',data={'display_timezone':'Australia/Perth','dry_run':'1','safety_warning_interval_minutes':str(minutes)},follow_redirects=False)
    assert response.status_code==303
    assert db.get_setting('safety_warning_interval_minutes')==minutes
    assert f'value="{minutes}"' in client.get('/settings/general').text

@pytest.mark.parametrize('value',['0','-1','1441','1.5','invalid'])
def test_invalid_form_rejected_without_changing_other_settings(value):
    client,db,_=_client({'dry_run':True,'display_timezone':'Australia/Sydney','safety_warning_interval_minutes':10})
    before=db.all_settings()
    response=client.post('/settings/general',data={'display_timezone':'Australia/Perth','safety_warning_interval_minutes':value})
    assert response.status_code==422 and db.all_settings()==before


def test_older_open_general_form_preserves_custom_interval():
    client,db,_=_client({'safety_warning_interval_minutes':20})
    assert client.post('/settings/general',data={'display_timezone':'Australia/Perth'},follow_redirects=False).status_code==303
    assert db.get_setting('safety_warning_interval_minutes')==20


def test_existing_database_seeds_default_without_changing_send_timestamps(tmp_path):
    path=str(tmp_path/'old.db');db=Database(path)
    db.set_setting('verification_live_last_ts',{'0':NOW.isoformat()})
    with db._lock:
        db._conn.execute("DELETE FROM settings WHERE key='safety_warning_interval_minutes'");db._conn.commit()
    db.close();db=Database(path)
    assert db.get_setting('safety_warning_interval_minutes')==180
    assert db.get_setting('verification_live_last_ts')=={'0':NOW.isoformat()}
    db.close()


@pytest.mark.parametrize('source', ['bom', 'rfs', 'traffic'])
def test_custom_warning_is_used_by_each_service(source, monkeypatch):
    db = Database(':memory:')
    db.set_setting('safety_warning_text', 'Custom warning: verify official sources.')
    tx = Tx()
    queue(source, db, tx, monkeypatch)
    assert tx.calls[-1][0] == 'Custom warning: verify official sources.'
    tx.message_budget = 10
    tx.calls.clear()
    queue(source, db, tx, monkeypatch)
    assert tx.calls == []
    db.close()


@pytest.mark.parametrize('text', ['x' * 126, 'é' * 63, '🙂' * 31 + 'ab'])
def test_warning_text_saves_at_utf8_limit(text):
    client, db, _ = _client({})
    response = client.post('/settings/general', data={'safety_warning_text': text}, follow_redirects=False)
    assert response.status_code == 303
    assert db.get_setting('safety_warning_text') == text
    assert text in client.get('/settings/general').text
    client.post('/settings/general', data={}, follow_redirects=False)
    assert db.get_setting('safety_warning_text') == text


@pytest.mark.parametrize('text', ['', '   ', 'x' * 127, 'é' * 64, '🙂' * 32])
def test_invalid_warning_rejected_before_other_settings_change(text):
    client, db, _ = _client({'safety_warning_text': 'Saved warning', 'appearance': 'dark'})
    before = db.all_settings()
    response = client.post('/settings/general', data={'safety_warning_text': text, 'appearance': 'light'})
    assert response.status_code == 422
    assert db.all_settings() == before


def test_warning_default_and_database_persistence(tmp_path):
    from app.config import FINAL_VERIFICATION_MESSAGE, safety_warning_text
    client, _, _ = _client({})
    assert FINAL_VERIFICATION_MESSAGE in client.get('/settings/general').text
    for invalid in (None, '', '   ', 12, 'é' * 64):
        assert safety_warning_text(invalid) == FINAL_VERIFICATION_MESSAGE
    path = str(tmp_path / 'warning.db')
    db = Database(path)
    assert db.get_setting('safety_warning_text') == FINAL_VERIFICATION_MESSAGE
    db.set_setting('safety_warning_text', 'Verify official sources.')
    db.close()
    db = Database(path)
    assert db.get_setting('safety_warning_text') == 'Verify official sources.'
    db.close()
