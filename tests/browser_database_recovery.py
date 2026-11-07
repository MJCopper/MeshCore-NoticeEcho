"""Optional browser recovery diagnostics test. Requires pytest + Playwright.

PYTHONPATH=. python tests/browser_database_recovery.py --output /tmp/database-browser
Set PLAYWRIGHT_CHROMIUM_EXECUTABLE or install Chromium with Playwright.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from test_troubleshooting import environment
from app.web.routes import router

parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path('/tmp/noticeecho-database-browser'))
root=parser.parse_args().output;root.mkdir(parents=True,exist_ok=True)
fixture=environment.__wrapped__();app=next(fixture);db=app.state.db
app.include_router(router);client=TestClient(app)
states={source:dict(source=source,state='waiting',alive=True,ready=True,restarts=0,consecutive_failures=0,last_error='',last_recovery=None,next_attempt=None) for source in ('bom','rfs','traffic')}
for attr,source in [('poller','bom'),('rfs_poller','rfs'),('traffic_poller','traffic')]:
 getattr(app.state,attr).worker=SimpleNamespace(snapshot=lambda source=source:states[source].copy())
failed=[False];original=db.all_settings
results=[];errors=[]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'),headless=True,args=['--no-sandbox'])
 page=browser.new_page();page.on('pageerror',lambda exc:errors.append(str(exc)))
 def local(route):
  req=route.request;url=urlsplit(req.url)
  if url.hostname!='noticeecho.test':route.abort();return
  if failed[0] and url.path=='/troubleshoot/status':
   def unavailable():raise sqlite3.OperationalError('database is locked')
   db.all_settings=unavailable
  try:response=client.request(req.method,url.path,content=req.post_data,headers={'content-type':req.headers.get('content-type','')})
  finally:db.all_settings=original
  route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','text/html'))
 page.route('**/*',local)
 for theme in ('dark','light'):
  db.set_setting('appearance',theme)
  for width in (390,1440):
   page.set_viewport_size({'width':width,'height':1000})
   page.goto('http://noticeecho.test/troubleshoot',wait_until='networkidle')
   page.wait_for_function("document.getElementById('database-runtime').textContent.includes('waiting')")
   panel=page.locator('#database-runtime');panel.locator('..').locator('summary').click()
   for state in ('backoff','waiting'):
    failed[0]=state=='backoff'
    states['bom'].update(state=state,ready=not failed[0],last_error='database is locked' if failed[0] else '')
    page.evaluate('refreshDiagnostics()')
    page.wait_for_function("state=>document.getElementById('database-runtime').textContent.includes(state)",arg=state)
    if failed[0]:
     page.wait_for_function("document.getElementById('diagnostic-summary').textContent.startsWith('Database unavailable')")
     assert client.get('/readyz').status_code==503
    else:
     page.wait_for_function("!document.getElementById('diagnostic-summary').textContent.startsWith('Database unavailable')")
     assert client.get('/readyz').status_code==200
    assert client.get('/healthz').status_code==200
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(root/f'{theme}-{width}-{state}.png'),full_page=True)
    results.append(dict(theme=theme,width=width,state=state))
 assert not errors,errors
 assert not app.state.tx.sent
 browser.close()
fixture.close()
(root/'results.json').write_text(json.dumps(dict(layouts=results,javascript_errors=errors),indent=2))
print(json.dumps(dict(layouts=len(results),javascript_errors=errors,radio_submissions=0,artifacts=str(root))))
