"""One shared list, with reviewable migration of former BOM-only districts."""
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from app.db import Database
from app.geography import KEY, configuration, proposal, evaluate, bom_coverage, incident_coverage, traffic_coverage
from app.models import Alert
from app.bom_area import CouncilMatch
from test_universal_geography import rfs_item, traffic_item, web_app


def old_policy():
    return dict(version=1,active=True,all_nsw=False,councils=[],location_terms=['Moonbi','New-England'],
                bom_districts=['Hunter','new england','Hunter'],include_uncertain=False)


def test_merge_normalises_duplicates_and_preserves_every_unique_entry():
    policy=proposal({KEY:old_policy()})
    assert policy['version']==2
    assert policy['location_terms']==['Moonbi','New-England','Hunter']
    assert 'bom_districts' not in policy
    large=old_policy() | {'location_terms':[f'Place {i}' for i in range(200)],
                          'bom_districts':[f'District {i}' for i in range(200)]}
    assert len(proposal({KEY:large})['location_terms'])==400


def test_district_names_are_shared_across_all_service_location_fields():
    policy=configuration(location_terms=['Hunter'])
    settings={KEY:policy}
    alert=Alert('hunter','Flood Warning','Flood Warning','Hunter','','','Alert')
    assert bom_coverage(alert,CouncilMatch('unknown'),settings).included
    assert incident_coverage(replace(rfs_item(),location='Hunter Road'),settings).included
    assert traffic_coverage(replace(traffic_item(),suburb='Hunter'),'',settings).included
    for service in ('bom','rfs','traffic'):
        assert not evaluate(policy,service,fields=(('location','Hunterville'),)).included


def test_legacy_policy_keeps_bom_only_district_scope_until_saved():
    policy=old_policy()
    assert evaluate(policy,'bom',district_fields=('Hunter',)).included
    assert not evaluate(policy,'rfs',fields=(('location','Hunter Road'),)).included
    merged=proposal({KEY:policy})
    assert evaluate(merged,'rfs',fields=(('location','Hunter Road'),)).included


def test_old_service_districts_populate_the_shared_migration_proposal():
    policy=proposal({'bom_all_councils':False,'bom_councils':['Tamworth Regional'],'bom_districts':['Hunter','hunter']})
    assert policy['location_terms']==['Hunter']
    assert 'bom_districts' not in policy


def test_active_policy_merge_preview_is_read_only_and_reports_broadening():
    db=Database(':memory:')
    previous=old_policy()
    db.set_setting(KEY,previous)
    incident=replace(rfs_item(),location='Hunter Road')
    db.rfs_save_incident(incident,incident.revision)
    client=TestClient(web_app(db))
    settings=db.all_settings()
    page=client.get('/settings/geography')
    assert page.status_code==200
    assert 'Review shared location terms' in page.text
    assert 'name="bom_districts"' not in page.text
    assert 'Moonbi\nNew-England\nHunter' in page.text
    assert 'Coverage changes' in page.text
    assert 'location term “Hunter”' in page.text
    assert db.all_settings()==settings
    preview=client.post('/settings/geography',data={'action':'preview','location_terms':'Moonbi\nNew-England\nHunter'})
    assert preview.status_code==200 and 'Coverage changes' in preview.text
    assert db.all_settings()==settings
    saved=client.post('/settings/geography',data={'action':'save','location_terms':'Moonbi\nNew-England\nHunter'})
    assert saved.status_code==200
    policy=db.get_setting(KEY)
    assert policy['version']==2 and 'bom_districts' not in policy
    assert policy['location_terms']==['Moonbi','New-England','Hunter']
    assert db.get_setting('geographic_policy_before_term_merge')==previous
    assert 'Review shared location terms' not in saved.text
    assert not db.query_service_history()
    assert not client.app.state.tx.pending
    # A subsequently cleared list stays empty; archived districts must not return.
    client.post('/settings/geography',data={'action':'save','location_terms':''})
    assert db.get_setting(KEY)['location_terms']==[]
    assert db.get_setting('geographic_policy_before_term_merge')==previous
    db.close()


@pytest.mark.parametrize('all_nsw',[False,True])
def test_merge_preserves_other_policy_settings(all_nsw):
    previous=old_policy() | {'all_nsw':all_nsw,'councils':['Tamworth Regional'],'include_uncertain':True}
    merged=proposal({KEY:previous})
    for key in ('all_nsw','councils','include_uncertain','active'):
        assert merged[key]==previous[key]
