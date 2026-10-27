#!/usr/bin/env python3
"""
Smoke test: builds a tiny fake VK archive (same markup and cp1251 encoding as the real
export), runs vk_archive_enrich on it with --offline and checks the result. When testing the
script itself (not the exe), it also runs it against a stub VK on that archive and on an
archive that has nothing but index.html, since people export only some sections.

    python tests/smoke_test.py                       # the script from this repository
    python tests/smoke_test.py path/to/vk_archive_enrich_cli.exe
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HEAD = '''<!DOCTYPE html>
<html>
<head>
  <meta charset="windows-1251">
  <meta name="jd" content="%s">
  <title>VK</title>
  <link rel="shortcut icon" href="%sfavicon.ico">
  <link rel="stylesheet" type="text/css" href="%sstyle.css">
</head>
<body>
  <div class="wrap">
    <div class="header">
  <div class="page_header">
    <div class="top_home_logo"></div>
  </div>
</div>
    <div class="page_content page_block">
<h2 class="page_block_h2">
<div class="page_block_header clear_fix">
  <div class="page_block_header_inner _header_inner" data-testid="header">%s</div>
</div>
</h2>
      <div class="wrap_page_content">%s</div>
    </div>
  </div>
</body>
</html>'''
JD = base64.urlsafe_b64encode(json.dumps({'user_id': 1, 'time_current': 1700000000}).encode()).decode().rstrip('=')
MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']


def page(depth, crumbs, body):
    p = '../' * depth
    return HEAD % (JD, p, p, crumbs, body)


def message(mid, author, day, month, hour, text, photo=None):
    who = '<a href="https://vk.ru/id%d">Тест Тестов</a>, ' % author if author != 1 else 'Вы, '
    kludges = ''
    if photo:
        kludges = ('<div class="attachment">\n  <div class="attachment__description">Фотография</div>\n'
                   "  <a class='attachment__link' href='%s'>%s</a>\n</div>" % (photo, photo))
    return ('<div class="item">\n  <div class=\'item__main\'><div class="message" data-id="%d">\n'
            '  <div class="message__header">%s%d %s 2020 в %d:00:00</div>\n'
            '  <div>%s<div class="kludges">%s</div></div>\n</div></div>\n</div>') % (
        mid, who, day, MONTHS[month - 1], hour, text, kludges)


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(text.encode('cp1251'))


def make_archive(root):
    menu = ''.join('<div class="item">\n  <div class=\'item__main\'><div class="index-row">\n'
                   '  <a class="menu__page-link" href="%s">\n    <span class="archive__page-icon"></span>\n'
                   '    <div>%s</div>\n  </a>\n</div></div>\n</div>' % x
                   for x in [('profile/friends0.html', 'Друзья'), ('messages/index-messages.html', 'Сообщения'),
                             ('profile/subscriptions0.html', 'Подписки')])
    write(root, 'index.html', page(0, 'Профиль', menu).replace('<div class="page_content page_block">',
                                                               '<div class="page_content clear_fix"><div class="menu">', 1))
    write(root, 'style.css', 'body{font-family:sans-serif}')
    write(root, 'profile/friends0.html', page(1, 'Друзья', '<div class="item">\n  <div class=\'item__main\'>'
                                                          '<a href="https://vk.ru/id2">Тест Тестов</a></div>\n</div>'
                                                          '<div class="item">\n  <div class=\'item__main\'>'
                                                          '<a href="https://vk.ru/id3">DELETED</a></div>\n</div>'))
    write(root, 'profile/subscriptions0.html', page(1, 'Подписки', '<div class="nothing-found-stub">\n'
                                                                  '  <div class="nothing-found-stub__text">Пусто</div>\n</div>'))
    write(root, 'messages/index-messages.html', page(1, 'Сообщения', '<div class="item">\n  <div class=\'item__main\'>'
                                                                    '<div class="message-peer">\n  <div class="message-peer--id">\n'
                                                                    '    <a href="2/messages0.html">Тест Тестов</a>\n  </div>\n</div></div>\n</div>'
                                                                    '<div class="item">\n  <div class=\'item__main\'>'
                                                                    '<div class="message-peer">\n  <div class="message-peer--id">\n'
                                                                    '    <a href="2000000001/messages0.html">Беседа</a>\n  </div>\n</div></div>\n</div>'))
    write(root, 'wall/wall0.html', page(1, 'Стена', ''.join(
        '<div class="item">\n  <div class=\'item__main\'><div class="post">\n'
        '  <a class="post__link fl_l" href="https://vk.ru/wall1_%d">Запись</a>\n'
        '  <div class="post__text">%s запись</div>\n</div></div>\n</div>' % x for x in [(5, 'Первая'), (4, 'Вторая'), (3, 'Третья')])))
    write(root, 'photos/photo-albums/-6.html', page(2, 'Фотографии со страницы', ''.join(  # an album: a grid, kept whole
        '<div class="item">\n  <div class=\'item__main\'><a href="https://vk.ru/photo1_%d"><img src="https://sun9-1.userapi.com/%d.jpg" '
        'alt="1_%d"></a></div>\n</div>' % (i, i, i) for i in range(1, 121))))
    pages = {'messages0.html': [message(4, 2, 3, 2, 12, 'Пока!'), message(3, 1, 2, 2, 11, 'Смотри', 'https://sun9-1.userapi.com/x.jpg')],
             'messages50.html': [message(2, 2, 1, 1, 10, 'Привет, как дела?'), message(1, 1, 1, 1, 9, 'Привет!')]}
    pagination = ('<div class="pagination clear_fix">\n  <div class="fl_r">\n'
                  '    <a class="pg_lnk_sel fl_l" href="messages0.html"><div class="pg_in">1</div></a>'
                  '<a class="pg_lnk fl_l" href="messages50.html"><div class="pg_in">2</div></a>\n  </div>\n</div>')
    for name, msgs in pages.items():
        write(root, 'messages/2/' + name, page(2, 'Тест Тестов', ''.join(msgs) + pagination))
    write(root, 'messages/2000000001/messages0.html', page(2, 'Беседа', message(11, 2, 5, 3, 12, 'Мем', 'https://sun9-1.userapi.com/y.jpg')
                                                           + message(10, 1, 5, 3, 11, 'Привет, беседа')))


def make_bare_archive(root):
    """Only the main page: an export where none of the sections was selected."""
    write(root, 'index.html', page(0, 'Профиль', '<div class="item">\n  <div class=\'item__main\'><div class="index-row">\n'
                                                 '  <a class="menu__page-link" href="comments/comments.html"><div>Комментарии</div></a>\n'
                                                 '</div></div>\n</div>'))


# The script with VK replaced by a stub that answers everything but knows nothing: every section the
# archive has goes through the online code, without network or a login.
STUB_VK = r'''
import os, sys
script = sys.argv.pop(1)
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
import vk_archive_enrich as m

ARCHIVE = m.Archive(sys.argv[1])
ME = ARCHIVE.self_id or 1
LTS = {mid: lts for p in ARCHIVE.peers() for mid, lts, *_ in ARCHIVE.scan_peer(p).msgs}


class Response:
    text = ''


class Session:
    def get(self, *args, **kwargs):
        return Response()


class StubVK:
    def __init__(self, cookies, token=None, reload=None):
        self.http = Session()

    def refresh(self, patient=True):
        pass

    def call(self, method, **params):
        if method == 'users.get':
            ids = params['user_ids'].split(',') if 'user_ids' in params else [ME]
            return [{'id': int(i), 'first_name': 'Тест', 'last_name': 'Тестов'} for i in ids]
        if method in ('photos.getById', 'docs.getById'):
            return []
        return {'items': [], 'count': 0}

    def execute(self, calls):  # every message comes with a photo, owner 7 and id = the message id
        return [{'items': [{'id': int(i), 'date': LTS.get(int(i), 0) - 3 * 3600, 'from_id': ME, 'attachments': [
            {'type': 'photo', 'photo': {'id': int(i), 'owner_id': 7, 'sizes': [
                {'url': 'https://example.invalid/%s.jpg' % i, 'width': 1, 'height': 1}]}}]}
            for i in p['message_ids'].split(',')]} if method == 'messages.getById' else None for method, p in calls]


def broken(*args, **kwargs):
    raise RuntimeError('broken on purpose by the test')


def download(self, rel, url, method, limit, keys):  # no network: an empty file stands in for each download
    os.makedirs(os.path.dirname(os.path.join(self.dst, rel)), exist_ok=True)
    open(os.path.join(self.dst, rel), 'wb').close()
    self._record(keys, rel, None)


m.VKApi = StubVK
m.Media._one = download
m.load_cookies = lambda browser, path: ([('.vk.ru', 'remixsid', 'test', '/')], 'the test')
for name in filter(None, os.environ.get('VKAE_TEST_BREAK', '').split(',')):  # parts that must fail without stopping the run
    setattr(m.Enricher, name, broken)
m.main()
'''
NO_MEDIA = 'photos,videos,covers,voice,music,docs,stickers,gifts,avatars'

# The VK client's patience, with the clock and the network replaced: the token refresh fails until the
# cookies are read again, the connection drops once, VK asks for a captcha once.
RESILIENCE = r'''
import os, sys, time
script = sys.argv.pop(1)
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
import requests
import vk_archive_enrich as m

time.sleep = lambda s: None
ups = iter([False, False, True])  # the line is down for a while, then back
m.Net.up = staticmethod(lambda: next(ups, True))


class Reply:
    def __init__(self, j):
        self.j = j

    def json(self):
        return self.j


class Http:
    def __init__(self):
        self.cookies = requests.cookies.RequestsCookieJar()
        self.api = 0

    def post(self, url, data=None, **kw):
        if 'web_token' in url:
            if self.cookies.get('remixsid') != 'new':
                return Reply({'type': 'error'})
            return Reply({'type': 'okay', 'data': {'access_token': 'T', 'expires': time.time() + 3600, 'user_id': 1}})
        self.api += 1
        if self.api == 1:
            raise requests.ConnectionError('the line dropped')
        if self.api == 2:
            return Reply({'error': {'error_code': 14, 'error_msg': 'Captcha needed'}})
        return Reply({'response': [{'id': 1}]})


reloads = []
api = m.VKApi([('.vk.ru', 'remixsid', 'old', '/')], reload=lambda: reloads.append(1) or [('.vk.ru', 'remixsid', 'new', '/')])
api.http = Http()
api.use_cookies([('.vk.ru', 'remixsid', 'old', '/')])
assert api.call('users.get') == [{'id': 1}]
assert reloads and api.token == 'T'
api.reload = None
api.expires = 0
api.use_cookies([('.vk.ru', 'remixsid', 'old', '/')])
try:
    api.call('users.get')
    sys.exit('refresh did not give up')
except SystemExit as e:
    assert e.code != 2 and 'session may have expired' in str(e.code), e.code
print('RESILIENCE OK')
'''


def run(cmd, *args, **env):
    env = dict(os.environ, PYTHONIOENCODING='utf-8', **env)
    p = subprocess.run(cmd + list(args), capture_output=True, env=env, timeout=300)
    out = p.stdout.decode('utf-8', 'replace') + p.stderr.decode('utf-8', 'replace')
    print(out)
    return p.returncode, out


def check(cond, what):
    print(('ok    ' if cond else 'FAIL  ') + what)
    return cond


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not show every character
        stream.reconfigure(errors='replace')
    if sys.argv[1:2] == ['--make-archive']:  # just write the fake archive (used to test the window)
        make_archive(sys.argv[2])
        return 0
    cmd = sys.argv[1:] or [sys.executable, os.path.join(HERE, '..', 'vk_archive_enrich.py')]
    script = cmd[-1] if cmd[-1].endswith('.py') else None
    tmp = tempfile.mkdtemp(prefix='vkae_smoke_')
    try:
        src, dst = os.path.join(tmp, 'Архив'), os.path.join(tmp, 'Архив_enriched')
        make_archive(src)
        code, out = run(cmd, src, dst, '--offline', '--github-activity')
        read = lambda rel: open(os.path.join(dst, rel), encoding='utf-8').read() if os.path.exists(os.path.join(dst, rel)) else ''
        ok = [check(code == 0, 'exit code 0')]
        index, conv, lst = read('index.html'), read('messages/2/messages0.html'), read('messages/index-messages.html')
        ok.append(check('<meta charset="utf-8">' in index and 'enrich.js' in index, 'pages converted to utf-8 with our css/js'))
        ok.append(check(re.search(r'index-messages\.html.*?ex-count">2<', index, re.S) and 'ex-zero' in index,
                        'menu shows counts, empty section dimmed'))
        ok.append(check('data-t="' in conv and 'class="ex-day"' in conv and 'ex-pager' in conv, 'messages: dates, day separators, pager'))
        ok.append(check('data-mine="2"' in lst and 'data-count="4"' in lst, 'conversation list with message counts'))
        ok.append(check('window.EXNAV' in read('messages/2/nav.js'), 'navigation data'))
        ok.append(check('Привет, как дела?' in read('search/2.js'), 'search data'))
        ok.append(check('EXSTATS' in read('messages/2/stats.html') and 'EXSTATS' in read('messages/stats.html'), 'statistics pages'))
        ok.append(check(os.path.exists(os.path.join(dst, 'enrich.css')), 'assets copied'))
        ok.append(check('DELETED (id3)</a>' in read('profile/friends0.html'), 'deleted accounts shown with their id'))
        ok.append(check(read('photos/photo-albums/-6.html').count('<div class="item">') == 120, 'an album of 120 photos on one page'))

        def conv_pages():
            return sorted(f for f in os.listdir(os.path.join(dst, 'messages', '2')) if f.startswith('messages'))
        code, out = run(cmd, src, dst, '--offline', '--messages-per-page', '1', '--items-per-page', '2', '--search-results', '7')
        pages = ['messages%d.html' % i for i in range(4)]
        ok.append(check(code == 0 and conv_pages() == pages and all(read('messages/2/' + f).count('class="message"') == 1 for f in pages)
                        and '"files": %s' % json.dumps(pages) in read('messages/2/nav.js') and '"pages": %s' % json.dumps(pages) in read('search/2.js'),
                        '--messages-per-page 1: a page per message, the archive\'s pages are gone'))
        ok.append(check(read('wall/wall0.html').count('<div class="item">') == 2 and 'Третья запись' in read('wall/wall1.html')
                        and 'href="wall1.html"' in read('wall/wall0.html'), '--items-per-page 2: the wall in pages of 2'))
        ok.append(check(read('photos/photo-albums/-6.html').count('<div class="item">') == 120
                        and not os.path.exists(os.path.join(dst, 'photos/photo-albums/-6_2.html')), '--items-per-page 2: albums stay whole'))
        ok.append(check('data-step="7"' in read('search.html'), '--search-results 7'))
        code, out = run(cmd, src, dst, '--offline', '--messages-per-page', '0', '--items-per-page', '0')
        ok.append(check(code == 0 and conv_pages() == ['messages0.html'] and read('messages/2/messages0.html').count('class="message"') == 4
                        and read('wall/wall0.html').count('<div class="item">') == 3 and not read('wall/wall1.html'),
                        'page size 0: everything on one page'))
        code, out = run(cmd, src, dst, '--offline')
        ok.append(check(code == 0 and conv_pages() == ['messages0.html', 'messages50.html'] and 'Привет!' in read('messages/2/messages50.html'),
                        'no page size: the archive\'s pages again'))
        code, out = run(cmd, src, dst, '--offline', '--messages-per-page', '-5')
        ok.append(check(code == 2, 'a negative page size is refused'))
        code, out = run(cmd, os.path.join(tmp, 'no such folder'), '--offline')
        ok.append(check(code == 2, 'not an archive: exit code 2 (the window does not restart)'))
        if script:
            stub = [sys.executable, '-c', STUB_VK, script]
            dst = os.path.join(tmp, 'stub_enriched')
            code, out = run(stub, src, dst, '--likes', '--github-activity', '--no-media', NO_MEDIA)
            ok.append(check(code == 0 and 'Traceback' not in out, 'online run (stub VK) on an archive without comments, photos, likes'))
            ok.append(check('Привет, как дела?' in read('messages/2/messages50.html') and 'enrich.js' in read('wall/wall0.html'),
                            'online run (stub VK) rendered the pages'))
            dst = os.path.join(tmp, 'chats_enriched')
            photos_only = NO_MEDIA.replace('photos,', '')
            index = lambda: json.load(open(os.path.join(dst, '_data', 'media_index.json'), encoding='utf-8'))
            code, out = run(stub, src, dst, '--no-media', photos_only, '--no-chat-media', 'all')
            ok.append(check(code == 0 and 'photo:7_3' in index() and 'photo:7_11' not in index(),
                            '--no-chat-media all: photos from the personal conversation only'))
            code, out = run(stub, src, dst, '--no-media', photos_only)
            ok.append(check(code == 0 and 'photo:7_11' in index() and 'saved by the last run' in out,
                            'a rerun soon after reuses the fetched messages and gets the group chat photos'))
            code, out = run([sys.executable, '-c', RESILIENCE, script])
            ok.append(check(code == 0 and 'RESILIENCE OK' in out and 'Got a new VK token' in out and 'Connection is back' in out
                            and 'captcha' in out, 'session lost, connection dropped, captcha: the VK client waits and goes on'))
            dst = os.path.join(tmp, 'broken_enriched')
            code, out = run(stub, src, dst, '--no-media', NO_MEDIA, VKAE_TEST_BREAK='do_wall,tx_wall')
            ok.append(check(code == 3 and 'ERROR in wall' in out and 'ERROR in page wall/wall0.html' in out
                            and 'Первая запись' in read('wall/wall0.html') and 'Привет!' in read('messages/2/messages50.html')
                            and 'broken on purpose' in read('enrich-report.html'),
                            'a part that fails is reported and skipped, the rest is still built'))
            bare = os.path.join(tmp, 'bare')
            make_bare_archive(bare)
            for mode in (['--offline'], ['--likes', '--no-media', NO_MEDIA]):
                code, out = run(stub if mode[0] != '--offline' else cmd, bare, bare + '_enriched', '--github-activity', *mode)
                built = all(os.path.exists(os.path.join(bare + '_enriched', f))
                            for f in ('index.html', 'search.html', 'enrich-report.html', 'messages/index-messages.html'))
                ok.append(check(code == 0 and 'Traceback' not in out and built,
                                'archive with only index.html (%s)' % ('offline' if mode[0] == '--offline' else 'stub VK')))
        print('\nSMOKE TEST %s' % ('PASSED' if all(ok) else 'FAILED'))
        return 0 if all(ok) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
