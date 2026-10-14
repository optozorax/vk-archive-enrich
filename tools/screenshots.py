#!/usr/bin/env python3
"""
Regenerates the README screenshots from a FAKE demo archive (no real person's data):

  1. tools/make_demo_archive.py writes a made-up VK export plus made-up "already downloaded" data;
  2. vk_archive_enrich.py renders it offline (--offline --github-activity);
  3. headless Chromium (Playwright) photographs the important pages, before and after.

The program window is photographed separately on Windows: tools/screenshot_window.py.

VK's own style.css is not part of this repository, so pass the one from your real export
(it only affects how the pages look):

    pip install playwright pillow
    python -m playwright install chromium
    python tools/screenshots.py --style "path/to/Archive/style.css"
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def run(cmd):
    print('$', ' '.join(cmd), flush=True)
    subprocess.run(cmd, check=True)


EMOJI_URL = 'https://github.com/googlefonts/noto-emoji/raw/v2.051/fonts/NotoColorEmoji.ttf'


def emoji_fontconfig(work, font=None):
    """Linux browsers often have no emoji font: give only the screenshot browser one (Noto Color Emoji,
    OFL), through a private fontconfig file. Windows and macOS have their own emoji fonts."""
    if sys.platform != 'linux':
        return {}
    if not font:
        font = os.path.join(os.path.expanduser('~'), '.cache', 'vk_archive_enrich', 'NotoColorEmoji.ttf')
        if not os.path.exists(font):
            import urllib.request
            os.makedirs(os.path.dirname(font), exist_ok=True)
            print('downloading the emoji font (once):', EMOJI_URL)
            urllib.request.urlretrieve(EMOJI_URL, font + '.part')
            os.replace(font + '.part', font)
    conf = os.path.join(work, 'fonts.conf')
    with open(conf, 'w') as f:
        f.write('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
                '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
                '  <dir>%s</dir>\n</fontconfig>\n' % os.path.dirname(os.path.abspath(font)))
    return {'FONTCONFIG_FILE': conf}


def richest_conversation(enriched):
    """The conversation whose newest page shows the most kinds of rendered content."""
    best, best_score = None, -1
    msgs = os.path.join(enriched, 'messages')
    for peer in os.listdir(msgs):
        page = os.path.join(msgs, peer, 'messages0.html')
        if not os.path.isfile(page):
            continue
        t = open(page, encoding='utf-8').read()
        kinds = ['ex-gallery', 'ex-fwd"', 'ex-voice', 'ex-sticker', 'ex-link', 'ex-video', 'ex-reply', 'ex-poll', 'ex-wall']
        score = sum(10 for k in kinds if k in t) + len(os.listdir(os.path.join(msgs, peer)))
        if score > best_score:
            best, best_score = peer, score
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--style', required=True, help="style.css from a real VK export")
    ap.add_argument('--work', help='where to build the demo archive (default: a temporary folder)')
    ap.add_argument('--out', default=os.path.join(ROOT, 'docs', 'screenshots'), help='where to save the PNGs')
    ap.add_argument('--chromium', help='use this Chromium binary instead of the one Playwright installed')
    ap.add_argument('--query', default='завтра', help='what to search for on the search screenshot')
    ap.add_argument('--keep', action='store_true', help='keep the work folder')
    ap.add_argument('--emoji-font', help='color emoji font for the browser on Linux (default: Noto Color Emoji, downloaded once)')
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright
    from PIL import Image

    work = a.work or tempfile.mkdtemp(prefix='vkae_demo_')
    os.makedirs(a.out, exist_ok=True)
    try:
        run([sys.executable, os.path.join(ROOT, 'tools', 'make_demo_archive.py'), work, '--style', a.style])
        src, dst = os.path.join(work, 'archive'), os.path.join(work, 'archive_enriched')
        run([sys.executable, os.path.join(ROOT, 'vk_archive_enrich.py'), src, dst, '--offline', '--github-activity'])
        peer = richest_conversation(dst)
        print('conversation used for the screenshots:', peer)

        # (file name, page, height of the shot, element to wait for)
        shots = [
            ('before-index', os.path.join(src, 'index.html'), 980, None),
            ('before-chat', os.path.join(src, 'messages', peer, 'messages0.html'), 1300, None),
            ('after-index', os.path.join(dst, 'index.html'), 980, None),
            ('after-chats', os.path.join(dst, 'messages', 'index-messages.html'), 1150, '.ex-peer'),
            ('after-chat', os.path.join(dst, 'messages', peer, 'messages0.html'), 1700, '.ex-pg-select'),
            ('after-media', os.path.join(dst, 'messages', peer, 'media.html'), 1250, None),
            ('after-chat-stats', os.path.join(dst, 'messages', peer, 'stats.html'), 1700, '#ex-st-heat svg'),
            ('after-stats', os.path.join(dst, 'messages', 'stats.html'), 1550, '#ex-st-heat svg'),
            ('after-search', os.path.join(dst, 'search.html') + '?q=' + a.query, 1150, '.ex-hit'),
        ]
        with sync_playwright() as pw:
            env = dict(os.environ, LANG='ru_RU.UTF-8', LANGUAGE='ru', **emoji_fontconfig(work, a.emoji_font))
            opts = dict(env=env, args=['--lang=ru-RU'])  # Russian date fields, like a Russian user's browser
            browser = pw.chromium.launch(executable_path=a.chromium, **opts) if a.chromium else pw.chromium.launch(**opts)
            page = browser.new_page(viewport={'width': 1100, 'height': 900}, device_scale_factor=1, locale='ru-RU')
            for name, path, height, wait in shots:
                url = 'file:///' + path.replace(os.sep, '/').lstrip('/')
                page.goto(url)
                if wait:
                    page.wait_for_selector(wait, timeout=30000)
                page.wait_for_timeout(600)  # lazy images, fonts
                box = page.locator('.page_content').first.bounding_box()
                total = page.evaluate('document.documentElement.scrollHeight')
                page.set_viewport_size({'width': 1100, 'height': max(900, min(height, total))})
                page.wait_for_timeout(300)
                x0 = max(0, int(box['x']) - 24)
                x1 = min(1100, int(box['x'] + box['width']) + 24)
                png = os.path.join(work, name + '.png')
                page.screenshot(path=png, clip={'x': x0, 'y': 0, 'width': x1 - x0, 'height': min(height, total)})
                out = os.path.join(a.out, name + '.jpg')  # JPEG keeps the repository small
                Image.open(png).convert('RGB').save(out, 'JPEG', quality=82, optimize=True, progressive=True)
                print('saved', os.path.relpath(out, ROOT), '%d KB' % (os.path.getsize(out) // 1024))
            browser.close()
    finally:
        if not a.keep and not a.work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
