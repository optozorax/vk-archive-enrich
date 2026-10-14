#!/usr/bin/env python3
"""
Smoke test: builds a tiny fake VK archive (same markup and cp1251 encoding as the real
export), runs vk_archive_enrich on it with --offline and checks the result.

    python tests/smoke_test.py                       # the script from this repository
    python tests/smoke_test.py path/to/vk_archive_enrich_cli.exe
"""
import base64
import json
import os
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
                                                          '<a href="https://vk.ru/id2">Тест Тестов</a></div>\n</div>'))
    write(root, 'profile/subscriptions0.html', page(1, 'Подписки', '<div class="nothing-found-stub">\n'
                                                                  '  <div class="nothing-found-stub__text">Пусто</div>\n</div>'))
    write(root, 'messages/index-messages.html', page(1, 'Сообщения', '<div class="item">\n  <div class=\'item__main\'>'
                                                                    '<div class="message-peer">\n  <div class="message-peer--id">\n'
                                                                    '    <a href="2/messages0.html">Тест Тестов</a>\n  </div>\n</div></div>\n</div>'))
    pages = {'messages0.html': [message(4, 2, 3, 2, 12, 'Пока!'), message(3, 1, 2, 2, 11, 'Смотри', 'https://sun9-1.userapi.com/x.jpg')],
             'messages50.html': [message(2, 2, 1, 1, 10, 'Привет, как дела?'), message(1, 1, 1, 1, 9, 'Привет!')]}
    pagination = ('<div class="pagination clear_fix">\n  <div class="fl_r">\n'
                  '    <a class="pg_lnk_sel fl_l" href="messages0.html"><div class="pg_in">1</div></a>'
                  '<a class="pg_lnk fl_l" href="messages50.html"><div class="pg_in">2</div></a>\n  </div>\n</div>')
    for name, msgs in pages.items():
        write(root, 'messages/2/' + name, page(2, 'Тест Тестов', ''.join(msgs) + pagination))


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
    tmp = tempfile.mkdtemp(prefix='vkae_smoke_')
    try:
        src, dst = os.path.join(tmp, 'Архив'), os.path.join(tmp, 'Архив_enriched')
        make_archive(src)
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        p = subprocess.run(cmd + [src, dst, '--offline', '--github-activity'], capture_output=True, env=env, timeout=300)
        print(p.stdout.decode('utf-8', 'replace'), p.stderr.decode('utf-8', 'replace'))
        read = lambda rel: open(os.path.join(dst, rel), encoding='utf-8').read() if os.path.exists(os.path.join(dst, rel)) else ''
        ok = [check(p.returncode == 0, 'exit code 0')]
        index, conv, lst = read('index.html'), read('messages/2/messages0.html'), read('messages/index-messages.html')
        ok.append(check('<meta charset="utf-8">' in index and 'enrich.js' in index, 'pages converted to utf-8 with our css/js'))
        ok.append(check('ex-count">1<' in index and 'ex-zero' in index, 'menu shows counts, empty section dimmed'))
        ok.append(check('data-t="' in conv and 'class="ex-day"' in conv and 'ex-pager' in conv, 'messages: dates, day separators, pager'))
        ok.append(check('data-mine="2"' in lst and 'data-count="4"' in lst, 'conversation list with message counts'))
        ok.append(check('window.EXNAV' in read('messages/2/nav.js'), 'navigation data'))
        ok.append(check('Привет, как дела?' in read('search/2.js'), 'search data'))
        ok.append(check('EXSTATS' in read('messages/2/stats.html') and 'EXSTATS' in read('messages/stats.html'), 'statistics pages'))
        ok.append(check(os.path.exists(os.path.join(dst, 'enrich.css')), 'assets copied'))
        print('\nSMOKE TEST %s' % ('PASSED' if all(ok) else 'FAILED'))
        return 0 if all(ok) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
