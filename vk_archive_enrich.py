#!/usr/bin/env python3
"""
vk_archive_enrich.py - makes an official VK data export ("архив данных ВКонтакте") self-contained,
so it keeps working even if vk.ru disappears.

The export only has text: attachments are bare labels ("Фотография",
"1 прикреплённое сообщение"), albums hotlink VK servers, there are no avatars,
forwarded messages, wall posts, voice messages or videos. This script logs in
with your browser session, fetches what is missing through the VK API, downloads
the media and writes a NEW, improved copy of the archive (the original is never
touched). See README.md for details.

    python3 vk_archive_enrich.py PATH_TO_ARCHIVE [OUTPUT] [options]      (--help for options)
"""
from __future__ import annotations

import argparse
import base64
import getpass
import calendar
import concurrent.futures as cf
import glob
import hashlib
import html
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit('This script needs the "requests" package: pip install requests')

__version__ = '1.1.0'
API_VERSION = '5.199'
WEB_APP_ID = '6287487'  # app id of the vk.ru web client; its token can read messages
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0'
VK = 'https://vk.ru'
FROZEN = getattr(sys, 'frozen', False)  # running from the Windows build (PyInstaller)
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0  # CREATE_NO_WINDOW: no console flashing for ffmpeg


def find_ffmpeg():
    """ffmpeg shipped next to the program (Windows build) or installed on PATH."""
    exe = 'ffmpeg.exe' if os.name == 'nt' else 'ffmpeg'
    for d in (getattr(sys, '_MEIPASS', None), os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else None):
        if d and os.path.isfile(os.path.join(d, exe)):
            return os.path.join(d, exe)
    return shutil.which('ffmpeg')


FFMPEG = find_ffmpeg()


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def esc(s):
    return html.escape('' if s is None else str(s), quote=True)


def url_of(rel, prefix):
    """Relative link from a page (prefix = '../' * depth) to a file under dst root."""
    return esc(prefix + urllib.parse.quote(rel, safe='/'))


_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s, maxlen=80):
    s = _BAD_CHARS.sub('_', s or '').strip().strip('.')
    return s[:maxlen].strip() or 'file'


def sha(s):
    return hashlib.sha1(s.encode('utf-8')).hexdigest()[:16]


def url_ext(url, default='.jpg'):
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    return ext if re.fullmatch(r'\.[a-z0-9]{2,5}', ext or '') else default


def fmt_size(n):
    n = float(n or 0)
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if n < 1024 or unit == 'ГБ':
            return ('%d %s' if unit == 'Б' else '%.1f %s') % (n, unit)
        n /= 1024


def fmt_dur(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return '%d:%02d:%02d' % (h, m, s) if h else '%d:%02d' % (m, s)


_DIV = re.compile(r'<div\b|</div>')


def div_end(s, start):
    """Index just after the </div> closing the <div ...> that starts at `start`."""
    depth = 0
    for m in _DIV.finditer(s, start):
        if m.group() == '</div>':
            depth -= 1
            if depth == 0:
                return m.end()
        else:
            depth += 1
    return len(s)


def div_blocks(s, opener):
    """(start, end) of every top-level block that starts with `opener`."""
    out, pos = [], 0
    while True:
        i = s.find(opener, pos)
        if i < 0:
            return out
        j = div_end(s, i)
        out.append((i, j))
        pos = j


# ----------------------------------------------------------------------------
# Firefox cookies -> VK API token
# ----------------------------------------------------------------------------

FF_PROFILE_GLOBS = [
    '/mnt/c/Users/*/AppData/Roaming/Mozilla/Firefox/Profiles/*',  # Windows profile seen from WSL
    os.path.expandvars(r'%APPDATA%\Mozilla\Firefox\Profiles\*'),  # native Windows
    '~/.mozilla/firefox/*',
    '~/snap/firefox/common/.mozilla/firefox/*',
    '~/Library/Application Support/Firefox/Profiles/*',
]


def firefox_cookie_dbs(profile=None):
    if profile:
        return [os.path.join(profile, 'cookies.sqlite')]
    dbs = []
    for g in FF_PROFILE_GLOBS:
        dbs += glob.glob(os.path.join(os.path.expanduser(g), 'cookies.sqlite'))
    return sorted(set(dbs), key=os.path.getmtime, reverse=True)


def load_vk_cookies(profile=None):
    """[(host, name, value, path)] of vk.ru / vk.com cookies from the first profile that is logged in."""
    for db in firefox_cookie_dbs(profile):
        tmp = tempfile.mkdtemp(prefix='vk_archive_enrich_')
        try:
            # Firefox keeps the db locked and recent writes in the -wal file: copy both.
            for suffix in ('', '-wal'):
                if os.path.exists(db + suffix):
                    shutil.copy2(db + suffix, os.path.join(tmp, 'cookies.sqlite' + suffix))
            con = sqlite3.connect(os.path.join(tmp, 'cookies.sqlite'))
            rows = con.execute(
                "SELECT host, name, value, path FROM moz_cookies "
                "WHERE (host LIKE '%vk.ru' OR host LIKE '%vk.com') "
                "ORDER BY originAttributes != '' DESC").fetchall()  # default container last = wins
            con.close()
        except sqlite3.Error:
            rows = []
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if logged_in(rows):
            return rows, db
    return [], None


def logged_in(rows):
    return any(r[1] in ('remixsid', 'remixnsid') for r in rows)


def is_vk_host(host):
    host = (host or '').lstrip('.')
    return host in ('vk.ru', 'vk.com') or host.endswith(('.vk.ru', '.vk.com'))


CHROMIUM_BROWSERS = ('chrome', 'edge', 'brave', 'opera', 'vivaldi', 'chromium', 'whale', 'safari')


def cookies_via_ytdlp(browser, profile=None):
    """Chrome & co. encrypt cookies (DPAPI / keyring / Keychain); yt-dlp knows how to decrypt them."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        raise RuntimeError('reading %s cookies needs yt-dlp: pip install yt-dlp' % browser)

    class Quiet:  # yt-dlp's logger interface
        def debug(self, *a, **k): pass
        info = warning = debug

        def error(self, msg, *a, **k):
            log('   yt-dlp: %s' % msg)

        def progress_bar(self):
            return None
    jar = extract_cookies_from_browser(browser, profile or None, Quiet())
    return [(c.domain, c.name, c.value, c.path) for c in jar if is_vk_host(c.domain) and c.value]


def cookies_from_file(path):
    """cookies.txt in Netscape format (browser extensions like "Get cookies.txt LOCALLY" export it)."""
    jar = http.cookiejar.MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return [(c.domain, c.name, c.value, c.path) for c in jar if is_vk_host(c.domain)]


def load_cookies(browser=None, cookies_file=None):
    """Returns (cookies, description). browser = 'firefox' / 'chrome' / ... optionally ':profile'."""
    if cookies_file:
        rows = cookies_from_file(cookies_file)
        return (rows if logged_in(rows) else []), 'file ' + cookies_file
    name, _, profile = (browser or '').partition(':')
    name = name.lower()
    if name in ('', 'firefox'):
        rows, db = load_vk_cookies(profile or None)
        if rows:
            return rows, 'Firefox (%s)' % os.path.dirname(db)
        if name:
            return [], 'Firefox'
    candidates = [name] if name else list(CHROMIUM_BROWSERS[:5])
    errors = []
    for b in candidates:
        if b not in CHROMIUM_BROWSERS:
            raise SystemExit('Unknown browser "%s". Supported: firefox, %s' % (b, ', '.join(CHROMIUM_BROWSERS)))
        try:
            rows = cookies_via_ytdlp(b, profile)
        except Exception as e:  # browser not installed, cannot decrypt, ...
            errors.append('%s: %s' % (b, str(e).splitlines()[0][:150]))
            continue
        if logged_in(rows):
            return rows, b.capitalize()
        errors.append('%s: no vk.ru session' % b)
    if name or errors:
        for e in errors:
            log('   ' + e)
    return [], name or 'any browser'


def write_netscape_cookies(cookies, path):
    """cookies.txt for yt-dlp; vk.ru cookies are duplicated for vk.com."""
    with open(path, 'w') as f:
        f.write('# Netscape HTTP Cookie File\n')
        for host, name, value, cpath in cookies:
            for h in {host, host.replace('vk.ru', 'vk.com')}:
                f.write('\t'.join([h, 'TRUE' if h.startswith('.') else 'FALSE', cpath or '/',
                                   'TRUE', '2147483647', name, value]) + '\n')


class VKError(Exception):
    def __init__(self, code, msg):
        super().__init__('%s: %s' % (code, msg))
        self.code = code


TOKEN_SNIPPET = ("fetch('https://login.vk.ru/?act=web_token',{method:'POST',credentials:'include',"
                 "body:new URLSearchParams({version:1,app_id:%s})}).then(r=>r.json())"
                 ".then(j=>prompt('access_token:',j.data.access_token))" % WEB_APP_ID)


def ask_token(expired=False):
    """Read a token typed / pasted by the user (hidden, so it stays out of shell history)."""
    if os.environ.get('VK_ARCHIVE_ENRICH_GUI'):  # the window answers on stdin
        print('@@VKAE_ASK_TOKEN %s@@' % ('expired' if expired else 'new'), flush=True)
        token = sys.stdin.readline().strip()
        if not token:
            sys.exit('No token given.')
        return token
    if not sys.stdin.isatty():
        sys.exit('The VK token %s and there is no terminal to ask for a new one.' % ('expired' if expired else 'is missing'))
    print('\n%s Open vk.ru in your browser, press F12 -> Console, paste this line and press Enter:\n\n    %s\n\n'
          'then copy the token from the window that appears.' % (
              'The token has expired (they live about an hour).' if expired else 'A VK access token is needed.', TOKEN_SNIPPET))
    token = getpass.getpass('access_token (input is hidden): ').strip()
    if not token:
        sys.exit('No token given.')
    return token


class VKApi:
    def __init__(self, cookies, token=None):
        self.http = requests.Session()
        self.http.headers['User-Agent'] = UA
        for host, name, value, path in cookies:
            self.http.cookies.set(name, value, domain=host, path=path or '/')
        self.can_refresh = bool(cookies)
        self.token = token
        self.expires = float('inf') if token else 0
        self.user_id = None
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.calls = 0

    def refresh(self):
        for dom in ('vk.ru', 'vk.com'):
            try:
                r = self.http.post('https://login.%s/?act=web_token' % dom,
                                   data={'version': '1', 'app_id': WEB_APP_ID},
                                   headers={'Origin': 'https://' + dom, 'Referer': 'https://%s/' % dom},
                                   timeout=30)
                j = r.json()
            except (requests.RequestException, ValueError):
                continue
            if j.get('type') == 'okay':
                d = j['data']
                self.token, self.expires, self.user_id = d['access_token'], d['expires'], d['user_id']
                return
        sys.exit('Could not get a VK token from the browser cookies (the session may have expired). '
                 'Open vk.ru in the browser, make sure you are logged in, and rerun - or use --token ask.')

    def _throttle(self):
        with self.lock:  # VK allows 3 requests per second for user tokens
            now = time.time()
            if self.next_slot > now:
                time.sleep(self.next_slot - now)
            self.next_slot = max(now, self.next_slot) + 0.35

    def call(self, method, **params):
        last = None
        for attempt in range(8):
            if self.can_refresh and time.time() > self.expires - 90:
                self.refresh()
            self._throttle()
            data = {k: v for k, v in params.items() if v is not None}
            data.update(access_token=self.token, v=API_VERSION)
            try:
                j = self.http.post('https://api.vk.ru/method/' + method, data=data, timeout=90).json()
            except (requests.RequestException, ValueError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
                continue
            self.calls += 1
            if 'error' not in j:
                return j['response']
            code, msg = j['error'].get('error_code'), j['error'].get('error_msg', '')
            last = VKError(code, msg)
            if code == 5 and self.can_refresh:
                self.expires = 0
                continue
            if code == 5 and attempt < 7:  # a pasted token expired: ask for a fresh one
                with self.lock:
                    self.token = ask_token(expired=True)
                continue
            if code in (6, 9, 10, 29):  # rate limits / temporary internal errors
                time.sleep(1.5 * (attempt + 1))
                continue
            if code == 14:
                sys.exit('VK asked for a captcha. Open vk.ru in the browser, browse a bit, wait '
                         'a few minutes and rerun (finished work is kept).')
            raise last
        raise VKError(-1, '%s: giving up after retries (%s)' % (method, last))

    def execute(self, calls):
        """Run up to 25 (method, params) calls in one request. Returns results, None for failed ones."""
        if not calls:
            return []
        code = 'return [' + ','.join(
            'API.%s(%s)' % (m, json.dumps(p, ensure_ascii=False)) for m, p in calls) + '];'
        try:
            res = self.call('execute', code=code)
            return [r if r not in (False, None) else None for r in res]
        except VKError:
            out = []
            for m, p in calls:  # e.g. response too big - fall back to single calls
                try:
                    out.append(self.call(m, **p))
                except VKError:
                    out.append(None)
            return out


# ----------------------------------------------------------------------------
# the source archive
# ----------------------------------------------------------------------------

MSG_SPLIT = re.compile(r'<div class="message" data-id="(\d+)">')
MONTHS = {'янв': 1, 'фев': 2, 'мар': 3, 'апр': 4, 'мая': 5, 'июн': 6,
          'июл': 7, 'авг': 8, 'сен': 9, 'окт': 10, 'ноя': 11, 'дек': 12}
DATE_RX = re.compile(r'(\d{1,2}) ([а-яё]+)\.? (\d{4}) в (\d{1,2}):(\d\d):(\d\d)')


def read_page(path):
    raw = open(path, 'rb').read()
    head = raw[:600].lower()
    if b'charset="utf-8"' in head or b'charset=utf-8' in head:
        return raw.decode('utf-8', 'replace')
    return raw.decode('cp1251', 'replace')


def page_num(name):
    m = re.search(r'(\d+)\.html$', name)
    return int(m.group(1)) if m else -1


def series_key(rel):
    """'likes/wall/likes3.html' -> ('likes/wall', 'likes', 3); 'comments/comments.html' -> (..., 1)."""
    d, _, name = rel.rpartition('/')
    m = re.fullmatch(r'(.*?)(\d*)\.html', name)
    if not m.group(1) or m.group(1).endswith('-'):  # album ids like "-6.html" are names, not page numbers
        return d, name[:-5], 1
    return d, m.group(1), int(m.group(2)) if m.group(2) else 1


def link_owner(fragment):
    """VK id referenced by the first profile link in an html fragment (groups negative)."""
    m = re.search(r'href="https?://vk\.(?:ru|com)/(id|club|public|event)(\d+)"', fragment)
    if not m:
        return None
    return int(m.group(2)) * (1 if m.group(1) == 'id' else -1)


def header_lts(hdr):
    """Message header date ('25 янв 2024 в 5:03:06', archive's local time) as a naive epoch."""
    m = DATE_RX.search(hdr)
    if not m or m.group(2)[:3] not in MONTHS:
        return None
    d, mon, y, hh, mm, ss = m.groups()
    return calendar.timegm((int(y), MONTHS[mon[:3]], int(d), int(hh), int(mm), int(ss)))


def html_text(fragment):
    t = re.sub(r'<br\s*/?>', '\n', fragment or '')
    return html.unescape(re.sub(r'<[^>]+>', '', t)).strip()


def count_items(page_html):
    if 'nothing-found-stub' in page_html:
        return 0
    return page_html.count('<div class="item">')


class Conv:
    """One conversation as the archive has it. msgs: newest first, as on the pages:
    (id, local time, author id, page index, has attachments, text)."""

    def __init__(self, peer):
        self.peer = peer
        self.pages = []  # file names, newest first
        self.msgs = []
        self.name = ''

    def page_of(self, i):
        return self.pages[i] if 0 <= i < len(self.pages) else self.pages[0]


class Archive:
    def __init__(self, src):
        self.src = os.path.abspath(src)
        if not os.path.exists(os.path.join(self.src, 'index.html')):
            sys.exit('%s does not look like a VK archive (no index.html)' % self.src)
        self.self_id = self._self_id()
        self.convs = {}  # peer -> Conv
        self.header_ids = set()  # authors linked from message headers
        self._items = {}

    def _self_id(self):
        t = read_page(os.path.join(self.src, 'index.html'))
        m = re.search(r'<meta name="jd" content="([^"]+)"', t)
        if m:
            b = m.group(1)
            try:
                return int(json.loads(base64.urlsafe_b64decode(b + '=' * (-len(b) % 4)))['user_id'])
            except (ValueError, KeyError):
                pass
        return None

    def path(self, *rel):
        return os.path.join(self.src, *rel)

    def exists(self, rel):
        return os.path.exists(self.path(rel))

    def html_files(self):
        for dp, dn, fn in os.walk(self.src):
            dn.sort()
            for f in sorted(fn):
                if f.endswith('.html'):
                    yield os.path.relpath(os.path.join(dp, f), self.src).replace(os.sep, '/')

    def pages(self, subdir, pattern=r'.*\.html$'):
        d = self.path(subdir)
        if not os.path.isdir(d):
            return []
        names = [f for f in os.listdir(d) if re.match(pattern, f)]
        return [subdir + '/' + f for f in sorted(names, key=page_num)]

    def series(self, rel):
        """All pages of the paginated list `rel` belongs to, in order."""
        d, prefix, _ = series_key(rel)
        full = self.path(d)
        if not os.path.isdir(full):
            return [rel]
        out = []
        for f in os.listdir(full):
            k = series_key(d + '/' + f) if f.endswith('.html') else None
            if k and k[1] == prefix:
                out.append((k[2], (d + '/' + f).lstrip('/')))
        return [r for _, r in sorted(out)] or [rel]

    def items(self, rel):
        if rel not in self._items:
            self._items[rel] = count_items(read_page(self.path(rel))) if self.exists(rel) else 0
        return self._items[rel]

    def peers(self):
        d = self.path('messages')
        if not os.path.isdir(d):
            return []
        return sorted((int(x) for x in os.listdir(d) if re.fullmatch(r'-?\d+', x)),
                      key=lambda p: (p >= 2000000000, p < 0, abs(p)))

    def peer_names(self):
        if not self.exists('messages/index-messages.html'):
            return {}
        t = read_page(self.path('messages/index-messages.html'))
        return {int(p): html_text(n) for p, n in re.findall(r'<a href="(-?\d+)/messages0\.html">(.*?)</a>', t, re.S)}

    def scan_peer(self, peer):
        conv = Conv(peer)
        for pi, rel in enumerate(self.pages('messages/%d' % peer, r'messages\d+\.html$')):
            conv.pages.append(os.path.basename(rel))
            parts = MSG_SPLIT.split(read_page(self.path(rel)))
            for i in range(1, len(parts), 2):
                mid, chunk = int(parts[i]), parts[i + 1]
                he = chunk.find('</div>') + 6
                hdr = chunk[:he]
                author = link_owner(hdr)
                if author:
                    self.header_ids.add(author)
                else:
                    author = self.self_id
                k = chunk.find('<div class="kludges">', he)
                text = html_text(chunk[he:k if k >= 0 else len(chunk)])
                if not text and k >= 0 and 'im_srv_lnk' in chunk[k:k + 2000]:
                    text = html_text(chunk[k:div_end(chunk, k)])  # service message ("X invited Y")
                conv.msgs.append((mid, header_lts(hdr), author, pi, 'class="attachment"' in chunk, text))
        self.convs[peer] = conv
        return conv

    def all_profile_ids(self):
        """Every user / group id linked anywhere in the archive (for avatars)."""
        ids = set()
        rx = re.compile(r'vk\.(?:ru|com)/(id|club|public|event)(\d+)\b')
        for rel in self.html_files():
            if rel.startswith('messages/') and rel.count('/') > 1:
                continue  # message pages: authors come from the API data instead
            for kind, num in rx.findall(read_page(self.path(rel))):
                ids.add(int(num) if kind == 'id' else -int(num))
        return ids

    def find_links(self, rels, rx):
        out = []
        for rel in rels:
            out += re.findall(rx, read_page(self.path(rel)))
        return out


# ----------------------------------------------------------------------------
# downloads
# ----------------------------------------------------------------------------

# media kinds as the user names them (--no-media) -> key prefixes used internally
MEDIA_KINDS = {'photos': ('photo', 'url'), 'videos': ('video',), 'covers': ('vthumb',), 'voice': ('voice',),
               'music': ('audio',), 'docs': ('doc',), 'stickers': ('sticker',), 'gifts': ('gift',),
               'avatars': ('ava', 'avabig')}


class Media:
    """Registry of local copies. Keys look like 'photo:1_2', 'doc:1_2', 'ava:123', 'url:<sha>'."""

    def __init__(self, dst, cookies, workers=8, max_file_mb=0, max_video_mb=0, ytdlp_cookies=None,
                 slow_workers=4, segments=6, disabled=()):
        self.dst = dst
        self.index_path = os.path.join(dst, '_data', 'media_index.json')
        self.failed_path = os.path.join(dst, '_data', 'media_failed.json')
        self.index = load_json(self.index_path, {})
        self.failed = load_json(self.failed_path, {})
        self.cookies = cookies
        self.workers = workers
        self.max_bytes = max_file_mb * 2 ** 20
        self.max_video_bytes = max_video_mb * 2 ** 20
        self.ytdlp_cookies = ytdlp_cookies
        self.pending = {}  # rel path -> [url, method, limit, keys]
        self.local = threading.local()
        self.lock = threading.Lock()
        self.stats = Counter()
        self.pool = self.slow_pool = None
        self.futs, self.slow_futs = set(), set()
        self.slow_workers = slow_workers
        self.disabled = {prefix for kind in disabled for prefix in MEDIA_KINDS[kind]}
        self.segments = segments
        self.queued = self.finished = self.succeeded = 0
        self.last_log = 0.0
        self.have = set()  # rel paths that exist (stat() is slow on mounted Windows drives)
        for dp, dn, fn in os.walk(os.path.join(dst, 'media')):
            for f in fn:
                if '.part' not in f:
                    self.have.add(os.path.relpath(os.path.join(dp, f), dst).replace(os.sep, '/'))

    def path(self, key):
        rel = self.index.get(key)
        return rel if rel in self.have else None

    def want(self, key, url, rel, method='http', limit=None):
        """Schedule a download unless we already have it. Use path() when rendering."""
        if not url or key.split(':')[0] in self.disabled:
            return
        have = self.index.get(key)
        if have in self.have:
            return
        if rel in self.have:
            self.index[key] = rel  # file is there already (interrupted run, shared placeholder)
            return
        if rel in self.pending:
            if key not in self.pending[rel][3]:
                self.pending[rel][3].append(key)
            return
        self.pending[rel] = [url, method, limit, [key]]

    def _session(self):
        s = getattr(self.local, 's', None)
        if s is None:
            s = self.local.s = requests.Session()
            s.headers['User-Agent'] = UA
            for host, name, value, path in self.cookies:
                s.cookies.set(name, value, domain=host, path=path or '/')
        return s

    SEGMENT_MIN = 24 << 20  # files bigger than this are fetched in parallel ranges

    def _http(self, url, tmp, limit, follow_page=True):
        """Download with resume: an existing .part file is continued with a Range request.
        VK's video CDN throttles every connection to about playback speed, so big files are
        split into ranges fetched in parallel (links only work with the same User-Agent)."""
        err, segmented = None, True
        for attempt in range(4):
            start = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            headers = {'Range': 'bytes=%d-' % start} if start else {}
            try:
                with self._session().get(url, headers=headers, stream=True, timeout=(20, 120)) as r:
                    if r.status_code == 416 and start:  # the .part is already complete
                        total = int((r.headers.get('content-range') or '/0').rsplit('/', 1)[1] or 0)
                        if total == start:
                            return None
                        os.remove(tmp)
                        continue
                    if r.status_code not in (200, 206):
                        err = 'HTTP %d' % r.status_code
                        if r.status_code in (404, 410, 451) or r.status_code == 403 and attempt:
                            return err
                        time.sleep(3 * (attempt + 1))
                        continue
                    ctype = r.headers.get('content-type', '')
                    if r.status_code == 200 and ctype.startswith('text/html') and \
                            not r.url.lower().split('?')[0].endswith(('.html', '.htm')):
                        # pdf / djvu / torrent documents open VK's viewer page; it links the real file
                        if follow_page:  # as a plain link, or JSON-escaped inside the viewer's script
                            m = re.search(r'href="(https://[^"]*(?:vkuserphoto\.ru|userapi\.com)/[^"]+)"', r.text) or \
                                re.search(r'(https:\\/\\/[^"\'\s]*(?:vkuserphoto\.ru|userapi\.com)\\/[^"\'\s]+)', r.text)
                            if m:
                                real = html.unescape(m.group(1).replace('\\/', '/'))
                                return self._http(real, tmp, limit, follow_page=False)
                        box = re.search(r'class="message_page_body">(.*?)</div>', r.text, re.S)
                        if box:  # e.g. uploader banned, removed on copyright request
                            text = html.unescape(re.sub(r'<[^>]+>', ' ', box.group(1)))
                            return 'VK: ' + re.sub(r'\s+', ' ', text).replace(' Назад', '').strip()[:200]
                        return 'got an html page instead of a file'
                    if r.status_code == 206:
                        total = int(r.headers['content-range'].rsplit('/', 1)[1])
                    else:  # full body: (re)start from zero
                        start, total = 0, int(r.headers.get('content-length') or 0)
                    if limit and total > limit:
                        return 'skipped: %s is over the size limit' % fmt_size(total)
                    if segmented and total - start > self.SEGMENT_MIN:
                        r.close()
                        if start == 0 and os.path.exists(tmp):
                            os.remove(tmp)
                        err = self._segmented(url, tmp, start, total)
                        if err is None:
                            return None
                        if err == 'no range support':
                            segmented = False
                        else:
                            time.sleep(3 * (attempt + 1))
                        continue
                    with open(tmp, 'ab' if r.status_code == 206 else 'wb') as f:
                        for block in r.iter_content(1 << 20):
                            f.write(block)
                    if total and os.path.getsize(tmp) < total:
                        err = 'truncated'
                        continue
                    return None
            except requests.RequestException as e:
                err = type(e).__name__
                time.sleep(3 * (attempt + 1))
        return err or 'failed'

    def _segmented(self, url, tmp, start, total):
        """Fetch bytes [start, total) as parallel ranges into tmp.0, tmp.1 ... then append to tmp.
        The split only depends on (start, total), so an interrupted run resumes each piece."""
        n = int(max(2, min(self.segments, (total - start) // (8 << 20))))
        size = -(-(total - start) // n)
        ranges = [(start + k * size, min(total, start + (k + 1) * size) - 1) for k in range(n)]

        def piece(k):
            a, b = ranges[k]
            path = '%s.%d' % (tmp, k)
            for attempt in range(8):
                have = os.path.getsize(path) if os.path.exists(path) else 0
                if a + have > b:
                    return None
                try:
                    with self._session().get(url, headers={'Range': 'bytes=%d-%d' % (a + have, b)},
                                             stream=True, timeout=(20, 120)) as r:
                        if r.status_code == 200:
                            return 'no range support'
                        if r.status_code != 206:
                            if r.status_code in (403, 404, 410):
                                return 'HTTP %d' % r.status_code
                            time.sleep(3 * (attempt + 1))
                            continue
                        with open(path, 'ab') as f:
                            for block in r.iter_content(1 << 20):
                                f.write(block)
                except requests.RequestException:
                    time.sleep(3 * (attempt + 1))
            return 'range download kept failing'

        with cf.ThreadPoolExecutor(n) as ex:
            errs = [e for e in ex.map(piece, range(n)) if e]
        if errs:
            if errs[0] == 'no range support':
                for k in range(n):
                    if os.path.exists('%s.%d' % (tmp, k)):
                        os.remove('%s.%d' % (tmp, k))
            return errs[0]
        with open(tmp, 'ab') as out:
            for k in range(n):
                with open('%s.%d' % (tmp, k), 'rb') as f:
                    shutil.copyfileobj(f, out, 1 << 20)
        for k in range(n):
            os.remove('%s.%d' % (tmp, k))
        return None if os.path.getsize(tmp) == total else 'size mismatch after joining pieces'

    def _ffmpeg(self, url, tmp, fmt):
        if not FFMPEG:
            return 'ffmpeg not found'
        args = [FFMPEG, '-y', '-loglevel', 'error', '-i', url, '-c', 'copy']
        if fmt == 'mp4':
            args += ['-bsf:a', 'aac_adtstoasc']
        p = subprocess.run(args + ['-f', fmt, tmp], capture_output=True, text=True, errors='replace',
                           timeout=3600, creationflags=NO_WINDOW)
        return None if p.returncode == 0 and os.path.getsize(tmp) > 0 else 'ffmpeg: ' + p.stderr.strip()[-200:]

    def _ytdlp(self, url, tmp, height):
        """YouTube & co. through yt-dlp used as a library (also works inside the Windows build)."""
        try:
            import yt_dlp
        except ImportError:
            return 'yt-dlp is not installed (pip install yt-dlp)'
        opts = {'format': 'bv*[height<=%d]+ba/b[height<=%d]/b' % (height, height), 'merge_output_format': 'mp4',
                'outtmpl': tmp + '.%(ext)s', 'noplaylist': True, 'quiet': True, 'no_warnings': True, 'noprogress': True}
        if FFMPEG:
            opts['ffmpeg_location'] = FFMPEG
        if self.ytdlp_cookies:
            opts['cookiefile'] = self.ytdlp_cookies
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except Exception as e:
            return 'yt-dlp: %s' % str(e).strip()[-200:]
        for f in glob.glob(glob.escape(tmp) + '.*'):
            if not f.endswith(('.part', '.ytdl')):
                os.replace(f, tmp)
                return None
        return 'yt-dlp: no file was produced'

    def _one(self, rel, url, method, limit, keys):
        dest = os.path.join(self.dst, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + '.part'
        try:
            if method.startswith('hls'):
                err = self._ffmpeg(url, tmp, method.split(':')[1])
            elif method.startswith('ytdlp'):
                err = self._ytdlp(url, tmp, int(method.split(':')[1]))
            else:
                for u in (url if isinstance(url, (list, tuple)) else [url]):  # fallbacks in order
                    err = self._http(u, tmp, self.max_bytes if limit is None else limit)
                    if err is None:
                        break
        except Exception as e:  # never let one file kill the run
            err = '%s: %s' % (type(e).__name__, e)
        if err is None:
            os.replace(tmp, dest)
        elif os.path.exists(tmp) and (method != 'http' or re.match(r'HTTP 4|skipped|got an html|VK: ', err)):
            os.remove(tmp)  # otherwise keep the .part: the next run resumes it
        self._record(keys, rel, err)

    def flush(self, label=''):
        """Hand pending downloads to the background pools. Videos and music go to their own
        pool so they never hold up photos; nothing waits here unless the photo/document queue
        is far ahead of the downloads. (Video links stay valid for days.)"""
        if self.pending:
            if self.pool is None:
                self.pool = cf.ThreadPoolExecutor(self.workers)
                self.slow_pool = cf.ThreadPoolExecutor(self.slow_workers)
            jobs, self.pending = self.pending, {}
            with self.lock:
                self.queued += len(jobs)
            for rel, v in jobs.items():
                slow = v[1] != 'http' or v[3][0].startswith(('video:', 'audio:'))
                if slow:
                    self.slow_futs.add(self.slow_pool.submit(self._one, rel, *v))
                else:
                    self.futs.add(self.pool.submit(self._one, rel, *v))
        while self.in_flight() > self.workers * 60:  # backpressure
            self.progress(force=False)
            time.sleep(1)
        self.progress(force=False)

    def in_flight(self, slow=False):
        self.futs = {f for f in self.futs if not f.done()}
        self.slow_futs = {f for f in self.slow_futs if not f.done()}
        return len(self.futs) + (len(self.slow_futs) if slow else 0)

    def shutdown(self):
        for pool in (self.pool, self.slow_pool):
            if pool:
                pool.shutdown(wait=False, cancel_futures=True)

    def wait(self, label=''):
        """Block until every queued download has finished."""
        try:
            while self.in_flight(slow=True):
                self.progress(force=False)
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()
            self.save()
            raise
        self.progress(force=True)
        self.save()

    def progress(self, force):
        if force or time.time() - self.last_log > 30:
            self.last_log = time.time()
            with self.lock:
                done, ok = self.finished, self.succeeded
            log('   downloads: %d/%d done (%d ok), %d in queue' % (done, self.queued, ok, self.queued - done))
            self.save()

    def _record(self, keys, rel, err):
        kind = keys[0].split(':')[0]
        with self.lock:
            self.finished += 1
            if err is None:
                self.succeeded += 1
                self.have.add(rel)
                for key in keys:
                    self.index[key] = rel
                    self.failed.pop(key, None)
                self.stats[kind + ' ok'] += 1
            else:
                for key in keys:
                    self.failed[key] = err
                self.stats[kind + (' skipped' if err.startswith('skipped') else ' failed')] += 1

    def save(self):
        with self.lock:
            index, failed = dict(self.index), dict(self.failed)
        save_json(self.index_path, index)
        save_json(self.failed_path, failed)


# ----------------------------------------------------------------------------
# picking urls out of API objects
# ----------------------------------------------------------------------------

SIZE_ORDER = {t: i for i, t in enumerate('s m x o p q r y z w base'.split())}


def best_photo_url(p):
    if not p:
        return None
    if (p.get('orig_photo') or {}).get('url'):
        return p['orig_photo']['url']
    sizes = [s for s in (p.get('sizes') or []) if s.get('url') or s.get('src')]
    if not sizes:
        return p.get('photo_2560') or p.get('photo_1280') or p.get('photo_807') or p.get('photo_604')
    s = max(sizes, key=lambda s: ((s.get('width') or 0) * (s.get('height') or 0), SIZE_ORDER.get(s.get('type'), 0)))
    return s.get('url') or s.get('src')


def pick_image(images, want=800):
    """From [{url,width,height}] the smallest image at least `want` wide, else the largest."""
    images = [i for i in (images or []) if i.get('url')]
    if not images:
        return None
    big = [i for i in images if (i.get('width') or 0) >= want]
    return (min(big, key=lambda i: i['width']) if big else max(images, key=lambda i: i.get('width') or 0))['url']


def oid(o):
    return '%s_%s' % (o.get('owner_id'), o.get('id'))


def doc_rel(d):
    title = d.get('title') or 'doc'
    ext = (d.get('ext') or '').lower()
    if ext and not title.lower().endswith('.' + ext):
        title += '.' + ext
    return 'media/docs/%s %s' % (oid(d), safe_name(title, 100))


class Want:
    """Schedules downloads for API objects (the renderer later asks Media for local paths)."""

    def __init__(self, media, opts, api=None):
        self.m = media
        self.o = opts
        self.api = api
        self.unresolved = {}  # videos that came without file urls: key -> "owner_id_access"
        self.resolved = set()

    def resolve_videos(self):
        """Videos inside wall posts etc. have no file urls; video.get returns them."""
        todo = {k: v for k, v in self.unresolved.items() if k not in self.resolved}
        self.unresolved = {}
        for part in chunks(sorted(todo), 100):
            self.resolved.update(part)
            try:
                r = self.api.call('video.get', videos=','.join(todo[k] for k in part), count=200)
            except VKError as e:
                log('   video.get failed:', e)
                continue
            for v in r.get('items', []):
                self.video(v)

    def photo(self, p):
        if p and p.get('id') is not None:
            self.m.want('photo:' + oid(p), best_photo_url(p), 'media/photos/%s.jpg' % oid(p))

    def url(self, u, folder='misc'):
        if u:
            self.m.want('url:' + sha(u), u, 'media/%s/%s%s' % (folder, sha(u), url_ext(u)))

    def video(self, v, full=True):
        if not v:
            return
        key = oid(v)
        thumb = pick_image(v.get('image')) or pick_image(v.get('first_frame'))
        if thumb:
            self.m.want('vthumb:' + key, thumb, 'media/video_thumbs/%s.jpg' % key)
        if not full or 'video' in self.m.disabled:
            return
        files = v.get('files') or {}
        if not files and self.api and key not in self.resolved and v.get('id'):
            self.unresolved[key] = key + ('_' + v['access_key'] if v.get('access_key') else '')
            return
        mp4 = sorted((int(k[4:]), u) for k, u in files.items()
                     if re.fullmatch(r'mp4_\d+', k) and u and int(k[4:]) <= self.o.max_video_height)
        limit = self.m.max_video_bytes
        if mp4:
            self.m.want('video:' + key, mp4[-1][1], 'media/videos/%s.mp4' % key, limit=limit)
        elif files.get('hls'):
            self.m.want('video:' + key, files['hls'], 'media/videos/%s.mp4' % key, 'hls:mp4')
        elif self.o.external_videos and (files.get('external') or v.get('player')):
            self.m.want('video:' + key, files.get('external') or v.get('player'),
                        'media/videos/%s.mp4' % key, 'ytdlp:%d' % self.o.max_video_height)

    def doc(self, d):
        if not d:
            return
        prev = d.get('preview') or {}
        if prev.get('audio_msg'):
            am = prev['audio_msg']
            self.m.want('voice:' + oid(d), am.get('link_mp3'), 'media/voice/%s.mp3' % oid(d))
            return
        if self.m.max_bytes and (d.get('size') or 0) > self.m.max_bytes:
            self.m.failed['doc:' + oid(d)] = 'skipped: %s is over the size limit' % fmt_size(d['size'])
            return
        self.m.want('doc:' + oid(d), d.get('url'), doc_rel(d))

    def attachment(self, a, light=False):
        """light=True: only pictures (used for likes)."""
        t = a.get('type')
        o = a.get(t) or {}
        if t == 'photo':
            self.photo(o)
        elif t == 'video':
            self.video(o, full=not light)
        elif t == 'doc' and not light:
            self.doc(o)
        elif t == 'audio_message' and not light:
            self.m.want('voice:' + oid(o), o.get('link_mp3') or o.get('link_ogg'), 'media/voice/%s.mp3' % oid(o))
        elif t == 'audio' and not light and o.get('url'):
            u = o['url']
            self.m.want('audio:' + oid(o), u, 'media/audio/%s.mp3' % oid(o), 'hls:mp3' if '.m3u8' in u else 'http')
        elif t == 'sticker':
            u = pick_image(o.get('images'), 256)
            if u:
                self.m.want('sticker:%s' % o.get('sticker_id'), u, 'media/stickers/%s.png' % o.get('sticker_id'))
        elif t == 'gift':
            u = o.get('thumb_256') or o.get('thumb_96')
            if u:
                self.m.want('gift:%s' % o.get('id'), u, 'media/gifts/%s%s' % (o.get('id'), url_ext(u, '.png')))
        elif t == 'link':
            self.photo(o.get('photo'))
        elif t in ('graffiti',):
            self.url(o.get('url'))
        elif t == 'market':
            self.url(o.get('thumb_photo'))
        elif t == 'article':
            self.photo(o.get('photo'))
        elif t == 'story':
            self.photo(o.get('photo'))
            self.video(o.get('video'), full=not light)
        elif t == 'poll':
            pass
        elif t == 'audio_playlist':
            self.url((o.get('photo') or {}).get('photo_300'))


def iter_post_atts(p):
    for a in p.get('attachments') or []:
        yield a
    for c in p.get('copy_history') or []:
        yield from iter_post_atts(c)


def iter_atts(m):
    """All attachments of a message, including forwarded / replied / wall posts inside."""
    for a in m.get('attachments') or []:
        yield a
        t = a.get('type')
        if t == 'wall':
            yield from iter_post_atts(a.get('wall') or {})
        elif t == 'wall_reply':
            yield from iter_atts(a.get('wall_reply') or {})
    for f in m.get('fwd_messages') or []:
        yield from iter_atts(f)
    if m.get('reply_message'):
        yield from iter_atts(m['reply_message'])


PEOPLE_KEYS = ('from_id', 'signer_id', 'initiator_id', 'receiver_id', 'author_id')


def collect_people(obj, out):
    """User / group ids that rendered API objects will show (authors, post owners)."""
    if isinstance(obj, dict):
        for k in PEOPLE_KEYS:
            v = obj.get(k)
            if isinstance(v, int) and v:
                out.add(v)
        if ('post_type' in obj or 'copy_history' in obj) and isinstance(obj.get('owner_id'), int):
            out.add(obj['owner_id'])
        for v in obj.values():
            if isinstance(v, (dict, list)):
                collect_people(v, out)
    elif isinstance(obj, list):
        for x in obj:
            collect_people(x, out)


# ----------------------------------------------------------------------------
# people: users, groups, chats
# ----------------------------------------------------------------------------

class People:
    def __init__(self, dst):
        self.path = os.path.join(dst, '_data', 'people.json')
        d = load_json(self.path, {})
        self.users = {int(k): v for k, v in d.get('users', {}).items()}
        self.groups = {int(k): v for k, v in d.get('groups', {}).items()}
        self.chats = {int(k): v for k, v in d.get('chats', {}).items()}

    def save(self):
        save_json(self.path, {'users': self.users, 'groups': self.groups, 'chats': self.chats})

    def fetch(self, api, ids, chat_peers):
        uids = sorted({i for i in ids if 0 < i < 2000000000})
        gids = sorted({-i for i in ids if i < 0})
        fields = 'photo_200,photo_max_orig,screen_name,deactivated'
        for part in chunks(uids, 500):
            try:
                for u in api.call('users.get', user_ids=','.join(map(str, part)), fields=fields):
                    self.users[u['id']] = u
            except VKError as e:
                log('   users.get failed:', e)
        for part in chunks(gids, 450):
            try:
                r = api.call('groups.getById', group_ids=','.join(map(str, part)), fields='photo_200,screen_name')
                for g in (r.get('groups', []) if isinstance(r, dict) else r):
                    self.groups[g['id']] = g
            except VKError as e:
                log('   groups.getById failed:', e)
        for part in chunks(sorted(chat_peers), 100):
            try:
                r = api.call('messages.getConversationsById', peer_ids=','.join(map(str, part)))
                for c in r.get('items', []):
                    self.chats[c['peer']['id']] = c
            except VKError as e:
                log('   messages.getConversationsById failed:', e)
        self.save()

    def name(self, i):
        if i is None:
            return ''
        if i >= 2000000000:
            c = self.chats.get(i) or {}
            return (c.get('chat_settings') or {}).get('title') or 'Беседа %d' % (i - 2000000000)
        if i > 0:
            u = self.users.get(i)
            return ('%s %s' % (u.get('first_name', ''), u.get('last_name', ''))).strip() if u else 'id%d' % i
        g = self.groups.get(-i)
        return g.get('name') if g else 'club%d' % -i

    def link(self, i):
        if i is None or i >= 2000000000:
            return None
        return '%s/%s%d' % (VK, 'id' if i > 0 else 'club', abs(i))

    def avatar_url(self, i, big=False):
        if i >= 2000000000:
            ph = ((self.chats.get(i) or {}).get('chat_settings') or {}).get('photo') or {}
            return ph.get('photo_200') or ph.get('photo_100')
        o = self.users.get(i) if i > 0 else self.groups.get(-i)
        if not o:
            return None
        return (o.get('photo_max_orig') if big else None) or o.get('photo_200') or o.get('photo_100')

    def want_avatars(self, media, ids, big_ids=()):
        for i in ids:
            u = self.avatar_url(i)
            if not u:
                continue
            if '/images/' in u and 'userapi' not in u and 'vkuserphoto' not in u:  # shared placeholder
                media.want('ava:%d' % i, u, 'media/avatars/static/%s%s' % (sha(u), url_ext(u, '.png')))
            else:
                media.want('ava:%d' % i, u, 'media/avatars/%d.jpg' % i)
        for i in big_ids:
            u = self.avatar_url(i, big=True)
            if u and '/images/' not in u:
                media.want('avabig:%d' % i, u, 'media/avatars/full/%d.jpg' % i)


# ----------------------------------------------------------------------------
# rendering API objects to html
# ----------------------------------------------------------------------------

MENTION = re.compile(r'\[(id|club|public|event)(\d+)(?::bp-\d+_\d+)?\|([^\]]+)\]')
URL_RX = re.compile(r'(https?://[^\s<>"\']+)')


class Render:
    def __init__(self, media, people, tz_offset, self_id):
        self.m = media
        self.pp = people
        self.tz = timezone(timedelta(seconds=tz_offset))
        self.self_id = self_id
        self.p = ''  # prefix from the current page to dst root

    # -- basics --
    def local(self, key):
        rel = self.m.path(key)
        return url_of(rel, self.p) if rel else None

    def date(self, ts):
        if not ts:
            return ''
        return datetime.fromtimestamp(ts, self.tz).strftime('%d.%m.%Y %H:%M')

    def text(self, s):
        if not s:
            return ''
        s = esc(s)
        s = URL_RX.sub(lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>' % (m.group(1), m.group(1)), s)
        s = MENTION.sub(lambda m: '<a href="%s/%s%s">%s</a>' % (VK, m.group(1), m.group(2), m.group(3)), s)
        return s.replace('\n', '<br>')

    def ava(self, i, cls='ex-ava'):
        if i is None:
            return ''
        src = self.local('ava:%d' % i)
        if src:
            return '<img class="%s" src="%s" alt="" loading="lazy">' % (cls, src)
        name = self.pp.name(i) or '?'
        return '<span class="%s ex-ava-ph">%s</span>' % (cls, esc(name[:1].upper()))

    def who(self, i):
        link = self.pp.link(i)
        name = esc(self.pp.name(i))
        inner = '%s<b>%s</b>' % (self.ava(i), name)
        return '<a class="ex-who" href="%s">%s</a>' % (link, inner) if link else '<span class="ex-who">%s</span>' % inner

    def head(self, author, ts, extra=''):
        return '<div class="ex-head">%s<span class="ex-date">%s</span>%s</div>' % (self.who(author), self.date(ts), extra)

    def comment(self, c, link=None):
        """A comment (wall, photo, video, board topic) as a card."""
        if not c:
            return ''
        extra = ' · <a href="%s" target="_blank">открыть в VK</a>' % esc(link) if link else ''
        if c.get('deleted'):
            return '<div class="ex-card ex-comment ex-dim">Комментарий удалён</div>'
        return '<div class="ex-card ex-comment">%s<div class="ex-text">%s</div>%s</div>' % (
            self.head(c.get('from_id'), c.get('date'), extra), self.text(c.get('text')),
            self.attachments(c.get('attachments'), 1))

    def photo_card(self, p):
        """A liked photo: author, date, the picture, its caption."""
        owner = p.get('owner_id')
        extra = ''
        if owner and owner < 0 and p.get('user_id') and p['user_id'] != 100:
            extra = ' · загрузил(а) %s' % esc(self.pp.name(p['user_id']))
        return '<div class="ex-card ex-photo-card">%s%s</div>' % (
            self.head(owner, p.get('date'), extra), self.photo(p, 'ex-like-thumb'))

    # -- attachments --
    def attachments(self, atts, depth=0):
        if not atts:
            return ''
        photos, rest = [], []
        for a in atts:
            t = a.get('type')
            if t == 'photo':
                photos.append(self.photo(a['photo']))
            else:
                rest.append(self.attachment(a, depth))
        out = ''
        if photos:
            out += '<div class="ex-gallery%s">%s</div>' % (' ex-many' if len(photos) > 1 else '', ''.join(photos))
        return out + ''.join(rest)

    def photo(self, p, cls=''):
        src = self.local('photo:' + oid(p))
        remote = best_photo_url(p)
        cap = '<div class="ex-cap">%s</div>' % self.text(p.get('text')) if p.get('text') else ''
        if src:
            return '<a class="ex-photo %s" href="%s" target="_blank"><img src="%s" loading="lazy" alt=""></a>%s' % (cls, src, src, cap)
        if remote:
            return '<a class="ex-photo ex-remote %s" href="%s" target="_blank" title="не скачано"><img src="%s" loading="lazy" alt=""></a>%s' % (
                cls, esc(remote), esc(remote), cap)
        return '<span class="ex-missing">[фото недоступно]</span>'

    def attachment(self, a, depth=0):
        t = a.get('type')
        o = a.get(t) or {}
        fn = getattr(self, 'a_' + t, None)
        if fn:
            try:
                return fn(o, depth)
            except Exception as e:  # a weird object must not break the whole page
                return '<div class="ex-card ex-missing">[%s: %s]</div>' % (esc(t), esc(e))
        return '<div class="ex-card ex-missing">[%s]</div>' % esc(t)

    def a_video(self, v, depth=0):
        key = oid(v)
        thumb = self.local('vthumb:' + key)
        file = self.local('video:' + key)
        title = esc(v.get('title') or 'Видео')
        dur = fmt_dur(v['duration']) if v.get('duration') else ''
        vk_link = '%s/video%s' % (VK, key)
        if file:
            body = '<video controls preload="none" src="%s"%s></video>' % (file, ' poster="%s"' % thumb if thumb else '')
        elif thumb:
            body = '<a href="%s" target="_blank" class="ex-vthumb"><img src="%s" loading="lazy" alt=""><span class="ex-play">▶</span></a>' % (vk_link, thumb)
        else:
            body = ''
        plat = ' · %s' % esc(v['platform']) if v.get('platform') else ''
        desc = '<div class="ex-desc">%s</div>' % self.text(v['description'][:600]) if v.get('description') else ''
        return '<div class="ex-card ex-video">%s<div class="ex-title">🎬 <a href="%s" target="_blank">%s</a> <span class="ex-dim">%s%s%s</span></div>%s</div>' % (
            body, vk_link, title, dur, plat, '' if file else ' · не скачано', desc)

    def a_doc(self, d, depth=0):
        prev = d.get('preview') or {}
        if prev.get('audio_msg'):
            return self.voice('voice:' + oid(d), prev['audio_msg'].get('duration'), None)
        f = self.local('doc:' + oid(d))
        title = esc(d.get('title') or 'файл')
        size = fmt_size(d.get('size'))
        href = f or esc(d.get('url') or '%s/doc%s' % (VK, oid(d)))
        if f and d.get('type') in (3, 4):  # gif / image
            return '<div class="ex-doc-img"><a href="%s" target="_blank"><img src="%s" loading="lazy" alt=""></a><div class="ex-dim">%s · %s</div></div>' % (f, f, title, size)
        if f and d.get('type') == 6:
            return '<div class="ex-card"><video controls preload="none" src="%s"></video><div class="ex-dim">%s · %s</div></div>' % (f, title, size)
        if f and d.get('type') == 5:
            return '<div class="ex-card">📎 %s<br><audio controls preload="none" src="%s"></audio></div>' % (title, f)
        miss = '' if f else ' <span class="ex-dim">· не скачано%s</span>' % (
            ' (%s)' % esc(self.m.failed.get('doc:' + oid(d), '')) if self.m.failed.get('doc:' + oid(d), '').startswith('skipped') else '')
        return '<div class="ex-card ex-doc"><a href="%s" target="_blank">📄 %s</a> <span class="ex-dim">%s · %s</span>%s</div>' % (
            href, title, esc((d.get('ext') or '').upper()), size, miss)

    def voice(self, key, duration, transcript):
        f = self.local(key)
        player = '<audio controls preload="none" src="%s"></audio>' % f if f else '<span class="ex-dim">не скачано</span>'
        tr = '<div class="ex-transcript">%s</div>' % self.text(transcript) if transcript else ''
        return '<div class="ex-card ex-voice">🎤 Голосовое сообщение <span class="ex-dim">%s</span><br>%s%s</div>' % (
            fmt_dur(duration), player, tr)

    def a_audio_message(self, o, depth=0):
        return self.voice('voice:' + oid(o), o.get('duration'), o.get('transcript'))

    def a_audio(self, o, depth=0):
        f = self.local('audio:' + oid(o))
        player = '<br><audio controls preload="none" src="%s"></audio>' % f if f else ''
        return '<div class="ex-card ex-audio">🎵 <b>%s</b> — %s <span class="ex-dim">%s</span>%s</div>' % (
            esc(o.get('artist')), esc(o.get('title')), fmt_dur(o.get('duration')), player)

    def a_audio_playlist(self, o, depth=0):
        cover = self.local('url:' + sha((o.get('photo') or {}).get('photo_300', ''))) if o.get('photo') else None
        rows = ''.join('<li>%s — %s <span class="ex-dim">%s</span></li>' % (
            esc(x.get('artist')), esc(x.get('title')), fmt_dur(x.get('duration'))) for x in (o.get('audios') or []))
        return '<div class="ex-card ex-playlist">%s<div class="ex-title">💿 %s <span class="ex-dim">%s треков</span></div><ol>%s</ol></div>' % (
            '<img class="ex-cover" src="%s" alt="">' % cover if cover else '', esc(o.get('title')), o.get('count', ''), rows)

    def a_sticker(self, o, depth=0):
        f = self.local('sticker:%s' % o.get('sticker_id'))
        if f:
            return '<img class="ex-sticker" src="%s" alt="стикер" loading="lazy">' % f
        u = pick_image(o.get('images'), 256)
        return '<img class="ex-sticker ex-remote" src="%s" alt="стикер">' % esc(u) if u else '<span class="ex-dim">[стикер]</span>'

    def a_gift(self, o, depth=0):
        f = self.local('gift:%s' % o.get('id'))
        return '<div class="ex-card ex-gift">🎁 Подарок<br>%s</div>' % (
            '<img src="%s" alt="" loading="lazy">' % f if f else '')

    def a_link(self, o, depth=0):
        ph = o.get('photo')
        img = ''
        if ph:
            src = self.local('photo:' + oid(ph))
            if src:
                img = '<img class="ex-link-img" src="%s" alt="" loading="lazy">' % src
        url = esc(o.get('url'))
        cap = esc(o.get('caption') or urllib.parse.urlparse(o.get('url') or '').netloc)
        desc = '<div class="ex-desc">%s</div>' % esc(o['description'][:300]) if o.get('description') else ''
        return '<a class="ex-card ex-link" href="%s" target="_blank" rel="noopener">%s<div><div class="ex-title">%s</div><div class="ex-dim">%s</div>%s</div></a>' % (
            url, img, esc(o.get('title') or o.get('url')), cap, desc)

    def a_poll(self, o, depth=0):
        total = o.get('votes') or 0
        rows = []
        mine = set(o.get('answer_ids') or [])
        for ans in o.get('answers') or []:
            rate = ans.get('rate') or 0
            rows.append('<div class="ex-poll-row%s"><div class="ex-poll-text">%s <span class="ex-dim">%s · %d%%</span></div><div class="ex-poll-bar"><i style="width:%.1f%%"></i></div></div>' % (
                ' ex-mine' if ans.get('id') in mine else '', esc(ans.get('text')), ans.get('votes', 0), round(rate), rate))
        flags = ', '.join(x for x in ['анонимный' if o.get('anonymous') else 'публичный',
                                      'несколько вариантов' if o.get('multiple') else '',
                                      'завершён' if o.get('closed') else ''] if x)
        return '<div class="ex-card ex-poll"><div class="ex-title">📊 %s</div><div class="ex-dim">Опрос · %s · %d голосов</div>%s</div>' % (
            esc(o.get('question')), flags, total, ''.join(rows))

    def a_call(self, o, depth=0):
        kind = 'Видеозвонок' if o.get('video') else 'Звонок'
        state = {'reached': '', 'canceled_by_initiator': 'отменён', 'canceled_by_receiver': 'отклонён'}.get(o.get('state'), o.get('state') or '')
        dur = fmt_dur(o['duration']) if o.get('duration') else ''
        who = esc(self.pp.name(o.get('initiator_id'))) if o.get('initiator_id') else ''
        return '<div class="ex-card ex-call">📞 %s <span class="ex-dim">%s</span></div>' % (
            kind, ' · '.join(x for x in [who, dur, state] if x))

    def a_story(self, o, depth=0):
        inner = ''
        if o.get('photo'):
            inner = self.photo(o['photo'])
        elif o.get('video'):
            inner = self.a_video(o['video'])
        if not inner:
            inner = '<span class="ex-dim">история недоступна%s</span>' % (' (истекла)' if o.get('is_expired') or o.get('expires_at') else '')
        return '<div class="ex-card ex-story">📖 История %s<br>%s</div>' % (esc(self.date(o.get('date'))), inner)

    def a_market(self, o, depth=0):
        img = self.local('url:' + sha(o.get('thumb_photo') or ''))
        price = esc((o.get('price') or {}).get('text'))
        return '<div class="ex-card ex-link">%s<div><div class="ex-title">🛍 %s</div><div class="ex-dim">%s</div></div></div>' % (
            '<img class="ex-link-img" src="%s" alt="">' % img if img else '', esc(o.get('title')), price)

    def a_article(self, o, depth=0):
        img = self.local('photo:' + oid(o['photo'])) if o.get('photo') else None
        return '<a class="ex-card ex-link" href="%s" target="_blank">%s<div><div class="ex-title">📰 %s</div><div class="ex-dim">%s</div></div></a>' % (
            esc(o.get('url')), '<img class="ex-link-img" src="%s" alt="">' % img if img else '', esc(o.get('title')), esc(o.get('owner_name')))

    def a_graffiti(self, o, depth=0):
        f = self.local('url:' + sha(o.get('url') or ''))
        return '<img class="ex-graffiti" src="%s" alt="граффити">' % (f or esc(o.get('url')))

    def a_wall(self, w, depth=0):
        return self.post(w, depth)

    def a_wall_reply(self, c, depth=0):
        link = '%s/wall%s_%s?reply=%s' % (VK, c.get('owner_id'), c.get('post_id'), c.get('id'))
        return '<div class="ex-card ex-comment">%s<div class="ex-text">%s</div>%s</div>' % (
            self.head(c.get('from_id'), c.get('date'), ' · <a href="%s" target="_blank">комментарий</a>' % link),
            self.text(c.get('text')), self.attachments(c.get('attachments'), depth + 1))

    def a_money_transfer(self, o, depth=0):
        return '<div class="ex-card">💸 Денежный перевод %s</div>' % esc((o.get('amount') or {}).get('text'))

    a_money_request = a_money_transfer

    def a_geo(self, g, depth=0):
        return self.geo(g)

    def geo(self, g):
        c = g.get('coordinates') or {}
        if isinstance(c, str):
            lat, lon = (c.split() + ['', ''])[:2]
        else:
            lat, lon = c.get('latitude'), c.get('longitude')
        place = (g.get('place') or {}).get('title') or ''
        return '<div class="ex-card">📍 <a href="https://www.openstreetmap.org/?mlat=%s&mlon=%s#map=16/%s/%s" target="_blank">%s %s, %s</a></div>' % (
            lat, lon, lat, lon, esc(place), lat, lon)

    def post(self, w, depth=0):
        if w.get('is_deleted'):
            return '<div class="ex-card ex-wall ex-dim">Запись удалена</div>'
        author = w.get('from_id') or w.get('owner_id')
        link = '%s/wall%s_%s' % (VK, w.get('owner_id') or w.get('to_id'), w.get('id'))
        extra = ' · <a href="%s" target="_blank">запись</a>' % link
        if w.get('signer_id'):
            extra += ' · %s' % esc(self.pp.name(w['signer_id']))
        body = '<div class="ex-text">%s</div>' % self.text(w.get('text')) if w.get('text') else ''
        body += self.attachments(w.get('attachments'), depth + 1)
        if depth < 6:
            for ch in w.get('copy_history') or []:
                body += self.post(ch, depth + 1)
        return '<div class="ex-card ex-wall">%s%s</div>' % (self.head(author, w.get('date'), extra), body)

    # -- messages --
    def message_extras(self, m, depth=0):
        out = ''
        if m.get('reply_message'):
            r = m['reply_message']
            out += '<div class="ex-reply"><div class="ex-label">↩ В ответ на</div>%s<div class="ex-text">%s</div>%s</div>' % (
                self.head(r.get('from_id'), r.get('date')), self.text(r.get('text')), self.attachments(r.get('attachments'), depth + 1))
        out += self.attachments(m.get('attachments'), depth)
        if m.get('geo'):
            out += self.geo(m['geo'])
        if m.get('fwd_messages'):
            out += self.forwarded(m['fwd_messages'], depth + 1)
        return out

    def forwarded(self, msgs, depth):
        if depth > 12:
            return '<div class="ex-dim">…ещё %d пересланных</div>' % len(msgs)
        items = ''.join('<div class="ex-fwd-msg">%s<div class="ex-text">%s</div>%s</div>' % (
            self.head(f.get('from_id'), f.get('date')), self.text(f.get('text')), self.message_extras(f, depth)) for f in msgs)
        return '<div class="ex-fwd"><div class="ex-label">↪ Пересланные сообщения (%d)</div>%s</div>' % (len(msgs), items)


# ----------------------------------------------------------------------------
# small russian text helpers for the pages
# ----------------------------------------------------------------------------

RU_MONTHS_GEN = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
                 'сентября', 'октября', 'ноября', 'декабря']
RU_MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
RU_WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
MSG_FORMS = ('сообщение', 'сообщения', 'сообщений')
# attachment types counted per conversation: (type, plural forms)
MEDIA_COUNTED = [('photo', ('фото', 'фото', 'фото')), ('video', ('видео', 'видео', 'видео')),
                 ('doc', ('файл', 'файла', 'файлов')), ('audio_message', ('голосовое', 'голосовых', 'голосовых')),
                 ('audio', ('трек', 'трека', 'треков')), ('link', ('ссылка', 'ссылки', 'ссылок'))]


def plural(n, forms):
    n = abs(n) % 100
    if 10 < n < 20:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


def num(n):
    return '{:,}'.format(n).replace(',', ' ')


def nplural(n, forms):
    return '%s %s' % (num(n), plural(n, forms))


def lday(lts):
    return datetime.fromtimestamp(lts, timezone.utc).date()


def ru_date(d, year=True):
    return '%d %s%s' % (d.day, RU_MONTHS_GEN[d.month - 1], ' %d' % d.year if year else '')


def short_date(lts):
    return lday(lts).strftime('%d.%m.%Y') if lts else ''


def peer_kind(peer):
    return 'chat' if peer >= 2000000000 else 'group' if peer < 0 else 'user'


# ----------------------------------------------------------------------------
# the enrichment run
# ----------------------------------------------------------------------------

SECTIONS = ('messages', 'wall', 'comments', 'albums', 'docs', 'profile', 'support', 'likes')
LIKE_KINDS = ('photo', 'wall', 'video', 'wall_reply', 'photo_comment', 'video_comment', 'topic_comment', 'market')
LIKE_LINK = {  # how each likes/<kind> page links its objects; the match is the storage key
    'photo': r'vk\.(?:ru|com)/photo(-?\d+_\d+)"',
    'wall': r'vk\.(?:ru|com)/wall(-?\d+_\d+)"',
    'video': r'vk\.(?:ru|com)/video(-?\d+_\d+)"',
    'wall_reply': r'vk\.(?:ru|com)/wall(-?\d+_\d+)\?reply=(\d+)',
    'photo_comment': r'vk\.(?:ru|com)/photo(-?\d+_\d+)\?reply=(\d+)',
    'video_comment': r'vk\.(?:ru|com)/video(-?\d+_\d+)\?reply=(\d+)',
    'topic_comment': r'vk\.(?:ru|com)/topic(-?\d+_\d+)\?reply=(\d+)',
    'market': r'vk\.(?:ru|com)/market\?w=product(-?\d+_\d+)"',
}
HERE = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))  # where assets/ lives


def clean_html(fragment):
    """VK's own markup of a support reply reduced to text, line breaks, bold and links."""
    t = re.sub(r'<img[^>]*\balt="([^"]*)"[^>]*>', r'\1', fragment or '')  # emoji images -> their character

    def link(m):
        href = html.unescape(m.group(1))
        away = re.search(r'away\.php\?to=([^&]+)', href)
        href = urllib.parse.unquote(away.group(1)) if away else urllib.parse.urljoin(VK + '/', href)
        return '<a href="%s" target="_blank" rel="noopener">%s</a>' % (esc(href), re.sub(r'<[^>]+>', '', m.group(2)))
    t = re.sub(r'<a\b[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', link, t, flags=re.S)
    t = re.sub(r'<(?!/?(?:a|b|br)\b)[^>]+>', '', t)  # every other tag goes
    return re.sub(r'<br\s*/?>', '<br>', t).strip()


def inner_div(page, cls, start=0):
    """Inner html of the first <div class="cls..."> in page[start:]."""
    k = page.find('class="%s' % cls, start)
    if k < 0:
        return ''
    a = page.rfind('<div', 0, k)
    b = div_end(page, a)
    return page[page.find('>', k) + 1:b - 6]


def parse_ticket(page):
    """A support ticket page (vk.ru/support?act=show) -> dict, or None when VK shows no ticket."""
    rows_at = page.find('id="tickets_reply_rows"')
    if rows_at < 0:
        return None
    status = re.search(r'id="helpdesk_ticket_status"[^>]*>(.*?)</span>', page, re.S)
    a = page.rfind('<div', 0, rows_at)
    rows = page[a:div_end(page, a)]
    replies = []
    for x, y in div_blocks(rows, '<div class="tickets_reply_row'):
        row = rows[x:y]
        avatar = re.search(r'class="tickets_image".*?<img[^>]*src="([^"]+)"', row, re.S)
        author = re.search(r'<span class="tickets_author">(.*?)</span>', row, re.S)
        attachments = [{'href': urllib.parse.urljoin(VK + '/', html.unescape(h)), 'thumb': html.unescape(img),
                        'name': html_text(name)}
                       for h, img, name in re.findall(r'<a href="([^"]+)"[^>]*class="tr_attach">.*?<img[^>]*src="([^"]+)"'
                                                      r'.*?(?:class="tr_doc_photo_hint">(.*?)</div>)?\s*</a>', row, re.S)]
        fields = []
        for fx, fy in div_blocks(row, '<div class="tickets_extra_field clear_fix'):
            f = row[fx:fy]
            label = re.search(r'tickets_extra_field__label">(.*?)</span>', f, re.S)
            images = [{'href': html.unescape(h), 'thumb': html.unescape(i)} for h, i in re.findall(
                r'<a href="([^"]+)"[^>]*>\s*<img class="tickets_extra_field__img" src="([^"]+)"', f)]
            value = f[f.find('</span>') + 7:] if label else f
            value = re.sub(r'<a href="[^"]+"[^>]*>\s*<img[^>]*>\s*</a>', '', value)
            fields.append({'label': html_text(label.group(1)) if label else '', 'value': clean_html(value), 'images': images})
        text = inner_div(row, 'tickets_reply_text').split('<div class="tickets_extra_fields')[0]
        replies.append({'author': html_text(author.group(1)) if author else '',
                        'mine': bool(author and 'href=' in author.group(1)),  # the ticket's author links to a profile
                        'avatar': urllib.parse.urljoin(VK + '/', html.unescape(avatar.group(1))) if avatar else '',
                        'date': html_text(inner_div(row, 'tickets_reply_actions__date')),
                        'text': clean_html(text), 'attachments': attachments, 'fields': fields})
    return {'title': html_text(inner_div(page, 'tickets_header_title')), 'status': html_text(status.group(1)) if status else '',
            'replies': replies, 'fetched': int(time.time())}


def like_key(m):
    return '_'.join(g for g in m.groups() if g)


class Enricher:
    def __init__(self, args):
        self.a = args
        self.src = Archive(args.src)
        self.dst = os.path.abspath(args.dst or self.src.src.rstrip('/\\') + '_enriched')
        if os.path.abspath(self.dst) == self.src.src:
            sys.exit('Destination must differ from the source archive')
        os.makedirs(os.path.join(self.dst, '_data'), exist_ok=True)
        self.data = lambda *p: os.path.join(self.dst, '_data', *p)
        self.self_id = self.src.self_id
        self.api = None
        self.cookies = []
        self.tmpdir = tempfile.mkdtemp(prefix='vk_archive_enrich_')
        self.people = People(self.dst)
        self.stats = Counter()
        self.peers = self.src.peers()
        self.fetch_peers = set(self.peers)
        if args.peers:
            self.fetch_peers = {int(x) for x in args.peers.split(',')}
        self.sections = args.sections
        self.msgs = {}  # peer -> {id: message from the API}
        self.tz_votes = Counter()
        self.archive_names = self.src.peer_names()
        self.wall, self.my_comments, self.photos, self.docs, self.stories = {'posts': {}, 'comments': {}}, {}, {}, {}, {}
        self.support = {}
        self.likes = {k: {} for k in LIKE_KINDS}
        self._counts = {}

    def online(self, section):
        return self.api is not None and section in self.sections

    # -- setup --
    def connect(self):
        disabled = self.a.no_media
        if self.a.offline:
            self.media = Media(self.dst, [], self.a.workers, disabled=disabled)
            self.want = Want(self.media, self.a)
            return
        if self.a.token in ('ask', '-'):
            self.a.token = ask_token()
        self.cookies, where = ([], '') if self.a.token else load_cookies(self.a.browser, self.a.cookies)
        if self.cookies:
            log('Using the vk.ru session from', where)
        elif not self.a.token:
            sys.exit('No logged-in vk.ru session found in %s. Log in to vk.ru there, or use --browser NAME, '
                     '--cookies cookies.txt or --token ask (see README).' % where)
        self.api = VKApi(self.cookies, self.a.token)
        if not self.a.token:
            self.api.refresh()
        me = self.api.call('users.get')[0]
        if self.self_id and me['id'] != self.self_id:
            sys.exit('The browser is logged in as id%d but the archive belongs to id%d.' % (me['id'], self.self_id))
        self.self_id = me['id']
        log('Logged in as id%d' % me['id'])
        ytc = None
        if self.cookies and self.a.external_videos:
            ytc = os.path.join(self.tmpdir, 'cookies.txt')
            write_netscape_cookies(self.cookies, ytc)
        self.media = Media(self.dst, self.cookies, self.a.workers, self.a.max_file_mb, self.a.max_video_mb, ytc,
                           self.a.video_workers, self.a.segments, disabled)
        self.want = Want(self.media, self.a, self.api)

    def flush(self, label):
        if self.api:
            self.want.resolve_videos()
        self.media.flush(label)

    # -- phase: messages --
    def scan(self):
        t0 = time.time()
        for peer in self.peers:
            self.src.scan_peer(peer)
        log('Archive: %d conversations, %d messages (read in %.0fs)' % (
            len(self.peers), sum(len(c.msgs) for c in self.src.convs.values()), time.time() - t0))

    def do_messages(self):
        fetch = self.online('messages')
        if fetch:
            log('Messages: fetching attachments of %d conversations' % len(self.fetch_peers & set(self.peers)))
        todo = [p for p in self.peers if p in self.fetch_peers]
        for n, peer in enumerate(self.peers, 1):
            path = self.data('messages', '%d.json' % peer)
            conv = self.src.convs[peer]
            if not fetch or peer not in self.fetch_peers:
                self.msgs[peer] = {int(k): v for k, v in load_json(path, {}).get('items', {}).items()}
                continue
            ids = [m[0] for m in conv.msgs if m[4] or self.a.all_messages]
            got = {}
            for group in chunks(list(chunks(ids, 100)), 10):
                res = self.api.execute([('messages.getById', {'message_ids': ','.join(map(str, c))}) for c in group])
                for r in res:
                    for m in (r or {}).get('items', []):
                        got[m['id']] = m
            save_json(path, {'peer': peer, 'fetched': int(time.time()), 'requested': len(ids), 'items': got})
            self.msgs[peer] = got
            self.stats['messages requested'] += len(ids)
            self.stats['messages returned by API'] += len(got)
            for mid, lts, *_ in conv.msgs:  # learn the archive's timezone
                if lts and mid in got and sum(self.tz_votes.values()) < 20000:
                    self.tz_votes[round((lts - got[mid]['date']) / 900) * 900] += 1
            for m in got.values():
                for a in iter_atts(m):
                    self.want.attachment(a)
            missing = {m[0] for m in conv.msgs if m[4] and m[0] not in got}
            if missing:
                self.want_archive_fallback(peer, missing)
            log('[%d/%d] peer %d: %d/%d messages from API, %d new files queued' % (
                todo.index(peer) + 1, len(todo), peer, len(got), len(ids), len(self.media.pending)))
            self.flush('peer %d' % peer)

    def want_archive_fallback(self, peer, missing):
        """Messages VK no longer returns: grab photos / voice links the archive itself has."""
        for name in self.src.convs[peer].pages:
            parts = MSG_SPLIT.split(read_page(self.src.path('messages', str(peer), name)))
            for i in range(1, len(parts), 2):
                if int(parts[i]) in missing:
                    for u in re.findall(r"attachment__link' href='(https://[^']*(?:userapi\.com|vkuserphoto\.ru)[^']*)'", parts[i + 1]):
                        self.want.url(html.unescape(u), 'archive')

    # -- phase: people --
    def do_people(self):
        ids = {self.self_id}
        for peer in self.peers:
            if peer < 2000000000:
                ids.add(peer)
            collect_people(list(self.msgs.get(peer, {}).values()), ids)
        for d in (self.wall.get('posts', {}), self.wall.get('comments', {}), self.my_comments):
            collect_people(list(d.values()), ids)
        for kind, store in self.likes.items():
            collect_people(list(store.values()), ids)
            if kind in ('photo', 'video', 'market'):
                for o in store.values():
                    ids.add(o.get('owner_id') or 0)
                    if (o.get('user_id') or 100) != 100:
                        ids.add(o['user_id'])
        archive_ids = self.src.all_profile_ids()
        ids |= archive_ids | self.src.header_ids
        ids.discard(0)
        ids.discard(None)
        chat_peers = [p for p in self.peers if p >= 2000000000]
        if self.api:
            log('People: %d users/groups, %d chats' % (len(ids), len(chat_peers)))
            self.people.fetch(self.api, ids, chat_peers)
            big = {self.self_id} | {p for p in self.peers if 0 < p < 2000000000} | {i for i in archive_ids if i > 0}
            self.people.want_avatars(self.media, ids | set(chat_peers), big)
            self.flush('avatars')

    # -- phase: wall, photos, docs, comments, gifts, likes --
    def do_wall(self):
        path = self.data('wall.json')
        self.wall = load_json(path, {'posts': {}, 'comments': {}})
        if not self.online('wall'):
            return
        posts = sorted(set(self.src.find_links(self.src.pages('wall', r'wall\d+\.html$'),
                                               r'class="post__link fl_l" href="https?://vk\.(?:ru|com)/wall(-?\d+_\d+)"')))
        log('Wall: %d posts' % len(posts))
        for part in chunks(posts, 100):
            try:
                r = self.api.call('wall.getById', posts=','.join(part), copy_history_depth=3, extended=1)
                for p in (r.get('items', []) if isinstance(r, dict) else r):
                    self.wall['posts']['%s_%s' % (p['owner_id'], p['id'])] = p
            except VKError as e:
                log('   wall.getById failed:', e)
        for d in sorted(os.listdir(self.src.path('wall')) if os.path.isdir(self.src.path('wall')) else []):
            if not d.isdigit():
                continue
            key, items, offset = '%d_%s' % (self.self_id, d), {}, 0
            while True:
                try:
                    r = self.api.call('wall.getComments', owner_id=self.self_id, post_id=int(d), count=100, offset=offset,
                                      sort='asc', extended=1, thread_items_count=10)
                except VKError as e:
                    log('   wall.getComments %s failed: %s' % (key, e))
                    break
                for c in r.get('items', []):
                    items[str(c['id'])] = c
                    for t in (c.get('thread') or {}).get('items', []):
                        items[str(t['id'])] = t
                offset += 100
                if offset >= r.get('count', 0):
                    break
            self.wall['comments'][key] = items
        save_json(path, self.wall)
        for p in self.wall['posts'].values():
            for a in iter_post_atts(p):
                self.want.attachment(a)
        for cs in self.wall['comments'].values():
            for c in cs.values():
                for a in iter_atts(c):
                    self.want.attachment(a)
        self.flush('wall')

    def do_my_comments(self):
        path = self.data('my_comments.json')
        self.my_comments = load_json(path, {})
        if not self.online('comments'):
            return
        refs = sorted(set(self.src.find_links(self.src.series('comments/comments.html'),
                                              r'href="https?://vk\.(?:ru|com)/wall(-?\d+)_\d+\?reply=(\d+)')))
        log('My comments: %d' % len(refs))
        for part in chunks(refs, 25):
            res = self.api.execute([('wall.getComment', {'owner_id': int(o), 'comment_id': int(c)}) for o, c in part])
            for (o, c), r in zip(part, res):
                items = (r or {}).get('items') or []
                if items:
                    self.my_comments['%s_%s' % (o, c)] = items[0]
        save_json(path, self.my_comments)
        for c in self.my_comments.values():
            for a in iter_atts(c):
                self.want.attachment(a)
        self.flush('comments')

    def do_photos(self):
        path = self.data('photos.json')
        self.photos = load_json(path, {})
        rels = self.src.pages('photos/photo-albums')
        found = self.src.find_links(rels, r'<a href="https?://vk\.(?:ru|com)/photo(-?\d+_\d+)"><img src="([^"]+)"')
        self.album_src = {k: html.unescape(u) for k, u in found}
        if not self.online('albums'):
            return
        log('Photo albums: %d photos' % len(self.album_src))
        for part in chunks(sorted(self.album_src), 100):
            try:
                for p in self.api.call('photos.getById', photos=','.join(part), extended=0):
                    self.photos[oid(p)] = p
            except VKError as e:
                log('   photos.getById failed:', e)
        save_json(path, self.photos)
        for k, u in self.album_src.items():
            if k in self.photos:
                self.want.photo(self.photos[k])
            else:  # API did not return it: the archive url may still work
                self.media.want('photo:' + k, u, 'media/photos/%s.jpg' % k)
        self.flush('albums')

    def do_docs(self):
        path = self.data('docs.json')
        self.docs = load_json(path, {})
        if not self.online('docs'):
            return
        try:
            r = self.api.call('docs.get', owner_id=self.self_id, count=2000)
            for d in r.get('items', []):
                self.docs[oid(d)] = d
        except VKError as e:
            log('   docs.get failed:', e)
        wanted = set(self.src.find_links([r for r in ['profile/documents.html'] if self.src.exists(r)],
                                         r'vk\.(?:ru|com)/doc(-?\d+_\d+)'))
        for part in chunks([k for k in wanted if k not in self.docs], 100):
            try:
                for d in self.api.call('docs.getById', docs=','.join(part)):
                    self.docs[oid(d)] = d
            except VKError as e:
                log('   docs.getById failed:', e)
        save_json(path, self.docs)
        log('Documents: %d' % len(self.docs))
        for d in self.docs.values():
            self.want.doc(d)
        self.flush('documents')

    def do_profile(self):
        self.stories = load_json(self.data('stories.json'), {})
        if not self.online('profile'):
            return
        rels = [r for r in ('profile/gifts.html',) if self.src.exists(r)]
        for u in self.src.find_links(rels, r'<img src="(https?://[^"]+)"'):
            u = html.unescape(u)
            m = re.search(r'/images/gift/(\d+)/\d+\.(png|jpg)', u)
            if m:
                self.media.want('gift:' + m.group(1), [re.sub(r'/\d+\.(png|jpg)$', r'/256.\1', u), u],
                                'media/gifts/%s.%s' % m.groups())
            else:
                self.want.url(u, 'gifts')
        stories = self.src.find_links([r for r in ('profile/stories.html',) if self.src.exists(r)],
                                      r'vk\.(?:ru|com)/story(-?\d+_\d+)')
        if stories:
            try:
                r = self.api.call('stories.getById', stories=','.join(stories))
                for st in r.get('items', []):
                    self.stories[oid(st)] = st
                    self.want.attachment({'type': 'story', 'story': st})
            except VKError as e:
                log('   stories.getById failed:', e)
            save_json(self.data('stories.json'), self.stories)
        self.flush('gifts & stories')

    def like_refs(self, kind):
        rx = re.compile(LIKE_LINK[kind])
        out = []
        for rel in self.src.series('likes/%s/likes0.html' % kind) if self.src.exists('likes/%s/likes0.html' % kind) else []:
            out += [m for m in rx.finditer(read_page(self.src.path(rel)))]
        return out

    def do_support(self):
        """Support requests: the export lists only their titles; the conversations live on vk.ru pages."""
        path = self.data('support.json')
        self.support = load_json(path, {})
        rel = 'other/support.html'
        if not self.online('support') or not self.src.exists(rel):
            return
        if not self.cookies:
            log('Support requests: skipped, they need a browser session (cookies), not a token')
            return
        ids = re.findall(r'support\?act=show&(?:amp;)?id=(\d+)', read_page(self.src.path(rel)))
        log('Support requests: %d' % len(ids))
        os.makedirs(self.data('support'), exist_ok=True)
        for tid in ids:
            try:
                page = self.api.http.get('%s/support?act=show&id=%s' % (VK, tid), timeout=60).text
            except requests.RequestException as e:
                log('   support request %s: %s' % (tid, type(e).__name__))
                continue
            ticket = parse_ticket(page)
            if ticket is None:  # deleted or no longer shown: keep what an earlier run saved
                self.support.setdefault(tid, {'missing': True})
                continue
            a = page.rfind('<div', 0, page.find('tickets_header clear_fix'))
            with open(self.data('support', '%s.html' % tid), 'w', encoding='utf-8') as f:  # VK's own markup, for the record
                f.write(page[a:page.find('tickets_post_field_wrap', a)])
            self.support[tid] = ticket
            time.sleep(0.4)
        save_json(path, self.support)
        for t in self.support.values():
            for r in t.get('replies', []):
                self.want.url(r.get('avatar'), 'support')
                for att in r['attachments']:
                    self.want.url(att['thumb'], 'support')
                    ext = os.path.splitext(att['name'])[1].lower() or '.jpg'
                    self.media.want('url:' + sha(att['href']), att['href'], 'media/support/%s%s' % (sha(att['href']), ext))
                for f in r['fields']:
                    for img in f['images']:
                        self.want.url(img['href'], 'support')
        self.flush('support')

    def do_likes(self):
        path = self.data('likes.json')
        saved = load_json(path, {})
        for k in LIKE_KINDS:
            self.likes[k] = saved.get(k, {})
        if not self.online('likes'):
            return
        for kind in LIKE_KINDS:
            store = self.likes[kind]
            refs = {like_key(m): m for m in self.like_refs(kind)}
            new = sorted(k for k in refs if k not in store)
            if not refs:
                continue
            log('Likes/%s: %d, %d new' % (kind, len(refs), len(new)))
            calls, keys = [], []
            if kind in ('photo', 'wall', 'video', 'market'):
                for part in chunks(new, 100):
                    keys.append(None)
                    if kind == 'photo':
                        calls.append(('photos.getById', {'photos': ','.join(part)}))
                    elif kind == 'wall':
                        calls.append(('wall.getById', {'posts': ','.join(part), 'copy_history_depth': 2}))
                    elif kind == 'video':
                        calls.append(('video.get', {'videos': ','.join(part), 'count': 200}))
                    else:
                        calls.append(('market.getById', {'item_ids': ','.join(part), 'extended': 1}))
            else:  # one comment per call
                for k in new:
                    owner, obj, cid = refs[k].group(1).split('_') + [refs[k].group(2)]
                    keys.append(k)
                    if kind == 'wall_reply':
                        calls.append(('wall.getComment', {'owner_id': int(owner), 'comment_id': int(cid)}))
                    elif kind == 'photo_comment':
                        calls.append(('photos.getComments', {'owner_id': int(owner), 'photo_id': int(obj),
                                                              'start_comment_id': int(cid), 'count': 1}))
                    elif kind == 'video_comment':
                        calls.append(('video.getComments', {'owner_id': int(owner), 'video_id': int(obj),
                                                             'start_comment_id': int(cid), 'count': 1}))
                    else:
                        calls.append(('board.getComments', {'group_id': abs(int(owner)), 'topic_id': int(obj),
                                                             'start_comment_id': int(cid), 'count': 1}))
            per = 10 if kind == 'wall' else 25
            for gi, group in enumerate(chunks(calls, per)):
                for ci, r in enumerate(self.api.execute(group)):
                    key = keys[gi * per + ci]
                    items = r.get('items', []) if isinstance(r, dict) else (r or [])
                    if key is None:
                        for o in items:
                            store[oid(o)] = o
                    elif items and str(items[0].get('id')) == key.rsplit('_', 1)[1]:
                        store[key] = items[0]
                    else:
                        store[key] = {'deleted': True}
                if gi % 20 == 19:  # progress survives an interruption; the file is big, so not every time
                    save_json(path, self.likes)
            save_json(path, self.likes)
        for p in self.likes['photo'].values():
            self.want.photo(p)
        for p in self.likes['wall'].values():
            for a in iter_post_atts(p):
                self.want.attachment(a, light=True)
        for v in self.likes['video'].values():
            self.want.video(v, full=False)
        for kind in ('wall_reply', 'photo_comment', 'video_comment', 'topic_comment'):
            for c in self.likes[kind].values():
                for a in c.get('attachments') or []:
                    self.want.attachment(a, light=True)
        for m in self.likes['market'].values():
            self.want.url(m.get('thumb_photo'))
        self.flush('likes')

    # -- phase: render --
    def detect_tz(self):
        saved = load_json(self.data('meta.json'), {})
        off = self.tz_votes.most_common(1)[0][0] if self.tz_votes else saved.get('tz_offset', 3 * 3600)
        saved.update(tz_offset=off, self_id=self.self_id, updated=int(time.time()))
        save_json(self.data('meta.json'), saved)
        return off

    def render_all(self):
        off = self.detect_tz()
        log('Rendering pages (archive timezone UTC%+.1f)' % (off / 3600))
        self.r = Render(self.media, self.people, off, self.self_id)
        for name in os.listdir(self.src.src):  # static files
            p = self.src.path(name)
            if os.path.isfile(p) and not name.endswith('.html'):
                shutil.copy2(p, os.path.join(self.dst, name))
        for name in ('enrich.css', 'enrich.js'):
            shutil.copy2(os.path.join(HERE, 'assets', name), os.path.join(self.dst, name))
        self.conv_stats = {peer: self.conversation_stats(peer) for peer in self.peers}
        t0, n = time.time(), 0
        for peer in self.peers:
            n += self.render_conversation(peer)
        log('   conversations: %d pages in %.0fs' % (n, time.time() - t0))
        n = self.render_other_pages()
        self.write_messages_index()
        self.write_search()
        self.write_report()
        log('Rendered %d other pages' % n)

    def finish(self, rel, t):
        depth = rel.count('/')
        t = self.common(t, depth)
        out = os.path.join(self.dst, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            f.write(t)

    def common(self, t, depth):
        p = '../' * depth
        t = re.sub(r'<meta charset="windows-1251">', '<meta charset="utf-8">', t, count=1, flags=re.I)
        if 'enrich.css' not in t:
            t = re.sub(r'(<link rel="stylesheet" type="text/css" href="(?:\.\./)*style\.css">)',
                       r'\1\n  <link rel="stylesheet" type="text/css" href="%senrich.css">\n'
                       r'  <script src="%senrich.js" defer></script>' % (p, p), t, count=1)
        if '<meta name="viewport"' not in t:
            t = t.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n  <meta name="viewport" content="width=device-width, initial-scale=1">', 1)
        t = t.replace('<body>', '<body data-root="%s">' % p, 1)
        t = t.replace('<div class="top_home_logo"></div>', '<a class="ex-home" href="%sindex.html" title="На главную"><div class="top_home_logo"></div></a>' % p, 1)

        def vk_obj(m):
            q, kind, key = m.group(1), m.group(2), m.group(3)
            local = self.r.local({'photo': 'photo:', 'doc': 'doc:', 'video': 'video:'}[kind] + key)
            if not local:
                return m.group(0)
            return 'href=%s%s%s data-vk=%s%s/%s%s%s' % (q, local, q, q, VK, kind, key, q)
        t = re.sub(r'''href=(["'])https?://vk\.(?:ru|com)/(photo|doc|video)(-?\d+_\d+)(?:\?[^"']*)?\1''', vk_obj, t)

        def cdn(m):
            local = self.r.local('url:' + sha(html.unescape(m.group(2))))
            return 'src=%s%s%s' % (m.group(1), local, m.group(1)) if local else m.group(0)
        return re.sub(r'''src=(["'])(https?://[^"']*(?:userapi\.com|vkuserphoto\.ru)[^"']*)\1''', cdn, t)

    # -- generic page pieces --
    def pager(self, files, cur, kind='list'):
        if len(files) < 2:
            return ''
        labels = {'messages': ('«', '‹ новее', 'старее ›', '»', 'Самые новые', 'Самые старые'),
                  'list': ('«', '‹', '›', '»', 'Первая страница', 'Последняя страница')}[kind]

        def a(i, label, cls, title=''):
            if 0 <= i < len(files) and i != cur:
                return '<a class="%s" href="%s" title="%s">%s</a>' % (cls, esc(files[i]), title, label)
            return '<span class="%s ex-off">%s</span>' % (cls, label)
        data = '' if kind == 'messages' else " data-files='%s'" % esc(json.dumps(files))
        return '<div class="ex-pager" data-cur="%d" data-kind="%s"%s>%s%s<span class="ex-pg-label">%d / %d</span>%s%s</div>' % (
            cur, kind, data, a(0, labels[0], 'ex-pg-first', labels[4]), a(cur - 1, labels[1], 'ex-pg-prev'),
            cur + 1, len(files), a(cur + 1, labels[2], 'ex-pg-next'), a(len(files) - 1, labels[3], 'ex-pg-last', labels[5]))

    def with_pager(self, t, pager, top_extra=''):
        """Drop the archive's own pagination; put ours (and top_extra) above and below the list."""
        i = t.find('<div class="pagination')
        if i >= 0:
            t = t[:i] + t[div_end(t, i):]
        w = t.find('<div class="wrap_page_content">')
        if w < 0:
            return t
        e = div_end(t, w)
        bottom = pager.replace('class="ex-pager"', 'class="ex-pager ex-pager-bottom"', 1) if pager else ''
        t = t[:e - 6] + bottom + t[e - 6:]
        k = w + len('<div class="wrap_page_content">')
        return t[:k] + top_extra + pager + t[k:]

    def replace_attachments(self, chunk, rendered):
        """Swap the archive's attachment stubs in an item for rendered html."""
        blocks = div_blocks(chunk, '<div class="attachment">')
        if blocks:
            first, last = blocks[0][0], blocks[-1][1]
            keep_between = ''.join(chunk[blocks[i][1]:blocks[i + 1][0]] for i in range(len(blocks) - 1)
                                   if chunk[blocks[i][1]:blocks[i + 1][0]].strip())
            return chunk[:first] + '<div class="ex-rich">' + rendered + '</div>' + keep_between + chunk[last:]
        k = chunk.find('<div class="kludges">')
        if k >= 0 and rendered:
            k += len('<div class="kludges">')
            return chunk[:k] + '<div class="ex-rich">' + rendered + '</div>' + chunk[k:]
        return chunk

    def fallback_attachments(self, chunk):
        """Message VK did not return: show what the archive links to, from our local copy."""
        def one(m):
            u = html.unescape(m.group(1))
            local = self.r.local('url:' + sha(u))
            if not local:
                return m.group(0)
            if '.mp3' in u or '.ogg' in u or 'psv' in u:
                return '<audio controls preload="none" src="%s"></audio>' % local
            return '<a class="ex-photo" href="%s" target="_blank"><img src="%s" loading="lazy" alt=""></a>' % (local, local)
        return re.sub(r"<a class='attachment__link' href='(https://[^']*(?:userapi\.com|vkuserphoto\.ru)[^']*)'>[^<]*</a>", one, chunk)

    # -- conversations --
    def conversation_stats(self, peer):
        conv = self.src.convs[peer]
        times = [m[1] for m in conv.msgs if m[1]]
        media = Counter()
        for m in self.msgs.get(peer, {}).values():
            for a in iter_atts(m):
                media[a.get('type')] += 1
        return {'count': len(conv.msgs), 'first': min(times) if times else 0, 'last': max(times) if times else 0,
                'media': media, 'mine': sum(1 for m in conv.msgs if m[2] == self.self_id)}

    def peer_name(self, peer):
        known = peer in self.people.users or -peer in self.people.groups or peer in self.people.chats
        return self.people.name(peer) if known else (self.archive_names.get(peer) or self.people.name(peer))

    def media_line(self, media, sep=' · '):
        return sep.join(nplural(media[t], forms) for t, forms in MEDIA_COUNTED if media.get(t))

    def conv_bar(self, peer, page_prefix=''):
        st = self.conv_stats[peer]
        r = self.r
        parts = [nplural(st['count'], MSG_FORMS)]
        if st['media']:
            parts.append(self.media_line(st['media']))
        if st['first']:
            parts.append('%s — %s' % (short_date(st['first']), short_date(st['last'])))
        links = '<a class="ex-chip" href="%smedia.html">Медиа</a>' % page_prefix
        if self.a.github_activity:
            links += '<a class="ex-chip" href="%sstats.html">Статистика</a>' % page_prefix
        links += ('<form class="ex-conv-search" action="%ssearch.html" method="get"><input type="hidden" name="peer" value="%d">'
                  '<input type="search" name="q" placeholder="Поиск в переписке"></form>') % (r.p, peer)
        link = self.people.link(peer)
        title = '<a href="%s" target="_blank">%s</a>' % (link, esc(self.peer_name(peer))) if link else esc(self.peer_name(peer))
        return ('<div class="ex-conv-bar">%s<div class="ex-conv-info"><div class="ex-conv-name">%s</div>'
                '<div class="ex-dim">%s</div></div><div class="ex-conv-links">%s</div></div>') % (
            r.ava(peer, 'ex-ava ex-ava-lg'), title, ' · '.join(parts), links)

    def render_conversation(self, peer):
        conv = self.src.convs[peer]
        if not conv.pages:
            return 0
        api = self.msgs.get(peer, {})
        ranges = [[0, 0] for _ in conv.pages]  # newest, oldest local time per page
        lts_of = {}
        for mid, lts, author, pi, _, _ in conv.msgs:
            lts_of[mid] = lts
            if lts:
                rg = ranges[pi]
                rg[0] = max(rg[0], lts)
                rg[1] = min(rg[1], lts) if rg[1] else lts
        folder = os.path.join(self.dst, 'messages', str(peer))
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'nav.js'), 'w', encoding='utf-8') as f:
            f.write('window.EXNAV=%s;\n' % json.dumps({'peer': peer, 'files': conv.pages, 'newest': [x[0] for x in ranges],
                                                       'oldest': [x[1] for x in ranges]}))
        self.r.p = '../../'
        bar = self.conv_bar(peer)
        for pi, name in enumerate(conv.pages):
            t = read_page(self.src.path('messages', str(peer), name))
            t = self.tx_messages(t, peer, api, lts_of)
            t = self.with_pager(t, self.pager(conv.pages, pi, 'messages'), bar)
            t = t.replace('</body>', '<script src="nav.js"></script>\n</body>', 1)
            self.finish('messages/%d/%s' % (peer, name), t)
        self.write_gallery(peer)
        if self.a.github_activity:
            self.write_stats(peer)
        return len(conv.pages)

    def tx_messages(self, t, peer, api, lts_of):
        parts = MSG_SPLIT.split(t)
        out = [parts[0]]
        prev_day = None
        for i in range(1, len(parts), 2):
            mid, chunk = int(parts[i]), parts[i + 1]
            m = api.get(mid)
            hdr_end = chunk.find('</div>')
            author = m.get('from_id') if m else link_owner(chunk[:hdr_end])
            if author is None:
                author = self.self_id
            chunk = chunk.replace('<div class="message__header">', '<div class="message__header">' + self.r.ava(author), 1)
            if m:
                rendered = self.r.message_extras(m)
                if rendered:
                    chunk = self.replace_attachments(chunk, rendered)
            else:
                chunk = self.fallback_attachments(chunk)
            lts = lts_of.get(mid)
            if lts:
                d = lday(lts)
                if d != prev_day:  # day separator before this message's item
                    k = out[-1].rfind('<div class="item">')
                    if k >= 0:
                        sep = '<div class="ex-day" id="d%s"><span>%s</span></div>' % (d.strftime('%Y%m%d'), ru_date(d))
                        out[-1] = out[-1][:k] + sep + out[-1][k:]
                    prev_day = d
            out.append('<div class="message" data-id="%d" id="m%d" data-t="%d">' % (mid, mid, lts or 0) + chunk)
        return ''.join(out)

    def write_gallery(self, peer):
        msgs = self.msgs.get(peer, {})
        conv = self.src.convs[peer]
        page_of = {m[0]: conv.pages[m[3]] for m in conv.msgs}
        r = self.r
        r.p = '../../'
        photos, videos, docs, voices, audios, links = [], [], [], [], [], []
        for mid in sorted(msgs, reverse=True):
            m = msgs[mid]
            anchor = '%s#m%d' % (page_of.get(mid, conv.pages[0]), mid)
            back = '<a class="ex-back" href="%s">→ к сообщению</a>' % anchor
            for a in iter_atts(m):
                t = a.get('type')
                o = a.get(t) or {}
                if t == 'photo':
                    src = r.local('photo:' + oid(o))
                    if src:
                        photos.append('<a class="ex-photo" href="%s" data-msg="%s" data-tip="%s"><img src="%s" loading="lazy" alt=""></a>' % (
                            src, anchor, r.date(m.get('date')), src))
                elif t == 'video':
                    videos.append('<div class="ex-gal-item">%s%s</div>' % (r.a_video(o), back))
                elif t == 'doc':
                    docs.append('<div class="ex-gal-item">%s%s</div>' % (r.a_doc(o), back))
                elif t == 'audio_message':
                    voices.append('<div class="ex-gal-item">%s <span class="ex-dim">%s</span> %s</div>' % (
                        r.a_audio_message(o), r.date(m.get('date')), back))
                elif t == 'audio':
                    audios.append('<div class="ex-gal-item">%s%s</div>' % (r.a_audio(o), back))
                elif t == 'link':
                    links.append('<div class="ex-gal-item">%s%s</div>' % (r.a_link(o), back))
        sections = [('Фотографии', photos, 'ex-grid'), ('Видео', videos, ''), ('Файлы', docs, ''),
                    ('Голосовые сообщения', voices, ''), ('Музыка', audios, ''), ('Ссылки', links, '')]
        toc = ''.join('<a class="ex-chip" href="#s%d">%s <span class="ex-count">%s</span></a>' % (i, title, num(len(items)))
                      for i, (title, items, _) in enumerate(sections) if items)
        body = ''.join('<h3 class="ex-h3" id="s%d">%s <span class="ex-dim">%s</span></h3><div class="%s">%s</div>' % (
            i, title, num(len(items)), cls, ''.join(items)) for i, (title, items, cls) in enumerate(sections) if items)
        body = self.conv_bar(peer) + ('<div class="ex-toc">%s</div>' % toc if toc else '') + (
            body or '<div class="ex-empty-note">В этой переписке нет вложений.</div>')
        self.write_page('messages/%d/media.html' % peer, self.crumbs([('Сообщения', '../index-messages.html'),
                                                                      (self.peer_name(peer), 'messages0.html'), ('Медиа', None)]), body)

    def crumbs(self, items):
        p = self.r.p
        out = ['<a class="ui_crumb" href="%sindex.html">Главная</a>' % p]
        for label, href in items:
            out.append('<div class="ui_crumb_sep"></div>')
            out.append('<a class="ui_crumb" href="%s">%s</a>' % (href, esc(label)) if href else '<div class="ui_crumb">%s</div>' % esc(label))
        return ''.join(out)

    def write_page(self, rel, crumbs, body, extra_head=''):
        p = '../' * rel.count('/')
        page = PAGE.format(p=p, crumbs=crumbs, body=body, head=extra_head)
        path = os.path.join(self.dst, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(page)

    # -- activity statistics (--github-activity); enrich.js draws the charts from this data --
    def stats_data(self, convs, with_top=False):
        """Counts per day / hour / weekday in 4 buckets: [sent, received, sent to communities,
        received from communities], so pages can show sent and received apart and drop bots."""
        days = defaultdict(lambda: [0, 0, 0, 0])
        hours = [[0] * 4 for _ in range(24)]
        wdays = [[0] * 4 for _ in range(7)]
        per_peer = defaultdict(lambda: [0, 0])
        day_peer = defaultdict(dict)
        for conv in convs:
            grp = 2 if conv.peer < 0 else 0
            for mid, lts, author, *_ in conv.msgs:
                if not lts:
                    continue
                way = 0 if author == self.self_id else 1
                dt = datetime.fromtimestamp(lts, timezone.utc)
                key = dt.strftime('%Y-%m-%d')
                days[key][grp + way] += 1
                hours[dt.hour][grp + way] += 1
                wdays[dt.weekday()][grp + way] += 1
                per_peer[conv.peer][way] += 1
                if with_top:
                    day_peer[key].setdefault(conv.peer, [0, 0])[way] += 1
        data = {'days': days, 'hours': hours, 'wdays': wdays}
        if with_top:  # a few biggest conversations of each day, for the tooltips
            top = {}
            for key, peers in day_peer.items():
                keep = set(sorted(peers, key=lambda p: -peers[p][0])[:5]) | set(sorted(peers, key=lambda p: -peers[p][1])[:5])
                top[key] = [[p] + peers[p] for p in keep]
            data['top'] = top
            data['peers'] = {str(p): [self.peer_name(p), self.r.local('ava:%d' % p) or '', int(p < 0), sr[0], sr[1]]
                             for p, sr in per_peer.items()}
        return data

    @staticmethod
    def stats_block(data, communities_toggle=False):
        js = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
        toggle = ('<label class="ex-check"><input type="checkbox" id="ex-nogroups"> без сообществ</label>'
                  if communities_toggle else '')
        return ('<div class="ex-stats"><div class="ex-seg-row"><div class="ex-seg" role="group">'
                '<button type="button" data-mode="sent">Отправленные</button><button type="button" data-mode="recv">Полученные</button>'
                '<button type="button" data-mode="all">Все</button></div>%s</div>'
                '<div class="ex-tiles" id="ex-st-tiles"></div>'
                '<h3 class="ex-h3">Активность по дням</h3><div id="ex-st-heat"></div>'
                '<h3 class="ex-h3">По годам</h3><div id="ex-st-years"></div>'
                '<div class="ex-two"><div><h3 class="ex-h3">По часам</h3><div id="ex-st-hours"></div></div>'
                '<div><h3 class="ex-h3">По дням недели</h3><div id="ex-st-wdays"></div></div></div>'
                '%s<noscript><p class="ex-dim">Графики рисуются JavaScript-ом: откройте страницу в обычном браузере.</p></noscript>'
                '</div><script>window.EXSTATS=%s;</script>') % (
            toggle, '<h3 class="ex-h3">Самые большие переписки</h3><div id="ex-st-rank"></div>' if communities_toggle else '', js)

    def write_stats(self, peer):
        conv = self.src.convs[peer]
        r = self.r
        r.p = '../../'
        data = self.stats_data([conv])
        links, by_author = {}, Counter()
        for mid, lts, author, pi, _, _ in reversed(conv.msgs):  # oldest first: a day links to its first message
            if lts:
                by_author[author] += 1
                d = lday(lts)
                links.setdefault(d.strftime('%Y-%m-%d'), '%s#t%d' % (conv.pages[pi], calendar.timegm(d.timetuple())))
        data['links'] = links
        body = self.conv_bar(peer) + self.stats_block(data)
        if len(by_author) > 2:  # group chats: who wrote how much
            top = by_author.most_common(30)
            mx = top[0][1]
            body += '<h3 class="ex-h3">Кто сколько писал</h3><div class="ex-ranks">%s</div>' % ''.join(
                '<div class="ex-rank"><span class="ex-rank-who">%s<span>%s</span></span><span class="ex-rank-bar"><i style="width:%.1f%%"></i></span>'
                '<span class="ex-rank-n">%s</span></div>' % (r.ava(a, 'ex-ava ex-ava-sm'), esc(self.people.name(a) if a != self.self_id else 'Вы'),
                                                             100.0 * c / mx, num(c)) for a, c in top)
        self.write_page('messages/%d/stats.html' % peer, self.crumbs([('Сообщения', '../index-messages.html'),
                                                                      (self.peer_name(peer), 'messages0.html'), ('Статистика', None)]), body)

    # -- the conversations list --
    def write_messages_index(self):
        r = self.r
        r.p = '../'
        rows = []
        total = Counter()
        all_media = Counter()
        for peer in self.peers:
            st = self.conv_stats[peer]
            if not st['count']:
                continue
            total['msgs'] += st['count']
            total['mine'] += st['mine']
            all_media.update(st['media'])
            nmedia = sum(st['media'].get(t, 0) for t, _ in MEDIA_COUNTED)
            name = self.peer_name(peer)
            kind = peer_kind(peer)
            kind_label = {'chat': 'беседа', 'group': 'сообщество', 'user': ''}[kind]
            links = '<a class="ex-chip" href="%d/media.html">медиа</a>' % peer
            if self.a.github_activity:
                links += '<a class="ex-chip" href="%d/stats.html">статистика</a>' % peer
            rows.append(
                '<div class="ex-peer" data-name="%s" data-type="%s" data-last="%d" data-first="%d" data-count="%d" data-mine="%d" data-media="%d">'
                '%s<div class="ex-peer-main"><a class="ex-peer-name" href="%d/messages0.html">%s</a>%s'
                '<div class="ex-dim">%s (ваших %s) · %s — %s%s</div></div><div class="ex-peer-links">%s</div></div>' % (
                    esc(name.lower()), kind, st['last'], st['first'], st['count'], st['mine'], nmedia,
                    r.ava(peer, 'ex-ava ex-ava-md'), peer, esc(name),
                    ' <span class="ex-kind">%s</span>' % kind_label if kind_label else '',
                    nplural(st['count'], MSG_FORMS), num(st['mine']), short_date(st['first']), short_date(st['last']),
                    (' · ' + self.media_line(st['media'])) if st['media'] else '', links))
        head = '<div class="ex-summary">%s · %s, из них ваших %s%s</div>' % (
            nplural(len(rows), ('переписка', 'переписки', 'переписок')), nplural(total['msgs'], MSG_FORMS),
            num(total['mine']), (' · ' + self.media_line(all_media)) if all_media else '')
        controls = ('<div class="ex-controls"><input type="search" id="ex-q" placeholder="Найти переписку по имени">'
                    '<select id="ex-filter"><option value="">Все</option><option value="user">Люди</option>'
                    '<option value="chat">Беседы</option><option value="group">Сообщества</option></select>'
                    '<select id="ex-sort"><option value="last">Сначала недавние</option><option value="count">Больше всего сообщений</option>'
                    '<option value="mine">Больше всего ваших сообщений</option>'
                    '<option value="media">Больше всего медиа</option><option value="first">Самые давние</option>'
                    '<option value="name">По имени</option></select>'
                    '<a class="ex-chip" href="../search.html">Поиск по сообщениям</a>%s</div>') % (
                        '<a class="ex-chip" href="stats.html">Статистика</a>' if self.a.github_activity else '')
        rows.sort(key=lambda h: -int(re.search(r'data-last="(\d+)"', h).group(1)))
        body = head + controls + '<div id="ex-peers">%s</div>' % ''.join(rows)
        self.write_page('messages/index-messages.html', self.crumbs([('Сообщения', None)]), body)
        if self.a.github_activity:
            self.write_global_stats()

    def write_global_stats(self):
        """messages/stats.html: activity over all conversations."""
        self.r.p = '../'
        data = self.stats_data(self.src.convs.values(), with_top=True)
        self.write_page('messages/stats.html', self.crumbs([('Сообщения', 'index-messages.html'), ('Статистика', None)]),
                        self.stats_block(data, communities_toggle=True))

    # -- search --
    def write_search(self):
        """Per-conversation text files the search page loads with <script> (works from disk)."""
        folder = os.path.join(self.dst, 'search')
        os.makedirs(folder, exist_ok=True)
        self.r.p = ''
        peers, people = [], {}
        for peer in self.peers:
            conv = self.src.convs[peer]
            if not conv.msgs:
                continue
            api = self.msgs.get(peer, {})
            authors = {}
            rows = []
            for mid, lts, author, pi, _, text in conv.msgs:
                m = api.get(mid)
                if m:  # forwarded messages are not in the archive text - make them searchable too
                    extra = []

                    def fw(x, depth=0):
                        for f in x.get('fwd_messages') or []:
                            if f.get('text'):
                                extra.append('↪ ' + f['text'])
                            if depth < 5:
                                fw(f, depth + 1)
                    fw(m)
                    if extra:
                        text = (text + '\n' + '\n'.join(extra)).strip()
                if not text:
                    continue
                ai = authors.setdefault(author, len(authors))
                rows.append([mid, lts or 0, pi, ai, text])
            for a in authors:
                if a not in people:
                    people[a] = [self.people.name(a) if a != self.self_id else 'Вы', self.media.path('ava:%d' % a) or '']
            with open(os.path.join(folder, '%d.js' % peer), 'w', encoding='utf-8') as f:
                f.write('(window.EXSQ=window.EXSQ||[]).push({peer:%d,meta:%s,rows:%s});\n' % (
                    peer, json.dumps({'pages': conv.pages, 'authors': list(authors)}, ensure_ascii=False),
                    json.dumps(rows, ensure_ascii=False, separators=(',', ':'))))
            peers.append([peer, self.peer_name(peer), self.media.path('ava:%d' % peer) or '', len(rows)])
        with open(os.path.join(folder, 'index.js'), 'w', encoding='utf-8') as f:
            f.write('(window.EXSQ=window.EXSQ||[]).push({init:[%s,%s,%d]});\n' % (
                json.dumps(peers, ensure_ascii=False), json.dumps({str(k): v for k, v in people.items()}, ensure_ascii=False),
                self.self_id or 0))
        body = ('<div id="ex-search"><div class="ex-controls"><input type="search" id="ex-sq" placeholder="Что искать (все слова должны встретиться)" autofocus>'
                '<select id="ex-speer"><option value="">Во всех переписках</option></select>'
                '<select id="ex-sauthor"><option value="">Все авторы</option><option value="me">Только мои</option><option value="them">Только чужие</option></select></div>'
                '<div id="ex-sstatus" class="ex-dim">Загрузка…</div><div id="ex-sresults"></div></div>')
        self.write_page('search.html', self.crumbs([('Поиск по сообщениям', None)]), body,
                        '<script src="search/index.js"></script>')

    # -- every other page --
    def render_other_pages(self):
        done, n = set(), 0
        for rel in self.src.html_files():
            if rel in done or rel.startswith('messages/'):
                continue
            series = self.src.series(rel)
            done.update(series)
            heavy = len(series) and max(self.src.items(s) for s in series) > 100
            if heavy:
                n += self.repaginate(series)
                continue
            for i, s in enumerate(series):
                depth = s.count('/')
                self.r.p = '../' * depth
                t = self.transform(s, read_page(self.src.path(s)))
                if len(series) > 1:
                    t = self.with_pager(t, self.pager([x.rsplit('/', 1)[-1] for x in series], i))
                t = self.annotate(s, t)
                self.finish(s, t)
                n += 1
        return n

    def repaginate(self, series, size=50):
        """Lists with 1000 items per page (likes) become pages of `size` items with a proper pager."""
        items, template = [], None
        for rel in series:
            t = read_page(self.src.path(rel))
            w = t.find('<div class="wrap_page_content">')
            if w < 0:
                continue
            e = div_end(t, w)
            body = t[w:e]
            items += [body[a:b] for a, b in div_blocks(body, '<div class="item">')]
            if template is None:
                p = t.find('<div class="pagination')
                if p >= 0:
                    t = t[:p] + t[div_end(t, p):]
                    w = t.find('<div class="wrap_page_content">')
                    e = div_end(t, w)
                template = t[:w] + '<div class="wrap_page_content">%%ITEMS%%</div>' + t[e:]
        d, prefix, first = series_key(series[0])
        pages = list(chunks(items, size)) or [[]]
        names = [('%s.html' % prefix if (k == 0 and first == 1 and not re.search(r'\d\.html$', series[0])) else
                  '%s%d.html' % (prefix, k + (1 if first == 1 else 0))) for k in range(len(pages))]
        for k, chunk in enumerate(pages):
            rel = (d + '/' if d else '') + names[k]
            self.r.p = '../' * rel.count('/')
            t = template.replace('%%ITEMS%%', ''.join(chunk))
            t = self.transform(rel, t)
            t = self.with_pager(t, self.pager(names, k))
            t = self.annotate(rel, t, series[0])
            self.finish(rel, t)
        return len(pages)

    def transform(self, rel, t):
        parts = rel.split('/')
        if rel == 'index.html':
            return self.tx_index(t)
        if re.fullmatch(r'wall/wall\d+\.html', rel):
            return self.tx_wall(t)
        if parts[0] == 'wall' and len(parts) == 3 and parts[1].isdigit():
            return self.tx_wall_comments(t, '%d_%s' % (self.self_id, parts[1]))
        if parts[0] == 'comments':
            return self.tx_my_comments(t)
        if rel.startswith('photos/photo-albums/'):
            return self.tx_album(t)
        if rel == 'profile/page-info.html':
            return self.tx_page_info(t)
        if rel == 'profile/gifts.html':
            return self.tx_gifts(t)
        if parts[0] == 'profile':
            return self.tx_people_list(t)
        if rel == 'other/support.html':
            return self.tx_support(t)
        if parts[0] == 'likes' and len(parts) == 3:
            return self.tx_likes(t, parts[1])
        return t

    # -- counts --
    def count_of(self, rel):
        """How many things a page (and its pagination series / sub-pages) holds; None = not a list."""
        rel = os.path.normpath(rel).replace(os.sep, '/')
        if rel in self._counts:
            return self._counts[rel]
        self._counts[rel] = None  # cycle guard
        res = None
        if rel == 'messages/index-messages.html':
            res = sum(1 for p in self.peers if self.src.convs[p].msgs)
        elif rel in ('profile/page-info.html', 'index.html') or not self.src.exists(rel):
            res = None
        else:
            t = read_page(self.src.path(rel))
            w = t.find('<div class="wrap_page_content">')
            body = t[w:div_end(t, w)] if w >= 0 else ''
            blocks = [body[a:b] for a, b in div_blocks(body, '<div class="item">')]
            targets = []
            for b in blocks:
                hrefs = re.findall(r'href="([^"#?]+\.html)"', b)
                if len(hrefs) == 1 and 'http' not in b:
                    targets.append(hrefs[0])
            if blocks and len(targets) == len(blocks):  # a menu of sub-pages
                d = os.path.dirname(rel)
                sub = [self.count_of(os.path.join(d, h)) for h in targets]
                res = sum(x for x in sub if x)
            else:
                res = sum(self.src.items(s) for s in self.src.series(rel))
        self._counts[rel] = res
        return res

    def annotate(self, rel, t, count_rel=None):
        """Badges with item counts on links to other pages, and on the page's own title."""
        d = os.path.dirname(rel)
        own = set(self.src.series(rel))

        def link(m):
            href = m.group(2)
            if href.startswith(('http', '#')) or href.endswith('index.html') and 'index-messages' not in href:
                return m.group(0)
            target = os.path.normpath(os.path.join(d, href)).replace(os.sep, '/')
            if target in own or target.startswith('..') or (target.startswith('messages/') and target != 'messages/index-messages.html'):
                return m.group(0)
            c = self.count_of(target)
            if c is None:
                return m.group(0)
            opening = m.group(1)
            if c == 0:
                opening = re.sub(r'class="([^"]*)"', r'class="\1 ex-zero"', opening, count=1) if 'class="' in opening \
                    else opening.replace('<a ', '<a class="ex-zero" ', 1)
            return '%s%s<span class="ex-count">%s</span></a>' % (opening, m.group(3), num(c))
        t = re.sub(r'(<a\b[^>]*?href="([^"]+\.html)"[^>]*>)(.*?)</a>', link, t, flags=re.S)
        c = self.count_of(count_rel or rel)
        if c is not None:
            t = re.sub(r'(<div class="ui_crumb" >)(.*?)(</div>)', lambda m: '%s%s <span class="ex-count">%s</span>%s' % (
                m.group(1), m.group(2), num(c), m.group(3)), t, count=1)
        return t

    # -- page transforms --
    def tx_index(self, t):
        icon = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="%s"/></svg>'
        extra = ''.join(
            '<div class="item"><div class="item__main"><div class="index-row">'
            '<a class="menu__page-link" href="%s"><span class="ex-menu-icon">%s</span><div>%s</div></a></div></div></div>' % (
                href, icon % path, label)
            for href, path, label in [('search.html', ICON_SEARCH, 'Поиск по сообщениям')] +
            ([('messages/stats.html', ICON_STATS, 'Статистика переписок')] if self.a.github_activity else []) +
            [('enrich-report.html', ICON_REPORT, 'Что скачано (отчёт)')])
        return t.replace('<div class="wrap_page_content">', '<div class="wrap_page_content">' + extra, 1)

    def tx_wall(self, t):
        items = t.split('<div class="item">')
        for n in range(1, len(items)):
            it = items[n]
            m = re.search(r'class="post__link fl_l" href="https?://vk\.(?:ru|com)/wall(-?\d+_\d+)"', it)
            post = self.wall.get('posts', {}).get(m.group(1)) if m else None
            if not post:
                continue
            own = self.r.attachments(post.get('attachments'))
            hist = post.get('copy_history') or []
            rep = div_blocks(it, '<div class="repost__post">')
            rep_span = None
            if rep and hist:
                a, b = rep[0]
                inner = it[a:b]
                ch = hist[0]
                rendered = self.r.attachments(ch.get('attachments'), 1) + ''.join(self.r.post(x, 1) for x in hist[1:])
                inner = self.replace_attachments(inner, rendered) if div_blocks(inner, '<div class="attachment">') else \
                    inner[:-6] + '<div class="ex-rich">' + rendered + '</div></div>'
                owner = ch.get('owner_id') or ch.get('from_id')
                inner = inner.replace('<div class="repost__post__owner">', '<div class="repost__post__owner">' + self.r.ava(owner), 1)
                it = it[:a] + inner + it[b:]
                rep_span = (a, a + len(inner))
            outside = [(x, y) for x, y in div_blocks(it, '<div class="attachment">') if not rep_span or not (rep_span[0] <= x < rep_span[1])]
            if outside:
                x, y = outside[0][0], outside[-1][1]
                it = it[:x] + '<div class="ex-rich">' + own + '</div>' + it[y:]
            elif own:
                j = it.find("<div class='item__main'><div class='clear_fix'>")
                if j > 0:
                    it = it[:j] + "<div class='item__main'><div class=\"ex-rich\">" + own + '</div></div>' + it[j:]
            items[n] = it
        return '<div class="item">'.join(items)

    def comment_items(self, t, lookup):
        items = t.split('<div class="item">')
        for n in range(1, len(items)):
            it = items[n]
            m = re.search(r'href="https?://vk\.(?:ru|com)/wall(-?\d+)_(\d+)\?reply=(\d+)', it)
            c = lookup(m) if m else None
            if not c:
                continue
            rendered = self.r.attachments(c.get('attachments'))
            if rendered:
                it = self.replace_attachments(it, rendered)
            author = c.get('from_id')
            it = re.sub(r'(<a href="https?://vk\.(?:ru|com)/(?:id|club|public)\d+" class="mem_link" >)',
                        lambda mm: self.r.ava(author, 'ex-ava ex-ava-sm') + mm.group(1), it, count=1)
            items[n] = it
        return '<div class="item">'.join(items)

    def tx_wall_comments(self, t, post_key):
        cs = self.wall.get('comments', {}).get(post_key, {})
        return self.comment_items(t, lambda m: cs.get(m.group(3)))

    def tx_my_comments(self, t):
        return self.comment_items(t, lambda m: self.my_comments.get('%s_%s' % (m.group(1), m.group(3))))

    def tx_album(self, t):
        def one(m):
            key = m.group(2)
            local = self.r.local('photo:' + key)
            if not local:
                return m.group(0)
            return '<a class="ex-photo" href="%s" target="_blank"><img class="ex-album-img" src="%s" loading="lazy" alt="%s">' % (local, local, key)
        return re.sub(r'<a href="(https?://vk\.(?:ru|com)/photo(-?\d+_\d+))"><img src="[^"]+" alt="[^"]*">', one, t)

    def tx_page_info(self, t):
        local = self.r.local('avabig:%d' % self.self_id) or self.r.local('ava:%d' % self.self_id)
        if local:
            t = re.sub(r'<img class="fans_fan_img" src="[^"]+">',
                       '<a class="ex-photo" href="%s" target="_blank"><img class="fans_fan_img ex-self-ava" src="%s"></a>' % (local, local), t, count=1)
        return t

    def tx_gifts(self, t):
        def one(m):
            u = html.unescape(m.group(1))
            g = re.search(r'/images/gift/(\d+)/', u)
            local = self.r.local('gift:' + g.group(1)) if g else self.r.local('url:' + sha(u))
            return '<img class="ex-gift-img" src="%s"' % local if local else m.group(0)
        return self.tx_people_list(re.sub(r'<img src="(https?://[^"]+)"', one, t))

    def tx_people_list(self, t):
        """Avatars before profile links; a chip to the conversation with that person."""
        def one(m):
            i = int(m.group(3)) * (1 if m.group(2) == 'id' else -1)
            chip = ''
            st = self.conv_stats.get(i)
            if st and st['count']:
                chip = ' <a class="ex-chip ex-chip-sm" href="%smessages/%d/messages0.html" title="Переписка">💬 %s</a>' % (
                    self.r.p, i, num(st['count']))
            return self.r.ava(i, 'ex-ava ex-ava-md') + m.group(1) + m.group(4) + '</a>' + chip
        return re.sub(r'(<a href="(?:https?://vk\.(?:ru|com)/(id|club|public|event)(\d+))"(?: class="mem_link" ?)?>)(.*?)</a>', one, t, flags=re.S)

    def tx_likes(self, t, kind):
        store = self.likes.get(kind)
        if not store or kind not in LIKE_LINK:
            return t
        inner = re.compile(LIKE_LINK[kind])
        r = self.r

        def one(m):
            km = inner.search(m.group(1) + '"')
            o = store.get(like_key(km)) if km else None
            if not o:
                return m.group(0)
            if kind == 'photo':
                extra = r.photo_card(o)
            elif kind == 'video':
                th = r.local('vthumb:' + oid(o))
                extra = '<div class="ex-card ex-link">%s<div>%s<div class="ex-title">%s</div><div class="ex-dim">%s</div></div></div>' % (
                    '<img class="ex-link-img" src="%s" loading="lazy" alt="">' % th if th else '',
                    r.head(o.get('owner_id'), o.get('date')), esc(o.get('title')), fmt_dur(o.get('duration')))
            elif kind == 'wall':
                extra = r.post(o)
            elif kind == 'market':
                extra = r.a_market(o)
            else:
                extra = r.comment(o)
            return m.group(0) + '<div class="ex-like">%s</div>' % extra
        return re.sub(r'<a href="(https?://vk\.(?:ru|com)/[^"]+)">.*?</a>', one, t, flags=re.S)

    def tx_support(self, t):
        """Ticket links lead to the saved conversations; each item shows how many messages it has."""
        def one(m):
            ticket = self.support.get(m.group(1)) or {}
            if not ticket.get('replies'):
                return m.group(0)
            return '<a href="support/%s.html">%s</a> <span class="ex-count">%s</span>' % (
                m.group(1), m.group(2), num(len(ticket['replies'])))
        t = re.sub(r'<a href="https?://vk\.(?:ru|com)/support\?act=show&(?:amp;)?id=(\d+)">(.*?)</a>', one, t, flags=re.S)
        saved = self.r.p
        for tid, ticket in self.support.items():
            if ticket.get('replies'):
                self.write_ticket(tid, ticket)
        self.r.p = saved
        return t

    def write_ticket(self, tid, ticket):
        r = self.r
        r.p = '../../'

        def local(u):
            return r.local('url:' + sha(u)) if u else None

        def picture(full, thumb, title=''):
            return '<a class="ex-photo" href="%s" target="_blank" title="%s"><img src="%s" loading="lazy" alt=""></a>' % (
                local(full) or esc(full), esc(title), local(full) or local(thumb) or esc(thumb))
        rows = []
        for reply in ticket['replies']:
            ava = local(reply['avatar'])
            ava = '<img class="ex-ava" src="%s" alt="">' % ava if ava else '<span class="ex-ava ex-ava-ph">%s</span>' % esc(reply['author'][:1])
            fields = ''.join('<div class="ex-ticket-field"><span class="ex-dim">%s:</span> %s%s</div>' % (
                esc(f['label']), f['value'], '<div class="ex-gallery ex-many">%s</div>' % ''.join(
                    picture(i['href'], i['thumb']) for i in f['images']) if f['images'] else '') for f in reply['fields'])
            gallery = ''.join(picture(a['href'], a['thumb'], a['name']) for a in reply['attachments'])
            rows.append('<div class="ex-ticket-reply%s">%s<div class="ex-ticket-body"><div class="ex-head"><b>%s</b>'
                        '<span class="ex-date">%s</span></div><div class="ex-text">%s</div>%s%s</div></div>' % (
                            ' ex-mine' if reply['mine'] else '', ava, esc(reply['author']), esc(reply['date']), reply['text'],
                            fields, '<div class="ex-gallery ex-many">%s</div>' % gallery if gallery else ''))
        body = '<div class="ex-ticket-head"><div class="ex-conv-name">%s</div><div class="ex-dim">%s · %s</div></div>%s' % (
            esc(ticket['title']), esc(ticket.get('status', '')), nplural(len(ticket['replies']), MSG_FORMS), ''.join(rows))
        self.write_page('other/support/%s.html' % tid, self.crumbs([('Обращения в поддержку', '../support.html'),
                                                                     (ticket['title'], None)]), body)

    def write_report(self):
        self.r.p = ''
        have = Counter(k.split(':')[0] for k in self.media.index if self.media.path(k))
        fail = Counter(k.split(':')[0] for k, e in self.media.failed.items() if not e.startswith('skipped'))
        skip = Counter(k.split(':')[0] for k, e in self.media.failed.items() if e.startswith('skipped'))
        kinds = sorted(set(have) | set(fail) | set(skip))
        names = {'photo': 'Фотографии', 'doc': 'Документы', 'video': 'Видео', 'vthumb': 'Обложки видео',
                 'voice': 'Голосовые', 'audio': 'Музыка', 'sticker': 'Стикеры', 'gift': 'Подарки',
                 'ava': 'Аватарки', 'avabig': 'Аватарки (полный размер)', 'url': 'Прочие картинки'}
        size = 0
        for dp, dn, fn in os.walk(os.path.join(self.dst, 'media')):
            size += sum(os.path.getsize(os.path.join(dp, f)) for f in fn)
        rows = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
            names.get(k, k), num(have[k]), num(fail[k]) if fail[k] else '', num(skip[k]) if skip[k] else '') for k in kinds)
        info = [('Обновлено', datetime.now().strftime('%d.%m.%Y %H:%M')),
                ('Сообщений в архиве', num(sum(len(c.msgs) for c in self.src.convs.values()))),
                ('С вложениями (запрошено из API в этот запуск)', num(self.stats['messages requested']) if self.stats['messages requested'] else '—'),
                ('Записей на стене', num(len(self.wall.get('posts', {})))),
                ('Размер папки media', fmt_size(size))]
        fails = sorted(self.media.failed.items())
        fail_rows = ''.join('<tr><td>%s</td><td>%s</td></tr>' % (esc(k), esc(e)) for k, e in fails[:5000])
        body = ('<div class="ex-report"><table class="ex-table">%s</table>'
                '<h3 class="ex-h3">Файлы</h3><table class="ex-table"><tr><th>Тип</th><th>Скачано</th><th>Не удалось</th><th>Пропущено</th></tr>%s</table>'
                '<p class="ex-dim">Исходные ответы VK API лежат в папке <code>_data</code>. Повторный запуск скрипта докачает то, что не получилось.</p>'
                '<h3 class="ex-h3">Не скачалось <span class="ex-dim">%s</span></h3><table class="ex-table">%s</table></div>') % (
            ''.join('<tr><td>%s</td><td>%s</td></tr>' % (esc(a), b) for a, b in info), rows, num(len(fails)), fail_rows)
        self.write_page('enrich-report.html', self.crumbs([('Что скачано', None)]), body)

    # -- main --
    def run(self):
        self.connect()
        try:
            self.scan()
            self.do_messages()
            self.do_wall()
            self.do_my_comments()
            self.do_photos()
            self.do_docs()
            self.do_profile()
            self.do_support()
            self.do_likes()
            self.do_people()
            if self.api:
                log('Waiting for the remaining downloads')
                self.media.wait()
            self.render_all()
        finally:
            if getattr(self, 'media', None):
                self.media.shutdown()
                self.media.save()
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        st = self.media.stats
        if st:
            log('Downloads: %s' % ', '.join('%s: %d' % kv for kv in sorted(st.items())))
        log('Done. Open %s' % os.path.join(self.dst, 'index.html'))


# menu icons (Material Icons, Apache 2.0), drawn in VK's grey like the archive's own icons
ICON_SEARCH = ('M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57'
               'l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z')
ICON_STATS = 'M5 9.2h3V19H5V9.2zM10.6 5h2.8v14h-2.8V5zm5.6 8H19v6h-2.8v-6z'
ICON_REPORT = 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z'


PAGE = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VK</title>
  <link rel="stylesheet" type="text/css" href="{p}style.css">
  <link rel="stylesheet" type="text/css" href="{p}enrich.css">
  <script src="{p}enrich.js" defer></script>
  {head}
</head>
<body data-root="{p}">
  <div class="wrap">
    <div class="header"><div class="page_header"><a class="ex-home" href="{p}index.html" title="На главную"><div class="top_home_logo"></div></a></div></div>
    <div class="page_content page_block">
      <h2 class="page_block_h2"><div class="page_block_header clear_fix">
        <div class="page_block_header_inner _header_inner">{crumbs}</div>
      </div></h2>
      <div class="wrap_page_content ex-page">{body}</div>
    </div>
  </div>
</body>
</html>
'''


def parse_list(value, allowed, what):
    items = [x.strip().lower() for x in (value or '').split(',') if x.strip()]
    bad = [x for x in items if x not in allowed]
    if bad:
        sys.exit('Unknown %s: %s. Allowed: %s' % (what, ', '.join(bad), ', '.join(allowed)))
    return items


def main():
    for stream in (sys.stdout, sys.stderr):  # a console that cannot show some character must not crash the run
        if stream and hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    ap = argparse.ArgumentParser(
        prog='vk_archive_enrich.py', formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Дополняет официальный архив данных VK: скачивает фото, видео, голосовые, документы, '
                    'аватарки, пересланные сообщения и делает удобные страницы. Исходный архив не меняется.',
        epilog='Примеры:\n'
               '  python3 vk_archive_enrich.py Archive                         # всё, кроме лайков\n'
               '  python3 vk_archive_enrich.py Archive --likes --github-activity\n'
               '  python3 vk_archive_enrich.py Archive --only messages --no-media videos,music\n'
               '  python3 vk_archive_enrich.py Archive --browser chrome\n'
               '  python3 vk_archive_enrich.py Archive --offline              # пересобрать страницы без интернета\n')
    ap.add_argument('--version', action='version', version='vk_archive_enrich %s (ffmpeg: %s)' % (
        __version__, 'found' if FFMPEG else 'not found'))
    ap.add_argument('src', help='распакованный архив VK (папка с index.html)')
    ap.add_argument('dst', nargs='?', help='куда сложить результат (по умолчанию <архив>_enriched)')
    g = ap.add_argument_group('вход в VK')
    g.add_argument('--browser', metavar='NAME[:PROFILE]',
                   help='из какого браузера взять вход в vk.ru: firefox (по умолчанию ищется сам), %s. '
                        'Для всех кроме Firefox нужен пакет yt-dlp' % ', '.join(CHROMIUM_BROWSERS))
    g.add_argument('--cookies', metavar='FILE', help='файл cookies.txt (формат Netscape), выгруженный из браузера')
    g.add_argument('--token', help='access_token VK вместо cookies; "ask" - спросить (скрытый ввод), '
                                   'скрипт подскажет, как получить токен в консоли браузера')
    g = ap.add_argument_group('что скачивать')
    g.add_argument('--only', metavar='SECTIONS', help='только эти разделы: %s' % ', '.join(SECTIONS))
    g.add_argument('--skip', metavar='SECTIONS', help='пропустить эти разделы')
    g.add_argument('--likes', action='store_true', help='добавить раздел likes: то, что вы лайкали (по умолчанию выключен, это гигабайты)')
    g.add_argument('--no-media', metavar='TYPES', default='',
                   help='не скачивать эти типы файлов: %s' % ', '.join(MEDIA_KINDS))
    g.add_argument('--external-videos', action='store_true', help='качать и видео с YouTube и т.п. (через yt-dlp)')
    g.add_argument('--max-video-height', type=int, default=1080, help='максимальное качество видео (по умолчанию 1080)')
    g.add_argument('--max-file-mb', type=int, default=0, help='не качать файлы больше N МБ (0 = без ограничения)')
    g.add_argument('--max-video-mb', type=int, default=0, help='не качать видео больше N МБ (0 = без ограничения)')
    g.add_argument('--all-messages', action='store_true', help='запрашивать из API все сообщения, а не только с вложениями')
    g = ap.add_argument_group('страницы')
    g.add_argument('--github-activity', action='store_true', help='календарь активности и страницы статистики переписок')
    g.add_argument('--offline', action='store_true', help='без интернета: только пересобрать страницы из сохранённых данных')
    g.add_argument('--peers', metavar='IDS', help='скачивать только эти переписки (id через запятую), для проверки')
    g = ap.add_argument_group('скорость')
    g.add_argument('--workers', type=int, default=8, help='параллельных загрузок фото и файлов (8)')
    g.add_argument('--video-workers', type=int, default=4, help='параллельных загрузок видео и музыки (4)')
    g.add_argument('--segments', type=int, default=6, help='соединений на один большой файл (6)')
    if len(sys.argv) == 1:  # no arguments: a double-click or a plain run - open the window
        if FROZEN:
            ap.print_help()
            input('\nЭто консольная версия. Окно настроек открывает vk_archive_enrich.exe. Нажмите Enter...')
            return
        try:
            import vk_archive_enrich_gui
        except ImportError as e:  # no tkinter on this system
            ap.print_help()
            log('(the settings window is unavailable: %s)' % e)
            return
        vk_archive_enrich_gui.main()
        return
    args = ap.parse_args()

    sections = set(SECTIONS) - {'likes'}
    if args.likes:
        sections.add('likes')
    if args.only:
        sections = set(parse_list(args.only, SECTIONS, 'section'))
    sections -= set(parse_list(args.skip, SECTIONS, 'section'))
    args.sections = sections
    args.no_media = parse_list(args.no_media, list(MEDIA_KINDS), 'media type')
    if not FFMPEG and 'music' not in args.no_media:
        log('note: ffmpeg not found - music tracks will not be downloaded')
        args.no_media.append('music')
    try:
        Enricher(args).run()
    except KeyboardInterrupt:
        log('Interrupted - progress is saved, rerun to continue.')
        os._exit(130)  # don't wait for download threads still busy with a big file


if __name__ == '__main__':
    main()
