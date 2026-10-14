# -*- mode: python -*-
# PyInstaller build: one folder with two programs sharing a single Python runtime
#   vk_archive_enrich.exe      - the settings window (no console)
#   vk_archive_enrich_cli.exe  - the command line version; the window runs it to do the work
# ffmpeg.exe is included when packaging/bin/ffmpeg.exe exists (the CI downloads it).
#
#   pyinstaller packaging/vk_archive_enrich.spec --noconfirm
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
ICON = os.path.join(SPECPATH, 'icon.ico')
datas = [(os.path.join(ROOT, 'assets'), 'assets')]
binaries = [(os.path.join(SPECPATH, 'bin', 'ffmpeg.exe'), '.')] if os.path.exists(os.path.join(SPECPATH, 'bin', 'ffmpeg.exe')) else []

common = dict(pathex=[ROOT], binaries=binaries, datas=datas, hiddenimports=['yt_dlp'], noarchive=False)
cli = Analysis([os.path.join(ROOT, 'vk_archive_enrich.py')], **common)
gui = Analysis([os.path.join(ROOT, 'vk_archive_enrich_gui.py')], **common)

# UPX stays off: compressed executables are flagged by antivirus far more often.
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name='vk_archive_enrich_cli',
              console=True, icon=ICON, upx=False)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name='vk_archive_enrich',
              console=False, icon=ICON, upx=False)
COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas, name='vk_archive_enrich', upx=False)
