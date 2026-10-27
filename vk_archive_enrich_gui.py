#!/usr/bin/env python3
"""
Settings window for vk_archive_enrich: choose the archive and what to download, press
"Начать" and watch the log. The work itself runs in a separate process (the command line
version), so "Стоп" can always end it; an interrupted run continues where it stopped.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import vk_archive_enrich as core

APP = 'VK Archive Enrich'
REPO = 'https://github.com/optozorax/vk-archive-enrich'
SECTIONS = [('messages', 'Сообщения'), ('wall', 'Стена'), ('comments', 'Ваши комментарии'),
            ('albums', 'Фотоальбомы'), ('docs', 'Документы'), ('profile', 'Подарки и истории'),
            ('support', 'Обращения в поддержку'), ('likes', 'Лайки (гигабайты)')]
MEDIA = [('photos', 'Фото'), ('videos', 'Видео'), ('covers', 'Обложки видео'), ('voice', 'Голосовые'),
         ('music', 'Музыка'), ('docs', 'Документы'), ('stickers', 'Стикеры'), ('gifts', 'Подарки'),
         ('avatars', 'Аватарки')]
QUALITY = ['360', '480', '720', '1080', '1440', '2160']
CHAT_MEDIA = [('все отмеченные', ''), ('без видео', 'videos'), ('никакие', 'all')]  # files from group chats and communities
RESTART_DELAYS = [60, 300, 600, 1200]  # seconds before a restart, after 1, 2, 3, 4 failures in a row soon after starting
QUICK_FAIL = 300  # a run that stops sooner than this counts as such a failure
AS_ARCHIVE, ALL_ON_ONE = 'как в архиве', 'все на одной'
PER_PAGE = [AS_ARCHIVE, '20', '50', '100', '200', '500', '1000', '5000', ALL_ON_ONE]
SEARCH_RESULTS = ['50', '100', '200', '500', '1000', 'все сразу']
MAX_LOG_LINES = 5000


def settings_path():
    base = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'vk_archive_enrich', 'settings.json')


def worker_command():
    """The command line version: the _cli.exe next to us in the Windows build, else this Python."""
    if core.FROZEN:
        return [os.path.join(os.path.dirname(sys.executable), 'vk_archive_enrich_cli.exe')]
    return [sys.executable, '-u', os.path.abspath(core.__file__)]


def page_size(text):
    """A page size field as a command line value: None for "as in the archive", '0' for "all", False if it is no number."""
    text = text.strip().lower().replace(' ', '')
    if not text or text == AS_ARCHIVE.replace(' ', ''):
        return None
    if text.startswith('все'):
        return '0'
    return text if text.isdigit() else False


def open_path(path):
    if os.name == 'nt':
        os.startfile(path)  # noqa: the Windows way to open a file in its default program
    else:
        webbrowser.open('file://' + os.path.abspath(path))


class TokenDialog(tk.Toplevel):
    """Explains how to get a token in the browser console and reads it (hidden)."""

    def __init__(self, parent, expired):
        super().__init__(parent)
        self.title('Токен VK')
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        f = ttk.Frame(self, padding=14)
        f.pack(fill='both', expand=True)
        intro = ('Токен истёк: они живут около часа. Получите новый так же, как в первый раз.\n\n' if expired else '')
        ttk.Label(f, wraplength=520, justify='left', text=intro +
                  '1. Откройте vk.ru в браузере (вы должны быть залогинены).\n'
                  '2. Нажмите F12 и откройте вкладку Console (Консоль).\n'
                  '3. Вставьте строку ниже и нажмите Enter, затем скопируйте токен из появившегося окна.').pack(anchor='w')
        row = ttk.Frame(f)
        row.pack(fill='x', pady=8)
        snippet = ttk.Entry(row, width=64)
        snippet.insert(0, core.TOKEN_SNIPPET)
        snippet.configure(state='readonly')
        snippet.pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Скопировать', command=lambda: (self.clipboard_clear(), self.clipboard_append(core.TOKEN_SNIPPET))).pack(side='left', padx=(6, 0))
        ttk.Label(f, text='Токен:').pack(anchor='w', pady=(6, 2))
        self.entry = ttk.Entry(f, show='•', width=64)
        self.entry.pack(fill='x')
        self.entry.focus_set()
        buttons = ttk.Frame(f)
        buttons.pack(fill='x', pady=(12, 0))
        ttk.Button(buttons, text='Отмена', command=self.destroy).pack(side='right')
        ttk.Button(buttons, text='OK', command=self.ok).pack(side='right', padx=6)
        self.bind('<Return>', lambda e: self.ok())
        self.bind('<Escape>', lambda e: self.destroy())
        self.grab_set()

    def ok(self):
        self.result = self.entry.get().strip()
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.lines = queue.Queue()
        self.dst_auto = ''
        root.title('%s %s' % (APP, core.__version__))
        root.minsize(720, 520)
        try:
            self.icon = tk.PhotoImage(file=os.path.join(core.HERE, 'assets', 'icon.png'))
            root.iconphoto(True, self.icon)
        except tk.TclError:
            pass
        self.src = tk.StringVar()
        self.dst = tk.StringVar()
        self.login = tk.StringVar(value='auto')
        self.cookies = tk.StringVar()
        self.token = tk.StringVar()
        self.sections = {k: tk.BooleanVar(value=(k != 'likes')) for k, _ in SECTIONS}
        self.media = {k: tk.BooleanVar(value=True) for k, _ in MEDIA}
        self.chat_media = tk.StringVar(value=CHAT_MEDIA[0][0])
        self.autorestart = tk.BooleanVar(value=True)
        self.restart_job = None
        self.stopping = False
        self.started_at = 0.0
        self.quick_fails = self.restarts = 0
        self.activity = tk.BooleanVar(value=True)
        self.quality = tk.StringVar(value='1080')
        self.offline = tk.BooleanVar(value=False)
        self.msgs_per_page = tk.StringVar(value=AS_ARCHIVE)
        self.items_per_page = tk.StringVar(value=AS_ARCHIVE)
        self.search_results = tk.StringVar(value='100')
        self.status = tk.StringVar(value='Выберите папку с архивом VK и нажмите «Начать».')
        self.load_settings()
        self.build()
        self.src.trace_add('write', self.src_changed)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(100, self.poll)

    # -- layout --
    def build(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)

        box = ttk.LabelFrame(outer, text='Архив', padding=10)
        box.grid(row=0, column=0, sticky='ew')
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text='Папка архива VK:').grid(row=0, column=0, sticky='w')
        ttk.Entry(box, textvariable=self.src).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(box, text='Выбрать…', command=self.pick_src).grid(row=0, column=2)
        ttk.Label(box, text='Сохранить в:').grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Entry(box, textvariable=self.dst).grid(row=1, column=1, sticky='ew', padx=6, pady=(6, 0))
        ttk.Button(box, text='Выбрать…', command=self.pick_dst).grid(row=1, column=2, pady=(6, 0))
        ttk.Label(box, foreground='#6d7885', text='Папка распакованного архива — та, где лежит index.html. '
                  'Исходный архив не меняется.').grid(row=2, column=0, columnspan=3, sticky='w', pady=(4, 0))

        box = ttk.LabelFrame(outer, text='Вход во ВКонтакте', padding=10)
        box.grid(row=1, column=0, sticky='ew', pady=(10, 0))
        box.columnconfigure(1, weight=1)
        ttk.Radiobutton(box, text='Взять из браузера (Firefox — автоматически)', value='auto', variable=self.login).grid(
            row=0, column=0, columnspan=3, sticky='w')
        ttk.Radiobutton(box, text='Файл cookies.txt:', value='cookies', variable=self.login).grid(row=1, column=0, sticky='w', pady=(4, 0))
        ttk.Entry(box, textvariable=self.cookies).grid(row=1, column=1, sticky='ew', padx=6, pady=(4, 0))
        ttk.Button(box, text='Выбрать…', command=self.pick_cookies).grid(row=1, column=2, pady=(4, 0))
        ttk.Radiobutton(box, text='Токен:', value='token', variable=self.login).grid(row=2, column=0, sticky='w', pady=(4, 0))
        ttk.Entry(box, textvariable=self.token, show='•').grid(row=2, column=1, sticky='ew', padx=6, pady=(4, 0))
        ttk.Button(box, text='Как получить?', command=self.token_help).grid(row=2, column=2, pady=(4, 0))
        ttk.Label(box, foreground='#6d7885', text='Chrome и Edge не дают читать свои cookies: для них cookies.txt '
                  '(расширение «Get cookies.txt LOCALLY») или токен.').grid(row=3, column=0, columnspan=3, sticky='w', pady=(4, 0))

        row = ttk.Frame(outer)
        row.grid(row=2, column=0, sticky='ew', pady=(10, 0))
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=1)
        box = ttk.LabelFrame(row, text='Что скачать', padding=10)
        box.grid(row=0, column=0, sticky='nsew', padx=(0, 5))
        for i, (k, label) in enumerate(SECTIONS):
            ttk.Checkbutton(box, text=label, variable=self.sections[k]).grid(row=i // 2, column=i % 2, sticky='w', padx=(0, 12))
        box = ttk.LabelFrame(row, text='Какие файлы', padding=10)
        box.grid(row=0, column=1, sticky='nsew', padx=(5, 0))
        for i, (k, label) in enumerate(MEDIA):
            ttk.Checkbutton(box, text=label, variable=self.media[k]).grid(row=i // 3, column=i % 3, sticky='w', padx=(0, 12))
        line = ttk.Frame(box)
        line.grid(row=3, column=0, columnspan=3, sticky='w', pady=(6, 0))
        ttk.Label(line, text='Из бесед и сообществ:').pack(side='left')
        ttk.Combobox(line, textvariable=self.chat_media, values=[label for label, _ in CHAT_MEDIA], width=15,
                     state='readonly').pack(side='left', padx=(6, 0))

        box = ttk.LabelFrame(outer, text='Дополнительно', padding=10)
        box.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        ttk.Checkbutton(box, text='Статистика и календари активности', variable=self.activity).grid(row=0, column=0, sticky='w')
        ttk.Label(box, text='Видео не выше:').grid(row=0, column=1, sticky='e', padx=(24, 4))
        ttk.Combobox(box, textvariable=self.quality, values=QUALITY, width=6, state='readonly').grid(row=0, column=2, sticky='w')
        ttk.Checkbutton(box, text='Только пересобрать страницы', variable=self.offline).grid(row=0, column=3, sticky='w', padx=(24, 0))
        ttk.Checkbutton(box, text='Запускать заново, если работа прервётся (пропал вход во ВК или интернет)',
                        variable=self.autorestart).grid(row=1, column=0, columnspan=4, sticky='w', pady=(4, 0))

        box = ttk.LabelFrame(outer, text='Страницы', padding=10)
        box.grid(row=4, column=0, sticky='ew', pady=(10, 0))
        for col, (label, var, values, width) in enumerate([
                ('Сообщений на странице:', self.msgs_per_page, PER_PAGE, 13),
                ('Записей в списках:', self.items_per_page, PER_PAGE, 13),
                ('Результатов поиска сразу:', self.search_results, SEARCH_RESULTS, 9)]):
            ttk.Label(box, text=label).grid(row=0, column=2 * col, sticky='w', padx=(0 if col == 0 else 18, 4))
            ttk.Combobox(box, textvariable=var, values=values, width=width).grid(row=0, column=2 * col + 1, sticky='w')
        hint = ttk.Label(box, foreground='#6d7885', justify='left', text='Можно вписать своё число. Списки: стена, комментарии, '
                         'лайки, друзья. Для уже готового архива хватит «Только пересобрать страницы».')
        hint.grid(row=1, column=0, columnspan=6, sticky='ew', pady=(4, 0))
        hint.bind('<Configure>', lambda e: hint.configure(wraplength=e.width))

        bar = ttk.Frame(outer)
        bar.grid(row=5, column=0, sticky='ew', pady=(12, 0))
        self.btn_start = ttk.Button(bar, text='Начать', command=self.start)
        self.btn_start.pack(side='left')
        self.btn_stop = ttk.Button(bar, text='Стоп', command=self.stop, state='disabled')
        self.btn_stop.pack(side='left', padx=6)
        self.btn_open = ttk.Button(bar, text='Открыть результат', width=19, command=self.open_result)
        self.btn_open.pack(side='left')
        ttk.Button(bar, text='Папка', command=self.open_folder).pack(side='left', padx=6)
        ttk.Button(bar, text='Справка', command=lambda: webbrowser.open(REPO + '#readme')).pack(side='right')

        self.progress = ttk.Progressbar(outer, mode='determinate', maximum=100)
        self.progress.grid(row=6, column=0, sticky='ew', pady=(10, 2))
        ttk.Label(outer, textvariable=self.status).grid(row=7, column=0, sticky='w')

        mono = tkfont.Font(family='Consolas' if os.name == 'nt' else 'monospace', size=9)
        self.log = ScrolledText(outer, height=7, font=mono, wrap='word', state='disabled', background='#fbfbfc')
        self.log.grid(row=8, column=0, sticky='nsew', pady=(8, 0))
        outer.rowconfigure(8, weight=1)

    # -- pickers --
    def pick_src(self):
        d = filedialog.askdirectory(title='Папка с распакованным архивом VK (там лежит index.html)')
        if d:
            self.src.set(os.path.normpath(d))

    def pick_dst(self):
        d = filedialog.askdirectory(title='Куда сохранить дополненный архив')
        if d:
            self.dst.set(os.path.normpath(d))

    def pick_cookies(self):
        f = filedialog.askopenfilename(title='cookies.txt', filetypes=[('cookies.txt', '*.txt'), ('Все файлы', '*.*')])
        if f:
            self.cookies.set(os.path.normpath(f))
            self.login.set('cookies')

    def src_changed(self, *_):
        src = self.src.get().strip().rstrip('\\/')
        if src and (not self.dst.get().strip() or self.dst.get() == self.dst_auto):
            self.dst_auto = src + '_enriched'
            self.dst.set(self.dst_auto)

    def token_help(self):
        d = TokenDialog(self.root, expired=False)
        self.root.wait_window(d)
        if d.result:
            self.token.set(d.result)
            self.login.set('token')

    # -- settings --
    def load_settings(self):
        try:
            with open(settings_path(), encoding='utf-8') as f:
                s = json.load(f)
        except (OSError, ValueError):
            return
        for name in ('src', 'dst', 'login', 'cookies', 'quality', 'msgs_per_page', 'items_per_page', 'search_results'):
            if isinstance(s.get(name), str):
                getattr(self, name).set(s[name])
        if s.get('chat_media') in [label for label, _ in CHAT_MEDIA]:
            self.chat_media.set(s['chat_media'])
        for name in ('activity', 'offline', 'autorestart'):
            if isinstance(s.get(name), bool):
                getattr(self, name).set(s[name])
        for k, v in (s.get('sections') or {}).items():
            if k in self.sections:
                self.sections[k].set(bool(v))
        for k, v in (s.get('media') or {}).items():
            if k in self.media:
                self.media[k].set(bool(v))
        self.dst_auto = self.src.get().rstrip('\\/') + '_enriched' if self.src.get() else ''

    def save_settings(self):  # the token is deliberately not saved
        s = {name: getattr(self, name).get() for name in ('src', 'dst', 'login', 'cookies', 'quality', 'activity', 'offline',
                                                          'msgs_per_page', 'items_per_page', 'search_results',
                                                          'chat_media', 'autorestart')}
        s['sections'] = {k: v.get() for k, v in self.sections.items()}
        s['media'] = {k: v.get() for k, v in self.media.items()}
        try:
            os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
            with open(settings_path(), 'w', encoding='utf-8') as f:
                json.dump(s, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # -- running --
    def command(self):
        src = self.src.get().strip()
        if not src or not os.path.isfile(os.path.join(src, 'index.html')):
            messagebox.showerror(APP, 'В выбранной папке нет index.html. Укажите папку распакованного архива VK.')
            return None
        args = [src]
        if self.dst.get().strip():
            args.append(self.dst.get().strip())
        if self.offline.get():
            args.append('--offline')
        elif self.login.get() == 'cookies':
            if not os.path.isfile(self.cookies.get()):
                messagebox.showerror(APP, 'Выберите файл cookies.txt.')
                return None
            args += ['--cookies', self.cookies.get()]
        elif self.login.get() == 'token':
            args += ['--token', 'ask']  # the token itself goes through stdin, not the command line
        sections = [k for k, _ in SECTIONS if self.sections[k].get()]
        if not sections and not self.offline.get():
            messagebox.showerror(APP, 'Отметьте хотя бы один раздел.')
            return None
        args += ['--only', ','.join(sections or ['messages'])]
        off = [k for k, _ in MEDIA if not self.media[k].get()]
        if off:
            args += ['--no-media', ','.join(off)]
        chat_off = dict(CHAT_MEDIA).get(self.chat_media.get())
        if chat_off:
            args += ['--no-chat-media', chat_off]
        if self.activity.get():
            args.append('--github-activity')
        args += ['--max-video-height', self.quality.get()]
        for option, var, what in (('--messages-per-page', self.msgs_per_page, 'Сообщений на странице'),
                                  ('--items-per-page', self.items_per_page, 'Записей в списках'),
                                  ('--search-results', self.search_results, 'Результатов поиска сразу')):
            value = page_size(var.get())
            if value is False:
                messagebox.showerror(APP, '«%s»: впишите число или выберите из списка.' % what)
                return None
            if value is not None:
                args += [option, value]
        return worker_command() + args

    def start(self, auto=False):
        auto = auto or self.restart_job is not None  # "Начать" while waiting to restart: restart now
        self.cancel_restart()
        if self.proc:
            return
        cmd = self.command()
        if not cmd:
            return
        self.save_settings()
        if auto:
            self.restarts += 1
            self.append('\n—— Перезапуск %d ——\n' % self.restarts)
        else:
            self.quick_fails = self.restarts = 0
            self.clear_log()
        self.append('Запуск: %s\n' % ' '.join(cmd[len(worker_command()):]))
        env = dict(os.environ, VK_ARCHIVE_ENRICH_GUI='1', PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         env=env, creationflags=core.NO_WINDOW, start_new_session=(os.name != 'nt'))
        except OSError as e:
            messagebox.showerror(APP, 'Не удалось запустить: %s' % e)
            self.proc = None
            return
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()
        self.stopping = False
        self.started_at = time.time()
        self.btn_start.configure(state='disabled')
        self.btn_stop.configure(state='normal')
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)
        self.status.set('Работаю…')

    def reader(self, proc):
        for raw in iter(proc.stdout.readline, b''):
            self.lines.put(raw.decode('utf-8', 'replace'))
        self.lines.put(None)

    def stop(self):
        if self.restart_job:  # waiting to restart: don't
            self.cancel_restart()
            self.btn_stop.configure(state='disabled')
            self.status.set('Перезапуск отменён. Нажмите «Начать», чтобы продолжить.')
            return
        if not self.proc:
            return
        self.stopping = True
        self.status.set('Останавливаю…')
        if os.name == 'nt':  # the worker may have ffmpeg children: end the whole tree
            subprocess.run(['taskkill', '/PID', str(self.proc.pid), '/T', '/F'], capture_output=True, creationflags=core.NO_WINDOW)
        else:
            import signal
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                self.proc.terminate()

    def poll(self):
        try:
            while True:
                line = self.lines.get_nowait()
                if line is None:
                    self.finished()
                    break
                self.handle(line)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def handle(self, line):
        m = re.search(r'@@VKAE_ASK_TOKEN (\w+)@@', line)
        if m:
            self.answer_token(m.group(1) == 'expired')
            return
        self.append(line)
        m = re.search(r'downloads: (\d+)/(\d+) done', line)
        if m and int(m.group(2)):
            done, total = int(m.group(1)), int(m.group(2))
            self.determinate(100.0 * done / total)
            self.status.set('Файлы: %s из %s' % (core.num(done), core.num(total)))
            return
        m = re.search(r'\[(\d+)/(\d+)\] peer', line)
        if m:
            self.status.set('Переписки: %s из %s' % m.groups())
        elif 'Rendering pages' in line:
            self.indeterminate()
            self.status.set('Собираю страницы…')
        elif 'Archive:' in line:
            self.status.set('Читаю архив…')
        elif re.search(r'Likes/|Wall:|My comments|Photo albums|Documents:|People:', line):
            self.status.set(line.split(' ', 1)[-1].strip())

    def answer_token(self, expired):
        token = '' if expired else self.token.get().strip()
        if not token:
            d = TokenDialog(self.root, expired)
            self.root.wait_window(d)
            token = d.result or ''
            if token:
                self.token.set(token)
        try:
            self.proc.stdin.write((token + '\n').encode('utf-8'))
            self.proc.stdin.flush()
        except (OSError, AttributeError):
            pass

    def finished(self):
        code = self.proc.wait() if self.proc else 0
        self.proc = None
        self.btn_start.configure(state='normal')
        self.btn_stop.configure(state='disabled')
        self.on_finished(code)
        if code == 0:
            self.determinate(100)
            self.status.set('Готово! Нажмите «Открыть результат».')
            self.root.bell()
        elif code == 3:  # finished, but some parts failed (ERROR lines in the log)
            self.determinate(100)
            self.status.set('Готово, но с ошибками (строки ERROR в журнале). Нажмите «Открыть результат».')
            self.root.bell()
        elif code == 2:  # a problem only the user can fix
            self.determinate(0)
            self.status.set('Остановлено: причина в последних строках журнала. Исправьте и нажмите «Начать».')
        elif self.autorestart.get() and not self.stopping and not self.offline.get():
            self.plan_restart(code)
        else:
            self.determinate(0)
            self.status.set('Остановлено (код %s). Запустите снова — скачанное сохранится, работа продолжится.' % code)

    def plan_restart(self, code):
        """The run broke off (session lost, no internet for hours, a crash): start it again after a pause,
        a longer one if it keeps failing right after the start; give up after a few such failures."""
        quick = time.time() - self.started_at < QUICK_FAIL
        self.quick_fails = self.quick_fails + 1 if quick else 0
        if self.quick_fails > len(RESTART_DELAYS):
            self.determinate(0)
            self.status.set('Остановлено (код %s): перезапуски не помогли, причина в журнале. Нажмите «Начать», когда исправите.' % code)
            return
        delay = RESTART_DELAYS[max(self.quick_fails - 1, 0)]
        self.append('Работа прервалась (код %s). Перезапуск через %d мин. — скачанное не пропадёт.\n' % (code, delay // 60))
        self.btn_stop.configure(state='normal')
        self.countdown(time.time() + delay, code)

    def countdown(self, at, code):
        left = int(at - time.time() + 0.5)
        if left <= 0:
            self.restart_job = None
            self.start(auto=True)
            if not self.proc:  # e.g. the archive's drive is gone
                self.status.set('Перезапустить не получилось. Проверьте настройки и нажмите «Начать».')
            return
        self.status.set('Работа прервалась (код %s). Перезапуск через %d:%02d. «Стоп» — отменить, «Начать» — сейчас.' % (
            code, left // 60, left % 60))
        self.restart_job = self.root.after(1000, self.countdown, at, code)

    def cancel_restart(self):
        if self.restart_job:
            self.root.after_cancel(self.restart_job)
            self.restart_job = None

    def demo(self):
        """Example state for screenshots: invented paths and a log like a real run."""
        self.src.set(r'D:\Архив ВК')
        self.dst.set(r'D:\Архив ВК_enriched')
        self.sections['likes'].set(True)
        for line in ['18:02:11 Using the vk.ru session from Firefox',
                     '18:02:12 Logged in as id100000000',
                     '18:02:24 Archive: 27 conversations, 48 213 messages (read in 11s)',
                     '18:02:25 Messages: fetching attachments of 27 conversations',
                     '18:03:40 [12/27] peer 100000014: 1840/1840 messages from API, 2177 new files queued',
                     '18:04:10    downloads: 3120/6230 done (3109 ok), 3110 in queue',
                     '18:04:40    downloads: 3966/6230 done (3951 ok), 2264 in queue']:
            self.append(line + '\n')
        self.btn_start.configure(state='disabled')
        self.btn_stop.configure(state='normal')
        self.determinate(63.7)
        self.status.set('Файлы: 3 966 из 6 230')

    def on_finished(self, code):  # replaced by the build self-test
        pass

    def determinate(self, value):
        if str(self.progress.cget('mode')) != 'determinate':
            self.progress.stop()
            self.progress.configure(mode='determinate')
        self.progress['value'] = value

    def indeterminate(self):
        if str(self.progress.cget('mode')) != 'indeterminate':
            self.progress.configure(mode='indeterminate')
            self.progress.start(12)

    def append(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text)
        lines = int(self.log.index('end-1c').split('.')[0])
        if lines > MAX_LOG_LINES:
            self.log.delete('1.0', '%d.0' % (lines - MAX_LOG_LINES))
        self.log.see('end')
        self.log.configure(state='disabled')

    def clear_log(self):
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

    def result_dir(self):
        return self.dst.get().strip() or (self.src.get().strip().rstrip('\\/') + '_enriched')

    def open_result(self):
        index = os.path.join(self.result_dir(), 'index.html')
        if os.path.isfile(index):
            open_path(index)
        else:
            messagebox.showinfo(APP, 'Результата ещё нет: сначала нажмите «Начать».')

    def open_folder(self):
        d = self.result_dir()
        if os.path.isdir(d):
            open_path(d)
        elif os.path.isdir(self.src.get()):
            open_path(self.src.get())

    def on_close(self):
        if self.proc and not messagebox.askyesno(APP, 'Работа ещё идёт. Остановить и закрыть? Скачанное сохранится.'):
            return
        self.cancel_restart()
        self.stop()
        self.root.destroy()


def main():
    if os.name == 'nt':
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    try:
        tkfont.nametofont('TkDefaultFont').configure(size=10)
        tkfont.nametofont('TkTextFont').configure(size=10)
    except tk.TclError:
        pass
    app = App(root)
    root.update_idletasks()  # never taller than the screen (1080p laptops at 150% are only ~700pt tall)
    height = min(root.winfo_reqheight(), root.winfo_screenheight() - 90)
    root.geometry('%dx%d+%d+%d' % (root.winfo_reqwidth(), height, max(0, (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2), 20))
    # hooks for the automatic build checks; they never save settings
    if os.environ.get('VK_ARCHIVE_ENRICH_SELFTEST'):  # the window opens and closes
        app.save_settings = lambda: None
        root.after(int(os.environ['VK_ARCHIVE_ENRICH_SELFTEST']), root.destroy)
    if os.environ.get('VK_ARCHIVE_ENRICH_DEMO'):  # a filled-in window in mid-work, for the README screenshot
        app.save_settings = lambda: None
        app.demo()
    if os.environ.get('VK_ARCHIVE_ENRICH_SELFTEST_ARCHIVE'):  # a whole offline run through the window
        app.save_settings = lambda: None
        app.src.set(os.environ['VK_ARCHIVE_ENRICH_SELFTEST_ARCHIVE'])
        app.offline.set(True)
        app.on_finished = lambda code: (setattr(app, 'exit_code', code), root.after(300, root.destroy))
        root.after(300, app.start)
    root.mainloop()
    sys.exit(getattr(app, 'exit_code', 0))


if __name__ == '__main__':
    main()
