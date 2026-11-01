"""Optional real-browser terminal checks; run with pytest + playwright installed.

    PYTHONPATH=. python tests/browser_companion_terminal.py --output /tmp/terminal-check

Install Chromium with `python -m playwright install chromium`, or set
PLAYWRIGHT_CHROMIUM_EXECUTABLE to an existing browser. No radio is used.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from test_meshcore_settings import _web_client


def check(output_dir):
    client, radio = _web_client()
    original = radio.get_device_settings
    responses = {'get name': 'Weather node', 'get multi': 'alpha\r\nbeta\n\ngamma\r\n',
                 'get empty': '', 'get newline': 'One line\n',
                 'get long': 'LONG' * 500,
                 'get bulk': '\n'.join(f'row {index}' for index in range(110)),
                 'get html': '<img src=x onerror="window.terminalXss=1">'}

    async def settings():
        return dict(await original(), cli_supported=True)

    async def execute(command):
        return dict(output=responses.get(command, 'Response ' + command), state='reply', refresh_required=False)

    radio.get_device_settings = settings
    radio.execute_companion_command = execute
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as playwright:
        options = dict(headless=True)
        if os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
            options['executable_path'] = os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
        browser = playwright.chromium.launch(**options)
        page = browser.new_page(timezone_id='Australia/Sydney')
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.clock.install(time=datetime(2026, 11, 1, 3, 4, 5, tzinfo=timezone.utc))

        def local(route):
            request = route.request
            url = urlsplit(request.url)
            if url.hostname != 'noticeecho.test':
                route.abort()
                return
            response = client.request(request.method, url.path, content=request.post_data,
                                      headers={'content-type': request.headers.get('content-type', '')})
            route.fulfill(status=response.status_code, body=response.content,
                          content_type=response.headers.get('content-type', 'text/html'))

        page.route('**/*', local)
        page.goto('http://noticeecho.test/meshcore/settings', wait_until='networkidle')
        terminal = page.locator('#console-output')
        field = page.locator('#console-command')

        def submit(command):
            field.fill(command)
            field.press('Enter')
            page.wait_for_function("!document.getElementById('console-run').disabled")
            return terminal.text_content()

        def clear():
            page.locator('#console-clear').click()
            assert terminal.text_content() == ''

        def metrics():
            return terminal.evaluate('''element => {
                const s = getComputedStyle(element);
                return {rows: (element.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom)) / parseFloat(s.lineHeight),
                        lineHeight: parseFloat(s.lineHeight), top: element.scrollTop,
                        bottom: element.scrollHeight - element.clientHeight,
                        width: element.clientWidth, contentWidth: element.scrollWidth,
                        color: s.color, background: s.backgroundColor};
            }''')

        assert terminal.evaluate("element => !!(element.compareDocumentPosition(document.getElementById('console-command')) & Node.DOCUMENT_POSITION_FOLLOWING)")
        text = submit('get name')
        lines = text.splitlines()
        assert len(lines) == 4 and re.fullmatch(r'\d{2}:\d{2}:\d{2}', lines[0]), lines
        assert lines[1:] == ['> get name', 'Weather node', '']
        clear()
        text = submit('get multi')
        assert responses['get multi'] in text and len(text.splitlines()) == 7
        clear()
        assert submit('get empty').splitlines()[1:] == ['> get empty', '', '']
        clear()
        assert len(submit('get newline').splitlines()) == 4
        clear()
        for index in range(26):
            text = submit(f'get sample{index:02}')
            assert len(text.splitlines()) <= 100
        assert len(text.splitlines()) == 100
        assert '> get sample00\n' not in text and '> get sample01\n' in text
        assert abs(metrics()['top'] - metrics()['bottom']) <= 2
        terminal.evaluate('element => { element.scrollTop = 168; }')
        before = metrics()
        anchor = text.splitlines()[round(before['top'] / before['lineHeight'])]
        text = submit('get newer')
        after = metrics()
        assert abs(after['top'] - max(0, before['top'] - 4 * before['lineHeight'])) <= 2, (before, after)
        assert text.splitlines()[round(after['top'] / after['lineHeight'])] == anchor
        assert after['top'] < after['bottom']
        clear()
        text = submit('get bulk')
        assert len(text.splitlines()) == 100 and text.startswith('row 11\n') and text.endswith('row 109\n\n')
        assert len(submit('get html').splitlines()) == 100
        assert page.locator('#console-output img').count() == 0 and not page.evaluate('!!window.terminalXss')
        clear()
        for theme in ('dark', 'light'):
            page.evaluate("theme => document.documentElement.setAttribute('data-appearance', theme)", theme)
            for width in (390, 1440):
                page.set_viewport_size({'width': width, 'height': 1100})
                for command in ('get name', 'get long'):
                    submit(command)
                    # Allow the observer to account for non-overlay scrollbars.
                    page.wait_for_function('''() => {
                        const e = document.getElementById('console-output'), s = getComputedStyle(e);
                        return Math.abs((e.clientHeight - parseFloat(s.paddingTop) - parseFloat(s.paddingBottom)) / parseFloat(s.lineHeight) - 16) < .1;
                    }''')
                    m = metrics()
                    assert abs(m['rows'] - 16) < .1, m
                    assert m['background'] == 'rgb(8, 14, 22)'
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    if command == 'get long':
                        assert m['contentWidth'] > m['width']  # Long lines scroll inside the terminal.
                page.locator('#companion-console').screenshot(path=str(output_dir / f'terminal-{theme}-{width}.png'))
                results.append(dict(theme=theme, width=width, visible_rows=metrics()['rows'], no_page_overflow=True))
        assert not errors, errors
        clear()
        assert metrics()['top'] == 0 and abs(metrics()['rows'] - 16) < .1
        field.press('ArrowUp')
        assert field.input_value() == 'get long'  # Clear output does not clear command recall.
        browser.close()
    report = dict(layouts=results, buffer_limit=100,
                  checks=['four-line exchanges and timestamp', 'multiline/CRLF preservation', 'empty replies',
                          '100-line rollover', 'scroll position and anchor preservation', 'oversized response tail',
                          'HTML-safe text', '16 visible rows with long lines', 'clear and command recall'],
                  javascript_errors=errors)
    (output_dir / 'results.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('/tmp/noticeecho-companion-terminal'))
    check(parser.parse_args().output)
