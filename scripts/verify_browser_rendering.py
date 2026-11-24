"""Check rendering and intentional downloads in fresh Firefox/Chromium contexts.

Requires Playwright and its Chromium/Firefox binaries. Artifacts must be kept
outside the published repository. Run against local HTTP first, then live Pages.
"""
import argparse
import hashlib
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def check(engine, url, html_bytes, email_bytes, output):
    browser = engine.launch()
    result = {'browser': engine.name, 'version': browser.version, 'visits': []}
    try:
        for mode in ('direct', 'clicked-link'):
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
            page = context.new_page()
            downloads, email_requests, errors = [], [], []
            page.on('download', lambda d: downloads.append(d))
            page.on('request', lambda r: email_requests.append(r.url) if '.eml' in r.url else None)
            page.on('pageerror', lambda e: errors.append(type(e).__name__))
            if mode == 'direct':
                response = page.goto(url, wait_until='networkidle')
            else:
                page.set_content('<a id="dossier-link">Open public dossier</a>')
                page.locator('#dossier-link').evaluate('(a,url)=>a.href=url', url)
                with page.expect_navigation(wait_until='networkidle') as navigation:
                    page.locator('#dossier-link').click()
                response = navigation.value
            # Catch subframe downloads that start after the main document arrives.
            page.wait_for_timeout(1500)
            assert response.status == 200
            assert 'text/html' in response.headers.get('content-type', '')
            assert 'attachment' not in response.headers.get('content-disposition', '').lower()
            assert digest(response.body()) == digest(html_bytes), 'Deployed HTML differs from expected source'
            expect(page.locator('header h1')).to_be_visible()
            assert not downloads, 'Unexpected automatic download: ' + ', '.join(d.suggested_filename for d in downloads)
            assert not email_requests, 'Original email requested automatically'
            assert not errors, 'Page JavaScript error'
            visit = {'mode': mode, 'status': response.status,
                     'content_type': response.headers['content-type'], 'html_sha256': digest(response.body()),
                     'automatic_downloads': len(downloads), 'automatic_email_requests': len(email_requests)}
            if mode == 'direct':
                page.screenshot(path=str(output / f'{engine.name}-desktop.png'))
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path=str(output / f'{engine.name}-mobile.png'))
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.locator('#om-original-viewer summary').click()
                expect(page.locator('#om-original-viewer')).to_have_attribute('data-loaded', 'true')
                text = page.locator('#om-original-source').text_content()
                assert digest(text.encode()) == digest(email_bytes), 'Email text differs from preserved original'
                assert not downloads, 'Opening text viewer caused a download'
                with page.expect_download() as requested:
                    page.get_by_role('link', name='Download original EML', exact=False).click()
                download = requested.value
                assert digest(Path(download.path()).read_bytes()) == digest(email_bytes)
                assert len(downloads) == 1
                assert page.url == url
                visit.update(email_viewer='passed', explicit_original_download='passed', original_sha256=digest(email_bytes))
            result['visits'].append(visit)
            context.close()
    finally:
        browser.close()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url')
    parser.add_argument('--local-root', type=Path)
    parser.add_argument('--html', type=Path, required=True)
    parser.add_argument('--email', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    server = None
    if args.local_root:
        handler = partial(SimpleHTTPRequestHandler, directory=str(args.local_root))
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        args.url = f'http://127.0.0.1:{server.server_port}/'
    if not args.url:
        parser.error('Supply --url or --local-root')
    try:
        with sync_playwright() as p:
            results = [check(engine, args.url, args.html.read_bytes(), args.email.read_bytes(), args.output)
                       for engine in (p.chromium, p.firefox)]
        report = {'url': args.url, 'results': results}
        (args.output / 'rendering-results.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        if server:
            server.shutdown()


if __name__ == '__main__':
    main()
