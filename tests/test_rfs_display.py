"""RFS web headings use saved incident fields, independently of radio wording."""
from copy import deepcopy
from html import unescape
import re
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.db import Database
from app.presentation import rfs_notice_display
from app.rfs.feed import Incident
from app.rfs.web import router as rfs_router
from app.web.routes import router
from test_web_dashboard import DashboardPoller, DashboardTx


@pytest.mark.parametrize('level', ['Not Applicable', ' NOT  APPLICABLE: ', 'not-applicable',
                                  'N/A', 'Unknown', 'Unrecognized', 'Unrecognised.',
                                  'Not supplied', 'Other', '', None])
def test_placeholder_levels_are_details_not_headings(level):
    result = rfs_notice_display('MANILLA RD, ATTUNGA', {'level': level, 'kind': 'Haystack fire', 'status': 'Under control'})
    assert result['heading'] == 'Haystack fire · MANILLA RD, ATTUNGA'
    assert result['alert_level'] == (str(level).strip() if level else 'Not supplied')
    assert result['status'] == 'Under control'


@pytest.mark.parametrize('level', ['Emergency Warning', 'Watch and Act', 'Advice', 'Planned Burn', 'Novel evacuation level'])
def test_meaningful_levels_remain_prominent(level):
    assert rfs_notice_display('Example Road', {'level': level, 'kind': 'Grass Fire'})['heading'] == level + ' · Grass Fire · Example Road'


@pytest.mark.parametrize('title,meta,expected', [
    ('Road', {'kind': 'Unknown'}, 'Incident · Road'),
    ('Road', {'kind': 'Not Applicable'}, 'Incident · Road'),
    ('', {'kind': 'Fire Alarm', 'location': 'Example Road'}, 'Fire Alarm · Example Road'),
    ('', {}, 'Incident'),
    ('Old incident', None, 'Incident · Old incident'),
    ('Old incident', [], 'Incident · Old incident'),
    ('Grass-Fire: Road', {'kind': 'Grass Fire'}, 'Grass-Fire: Road'),
    ('Fireball Road', {'kind': 'Fire'}, 'Fire · Fireball Road'),
    ('Grass Fire', {'kind': 'Fire'}, 'Fire · Grass Fire'),
    ('Unknown Road, Other Town', {'kind': 'Unfamiliar response'}, 'Unfamiliar response · Unknown Road, Other Town'),
    ('Incident Road', {}, 'Incident Road'),
])
def test_fallbacks_and_duplicate_boundaries(title, meta, expected):
    original = deepcopy(meta)
    assert rfs_notice_display(title, meta)['heading'] == expected
    assert meta == original


@pytest.fixture
def display_app():
    db = Database(':memory:')
    app = FastAPI()
    app.include_router(router)
    app.include_router(rfs_router)
    app.state.db = db
    app.state.tx = DashboardTx()
    app.state.poller = DashboardPoller()
    app.state.rfs_poller = SimpleNamespace(last_poll='', last_result='not polled')
    yield app, TestClient(app)
    db.close()


def headings(html, css_class='rec-event'):
    return [unescape(re.sub('<[^>]+>', '', text)).strip() for text in re.findall(
        r'<div class="' + css_class + r'">(.*?)</div>', html, re.S)]


def add_notice(db, status='dry-run', **metadata):
    db.add_service_history('rfs', 'rfs-' + status, 'MANILLA RD, ATTUNGA', 'Tamworth',
                           transmitted_text='Original radio wording: Not Applicable — retained verbatim',
                           transmit_status=status,
                           metadata=dict(level='Not Applicable', kind='Haystack fire', status='Under control') | metadata)


@pytest.mark.parametrize('status,label', [('dry-run', 'dry-run'), ('success', 'Completed — locally confirmed'),
                                         ('unconfirmed', 'Completed — some parts unconfirmed'), ('failed', 'Incomplete — failed')])
def test_history_pages_and_partials_separate_incident_from_delivery(display_app, status, label):
    app, client = display_app
    add_notice(app.state.db, status)
    for url in ['/history', '/history?source=rfs', '/partials/history?source=rfs']:
        response = client.get(url)
        assert response.status_code == 200
        assert headings(response.text) == ['Haystack fire · MANILLA RD, ATTUNGA']
        assert 'Alert level: Not Applicable' in response.text
        assert 'Incident status: Under control' in response.text
        assert label in response.text
        assert 'Original radio wording: Not Applicable — retained verbatim' in response.text


def test_dashboard_heading_and_details_match_history(display_app):
    app, client = display_app
    add_notice(app.state.db, 'success')
    for url in ['/', '/partials/dashboard']:
        response = client.get(url)
        assert response.status_code == 200
        assert headings(response.text, 'ev') == ['NSW RFS Haystack fire · MANILLA RD, ATTUNGA']
        assert 'Alert level: Not Applicable' in response.text
        assert 'Incident status: Under control' in response.text


def test_older_records_other_services_and_html_escaping(display_app):
    app, client = display_app
    db = app.state.db
    db.add_service_history('rfs', 'old', 'Legacy Road', transmitted_text='old text', transmit_status='dry-run')
    db.add_history('bom', 'Flood Warning', 'Hunter', 'sent', 'BOM original', transmit_status='success')
    db.add_service_history('traffic', 'traffic', 'Road closed', transmitted_text='Traffic original', transmit_status='dry-run')
    add_notice(db, kind='<script>alert(1)</script>', level='<b>Novel level</b>')
    page = client.get('/history').text
    assert 'Incident · Legacy Road' in headings(page)
    assert 'Flood Warning' in headings(page) and 'Road closed' in headings(page)
    assert 'Alert level: Not supplied' in page and 'Incident status: Not supplied' in page
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in page
    assert '&lt;b&gt;Novel level&lt;/b&gt;' in page


def test_rendering_preserves_database_and_prepared_and_transmitted_text(display_app):
    app, client = display_app
    db = app.state.db
    add_notice(db, 'success')
    text = 'NSW RFS old TX: Not Applicable — exact text'
    db.add_transmit_log(0, len(text.encode()), True, text)
    tables = ('settings', 'service_history', 'transmit_log', 'rfs_incidents')
    def snapshot():
        return {table: [tuple(row) for row in db._conn.execute('SELECT * FROM ' + table)] for table in tables}
    before = snapshot()
    for url in ['/history', '/partials/history', '/', '/partials/dashboard', '/rfs', '/transmit-log', '/partials/transmit-log']:
        response = client.get(url)
        assert response.status_code == 200
        if 'transmit-log' in url:
            assert text in response.text
    assert snapshot() == before


def test_rfs_source_page_retains_explicit_alert_level_column(display_app):
    app, client = display_app
    event = Incident('source', 'MANILLA RD, ATTUNGA', 'Not Applicable', 'Tamworth',
                     'MANILLA RD, ATTUNGA 2345', 'Under control', 'Haystack fire', '', '')
    app.state.db.rfs_save_incident(event, event.revision)
    response = client.get('/rfs')
    assert response.status_code == 200
    assert '<th>Alert level</th>' in response.text
    assert '<td>Not Applicable</td>' in response.text
    assert 'Haystack fire' in response.text and 'MANILLA RD, ATTUNGA' in response.text
