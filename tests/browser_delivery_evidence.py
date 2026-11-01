"""Optional browser acceptance/evidence checks using an isolated database and mock radio.

PYTHONPATH=. python tests/browser_delivery_evidence.py --output /tmp/delivery-browser
Requires pytest, pytest-asyncio, respx and playwright. Install Chromium using
python -m playwright install chromium, or set PLAYWRIGHT_CHROMIUM_EXECUTABLE.
"""
import argparse
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from urllib.parse import urlsplit
import json
from playwright.sync_api import sync_playwright
from fastapi.testclient import TestClient
from test_troubleshooting import environment
from test_transmission_reliability import make_radio,manager
from app.web.routes import router
from app.transmission import TxResult
fixture=environment.__wrapped__();app=next(fixture);db=app.state.db
radio,commands=make_radio(counter='flat');tx,t=manager(db,radio);t.target='/dev/test';app.state.tx=tx
app.include_router(router);client=TestClient(app)
for outcome in ('accepted','local_confirmed','repeat_confirmed','unconfirmed'):
 row=db.add_service_history('rfs',outcome,title='Test '+outcome,transmit_status='queued',transmitted_text='Unchanged warning text '+outcome)
 result=TxResult(outcome,submitted=True,counter_status='timed_out',counter_error='Statistics request timed out; reconnect before using counter evidence')
 result.log_id=db.begin_transmission(0,'Unchanged warning text '+outcome,'meshcore',False,(row,0,1));db.finish_transmission(result);db.update_service_history(row,'success')
db.add_service_history('rfs','legacy','Legacy warning','Example',transmit_status='success',transmitted_text='Legacy message text')
db._conn.execute("INSERT INTO transmit_log (ts,channel,byte_count,success,text,manual,transport,outcome,evidence) VALUES ('2026-01-01',0,3,1,'old',0,'meshcore','local_confirmed','{}')")
db._conn.commit()
parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=Path('/tmp/noticeecho-delivery-browser'))
root=parser.parse_args().output;root.mkdir(parents=True,exist_ok=True)
results=[];errors=[]
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'),headless=True,args=['--no-sandbox'])
 page=browser.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
 def local(route):
  req=route.request;url=urlsplit(req.url)
  if url.hostname!='noticeecho.test':route.abort();return
  response=client.request(req.method,url.path+('?' +url.query if url.query else ''),content=req.post_data,headers={'content-type':req.headers.get('content-type','')})
  route.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','text/html'))
 page.route('**/*',local)
 for theme in ('dark','light'):
  db.set_setting('appearance',theme)
  for width in (390,1440):
   page.set_viewport_size({'width':width,'height':1000})
   for path,name in [('/','dashboard'),('/history','history'),('/transmit-log','transmit-log'),('/settings/meshcore','settings'),('/troubleshoot','diagnostics')]:
    page.goto('http://noticeecho.test'+path,wait_until='networkidle')
    assert page.locator('html').get_attribute('data-appearance')==theme
    if name in ('history','dashboard'):
     assert 'accepted by companion' in page.inner_text('body').lower()
     assert 'Completed — legacy success (RF evidence not recorded)' in page.inner_text('body')
    if name=='transmit-log':
     assert page.locator('.pill.muted',has_text='Accepted by companion').count()==1
     assert 'Completed — legacy success (RF evidence not recorded)' in page.inner_text('body')
     page.locator('.rec',has_text='Unchanged warning text unconfirmed').locator('details summary').click()
     assert 'Statistics request timed out' in page.inner_text('body')
    if name=='settings':
     assert 'Retry once when the send command outcome is unknown' in page.inner_text('body')
     assert not page.locator('input[name="meshcore_retry_unconfirmed"]').is_checked()
    if name=='diagnostics':
     page.locator('#statistics-check').click()
     page.wait_for_function("document.getElementById('statistics-result').textContent.includes('flood_tx: 10')")
     assert 'Usable for verification: yes' in page.locator('#statistics-result').inner_text()
     tx._active_notice=object();page.locator('#statistics-check').click()
     page.wait_for_function("document.getElementById('statistics-result').textContent.toLowerCase().includes('busy')")
     tx._active_notice=None
    metrics=page.evaluate('({width:innerWidth,content:document.documentElement.scrollWidth})')
    
    if metrics['content']>metrics['width']:
     print(page.evaluate("Array.from(document.querySelectorAll('body *')).filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>[e.tagName,e.className,e.getBoundingClientRect().width,e.textContent.slice(0,90)]).slice(0,20)"),flush=True)
     page.screenshot(path=str(root/'overflow.png'),full_page=True)
    assert metrics['content']<=metrics['width'],(theme,width,name,metrics)
    page.screenshot(path=str(root/f'{name}-{theme}-{width}.png'),full_page=True)
    results.append({'page':name,'theme':theme,'width':width,'no_horizontal_overflow':True})
 assert not errors,errors
 assert not commands.calls,commands.calls
 browser.close()
fixture.close()
(root/'results.json').write_text(json.dumps({'layouts':results,'javascript_errors':errors,'channel_messages_sent':0},indent=2))
print(json.dumps({'layouts':len(results),'javascript_errors':errors,'channel_messages_sent':0,'artifacts':str(root)},indent=2))
