#!/usr/bin/env python3
"""
Screenshot of the program window for the README (Windows only).

Opens the window in demo mode (invented paths, a log like a real run) and captures that
window alone with the Win32 PrintWindow call, so nothing else on the screen can get into
the picture. The window closes by itself after a few seconds.

    py -m pip install pillow
    py tools\\screenshot_window.py                       (from the sources)
    py tools\\screenshot_window.py path\\to\\vk_archive_enrich.exe
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG), ('biPlanes', wt.WORD),
                ('biBitCount', wt.WORD), ('biCompression', wt.DWORD), ('biSizeImage', wt.DWORD),
                ('biXPelsPerMeter', wt.LONG), ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD), ('biClrImportant', wt.DWORD)]


def window_of(title_prefix='VK Archive Enrich', timeout=20):
    """Found by title: from the sources Python may start the window in a child process."""
    user32 = ctypes.windll.user32
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def callback(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value.startswith(title_prefix) and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True
        user32.EnumWindows(callback, 0)
        if found:
            return found[0]
        time.sleep(0.25)
    raise SystemExit('the window did not appear')


def capture(hwnd, out):
    from PIL import Image
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    user32.PrintWindow(hwnd, mem, 2)  # PW_RENDERFULLCONTENT: the window's own pixels, not the screen
    info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(hwnd, hdc)
    Image.frombuffer('RGBA', (w, h), buf, 'raw', 'BGRA', 0, 1).convert('RGB').save(out, optimize=True)
    return w, h


def main():
    if os.name != 'nt':
        sys.exit('This script captures a Windows window; run it with Windows Python.')
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    exe = sys.argv[1] if len(sys.argv) > 1 else None
    cmd = [exe] if exe else [sys.executable, os.path.join(ROOT, 'vk_archive_enrich_gui.py')]
    out = os.path.join(ROOT, 'docs', 'screenshots', 'program.png')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    env = dict(os.environ, VK_ARCHIVE_ENRICH_DEMO='1', VK_ARCHIVE_ENRICH_SELFTEST='9000')
    proc = subprocess.Popen(cmd, env=env)
    hwnd = window_of()
    time.sleep(2.5)  # let it lay out and paint
    w, h = capture(hwnd, out)
    proc.wait(timeout=30)
    print('saved %s (%dx%d)' % (os.path.relpath(out, ROOT), w, h))


if __name__ == '__main__':
    main()
