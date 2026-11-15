"""Optional Playwright BOM scope/settings checks; uses no live feeds or radio.

PYTHONPATH=. python tests/browser_bom_scope.py --output /tmp/bom-scope-browser
Set PLAYWRIGHT_CHROMIUM_EXECUTABLE or install Chromium with Playwright.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from test_troubleshooting import environment
from test_bom_scope import run
from app.geography import KEY, configuration
from app.web.routes import router

parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path('/tmp/noticeecho-bom-scope-browser'))
root=parser.parse_args().output;root.mkdir(parents=True,exist_ok=True)
fixture=environment.__wrapped__();app=next(fixture);db=app.state.db
app.include_router(router);client=TestClient(app)
db.set_setting(KEY,configuration(location_terms=['Tamworth','Quirindi']))
asyncio.run(run(db))
asyncio.run(run(db,summary='Destructive winds expected.'))
assert not asyncio.run(run(db,locations='Tamworth, Quirindi, Sydney',summary='Destructive winds expected.'))[0]
asyncio.run(run(db,locations='Illawarra',summary='The warning for Sydney is CANCELLED.'))
asyncio.run(run(db,locations='Illawarra',cancel=True))
results=[];errors=[]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'),headless=True,args=['--no-sandbox'])
 page=browser.new_page();page.on('pageerror',lambda exc:errors.append(str(exc)))
 def local(route):
  req=route.request;url=urlsplit(req.url)
  if url.hostname!='noticeecho.test':route.abort();return
  response=client.request(req.method,url.path+('?' +url.query if url.query else ''),content=req.post_data,
                          headers={'content-type':req.headers.get('content-type','')},follow_redirects=True)
  route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','text/html'))
 page.route('**/*',local)
 for theme in ('dark','light'):
  db.set_setting('appearance',theme)
  for width in (390,1440):
   page.set_viewport_size({'width':width,'height':1000})
   for path,name in [('/settings/bom','settings'),('/history?source=bom&records=all','history')]:
    changes=db._conn.total_changes
    page.goto('http://noticeecho.test'+path,wait_until='networkidle')
    assert db._conn.total_changes==changes
    if name=='settings':
     field=page.get_by_role('checkbox',name='Send cancellation notifications',exact=True)
     assert field.is_checked()
     field.uncheck();page.get_by_role('button',name='Save monitoring and polling',exact=True).click()
     page.wait_for_load_state('networkidle');assert db.get_setting('bom_send_cancellations') is False
     assert not field.is_checked()
     field.check();page.get_by_role('button',name='Save monitoring and polling',exact=True).click()
     page.wait_for_load_state('networkidle');assert db.get_setting('bom_send_cancellations') is True
    else:
     page.get_by_text('BOM geographic scope decision',exact=True).evaluate_all('(items)=>items.forEach(item=>item.parentElement.open=true)')
     body=page.inner_text('body')
     assert 'Tamworth' in body and 'Quirindi' in body
     assert 'No previously announced selected scope matches this cancellation' in body
     assert 'Cancelled announced scope: Tamworth' in body
     assert 'Automatic updates compare information conveyed for selected locations' in body
     assert 'New or changed information: Tamworth' in body
     texts=page.locator('.rec-msg').all_text_contents()
     cancellation=[text for text in texts if 'CANCELLED' in text]
     assert len(cancellation)==1 and 'Sydney' not in cancellation[0] and 'Illawarra' not in cancellation[0]
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(root/f'{name}-{theme}-{width}.png'),full_page=True)
    results.append(dict(page=name,theme=theme,width=width))
 assert not errors,errors
 assert not app.state.tx.sent
 browser.close()
fixture.close()
(root/'results.json').write_text(json.dumps(dict(layouts=results,javascript_errors=errors),indent=2))
print(json.dumps(dict(layouts=len(results),javascript_errors=errors,radio_submissions=0,artifacts=str(root))))
