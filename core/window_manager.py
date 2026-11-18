"""Cross-platform Silent Window & Background Input Manager (Windows + macOS + Linux/X11).

Provides non-intrusive, window-scoped Computer Use capabilities inspired by modern
background CUA architectures:
1. Enumerate open desktop windows (`window_id`, `pid`, `app`, `title`, `bounds`).
2. Launch URLs or apps into an isolated background window WITHOUT stealing focus
   or hijacking the user's active browser tab.
3. Capture occluded/background single-window screenshots (`PrintWindow` with
   `PW_RENDERFULLCONTENT` on Windows, `screencapture -l` on macOS, `Xlib` on Linux).
4. Inject mouse clicks, wheel scrolls, shortcuts, and Unicode/Chinese text directly
   into the target window (`PostMessageW` + `WM_CHAR` on Windows, `CGEventPostToPid`
   on macOS, `XSendEvent` on X11) — zero physical mouse movement!
5. Instant "Watch Live / Tuck Back to Silent" window visibility toggle (`watch=True/False`).
6. Lightweight UI element tree extraction (`[idx] role "name" @(nx, ny)`) for fast,
   coordinate-exact clicking.
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image


@dataclass
class WindowInfo:
    window_id: str
    pid: int
    app: str
    title: str
    left: int
    top: int
    width: int
    height: int
    is_foreground: bool = False
    is_minimized: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        label_title = self.title.strip() or self.app.strip() or f"Window {self.window_id}"
        if len(label_title) > 56:
            label_title = label_title[:53] + "..."
        d["display_name"] = f"[{self.app or 'App'}] {label_title}" if self.app else label_title
        return d


@dataclass
class UIElementInfo:
    index: int
    role: str
    name: str
    norm_x: int
    norm_y: int
    width: int
    height: int
    hwnd: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "role": self.role,
            "name": self.name,
            "x": self.norm_x,
            "y": self.norm_y,
            "width": self.width,
            "height": self.height,
        }


# ======================================================================
# Windows Native Backend (ctypes user32 / gdi32 / dwmapi — zero pywin32!)
# ======================================================================
class _WindowsBackend:
    # Window messages & constants
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONDOWN = 0x0204
    WM_RBUTTONUP = 0x0205
    WM_MBUTTONDOWN = 0x0207
    WM_MBUTTONUP = 0x0208
    WM_MOUSEWHEEL = 0x020A
    WM_MOUSEHWHEEL = 0x020E
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_CHAR = 0x0102
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_SETTEXT = 0x000C

    MK_LBUTTON = 0x0001
    MK_RBUTTON = 0x0002
    MK_MBUTTON = 0x0010

    SW_RESTORE = 9
    SW_SHOW = 5
    SW_SHOWNOACTIVATE = 4
    SW_SHOWNA = 8

    HWND_TOP = 0
    HWND_BOTTOM = 1
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040

    PW_RENDERFULLCONTENT = 0x00000002
    DWMWA_CLOAKED = 14
    CWP_SKIPALL = 0x0001 | 0x0002 | 0x0004  # invisible | disabled | transparent

    VK_MAP: Dict[str, int] = {
        "backspace": 0x08,
        "tab": 0x09,
        "enter": 0x0D,
        "return": 0x0D,
        "shift": 0x10,
        "ctrl": 0x11,
        "control": 0x11,
        "alt": 0x12,
        "option": 0x12,
        "pause": 0x13,
        "capslock": 0x14,
        "esc": 0x1B,
        "escape": 0x1B,
        "space": 0x20,
        "pageup": 0x21,
        "pagedown": 0x22,
        "end": 0x23,
        "home": 0x24,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
        "insert": 0x2D,
        "delete": 0x2E,
        "win": 0x5B,
        "command": 0x5B,
        "f1": 0x70,
        "f2": 0x71,
        "f3": 0x72,
        "f4": 0x73,
        "f5": 0x74,
        "f6": 0x75,
        "f7": 0x76,
        "f8": 0x77,
        "f9": 0x78,
        "f10": 0x79,
        "f11": 0x7A,
        "f12": 0x7B,
    }

    def __init__(self) -> None:
        from ctypes import wintypes

        self.wintypes = wintypes
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32
        self.kernel32 = ctypes.windll.kernel32
        try:
            self.dwmapi = ctypes.windll.dwmapi
        except Exception:
            self.dwmapi = None
        try:
            self.psapi = ctypes.windll.psapi
        except Exception:
            self.psapi = None

        self._setup_signatures()

    def _setup_signatures(self) -> None:
        wt = self.wintypes
        u32 = self.user32
        g32 = self.gdi32

        u32.IsWindow.argtypes = [wt.HWND]
        u32.IsWindow.restype = wt.BOOL
        u32.IsWindowVisible.argtypes = [wt.HWND]
        u32.IsWindowVisible.restype = wt.BOOL
        u32.IsIconic.argtypes = [wt.HWND]
        u32.IsIconic.restype = wt.BOOL
        u32.GetForegroundWindow.argtypes = []
        u32.GetForegroundWindow.restype = wt.HWND
        u32.GetWindowTextLengthW.argtypes = [wt.HWND]
        u32.GetWindowTextLengthW.restype = ctypes.c_int
        u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u32.GetWindowTextW.restype = ctypes.c_int
        u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u32.GetClassNameW.restype = ctypes.c_int
        u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u32.GetWindowRect.restype = wt.BOOL
        u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u32.GetWindowThreadProcessId.restype = wt.DWORD
        u32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
        u32.ScreenToClient.restype = wt.BOOL
        u32.ChildWindowFromPointEx.argtypes = [wt.HWND, wt.POINT, wt.UINT]
        u32.ChildWindowFromPointEx.restype = wt.HWND
        u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u32.PostMessageW.restype = wt.BOOL
        u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u32.ShowWindow.restype = wt.BOOL
        u32.SetForegroundWindow.argtypes = [wt.HWND]
        u32.SetForegroundWindow.restype = wt.BOOL
        u32.SetWindowPos.argtypes = [
            wt.HWND,
            wt.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wt.UINT,
        ]
        u32.SetWindowPos.restype = wt.BOOL
        u32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
        u32.PrintWindow.restype = wt.BOOL
        u32.GetWindowDC.argtypes = [wt.HWND]
        u32.GetWindowDC.restype = wt.HDC
        u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
        u32.ReleaseDC.restype = ctypes.c_int
        u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u32.GetAsyncKeyState.restype = ctypes.c_short
        u32.VkKeyScanW.argtypes = [wt.WCHAR]
        u32.VkKeyScanW.restype = ctypes.c_short

        g32.CreateCompatibleDC.argtypes = [wt.HDC]
        g32.CreateCompatibleDC.restype = wt.HDC
        g32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
        g32.CreateCompatibleBitmap.restype = wt.HBITMAP
        g32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
        g32.SelectObject.restype = wt.HGDIOBJ
        g32.DeleteObject.argtypes = [wt.HGDIOBJ]
        g32.DeleteObject.restype = wt.BOOL
        g32.DeleteDC.argtypes = [wt.HDC]
        g32.DeleteDC.restype = wt.BOOL

    @staticmethod
    def _makelparam(lo: int, hi: int) -> int:
        return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)

    def check_global_stop_hotkey(self) -> bool:
        """Detect Ctrl + Alt + Q hardware hotkey anywhere on Windows."""
        try:
            ctrl = bool(self.user32.GetAsyncKeyState(0x11) & 0x8000)
            alt = bool(self.user32.GetAsyncKeyState(0x12) & 0x8000)
            q_key = bool(self.user32.GetAsyncKeyState(0x51) & 0x8000)
            return ctrl and alt and q_key
        except Exception:
            return False

    def _get_process_name(self, pid: int) -> str:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h_proc:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = self.wintypes.DWORD(260)
            if self.kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return ""
        except Exception:
            return ""
        finally:
            self.kernel32.CloseHandle(h_proc)

    def _is_cloaked(self, hwnd: int) -> bool:
        if not self.dwmapi:
            return False
        cloaked = ctypes.c_int(0)
        res = self.dwmapi.DwmGetWindowAttribute(
            hwnd, self.DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return res == 0 and cloaked.value != 0

    def _get_text(self, hwnd: int) -> str:
        length = self.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 2)
        self.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value.strip()

    def _get_class(self, hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(128)
        self.user32.GetClassNameW(hwnd, buf, 127)
        return buf.value.strip()

    def list_windows(self) -> List[WindowInfo]:
        wt = self.wintypes
        fg_hwnd = self.user32.GetForegroundWindow()
        results: List[WindowInfo] = []
        skip_classes = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def _cb(hwnd: int, _lparam: int) -> bool:
            if not hwnd or not self.user32.IsWindowVisible(hwnd):
                return True
            if self._is_cloaked(hwnd):
                return True
            cls = self._get_class(hwnd)
            if cls in skip_classes:
                return True
            title = self._get_text(hwnd)
            if not title:
                return True

            rect = wt.RECT()
            if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            is_min = bool(self.user32.IsIconic(hwnd))
            if not is_min and (w < 80 or h < 60):
                return True

            pid = wt.DWORD(0)
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            app = self._get_process_name(pid.value)

            results.append(
                WindowInfo(
                    window_id=str(int(hwnd)),
                    pid=int(pid.value),
                    app=app,
                    title=title,
                    left=int(rect.left),
                    top=int(rect.top),
                    width=max(1, int(w)),
                    height=max(1, int(h)),
                    is_foreground=bool(fg_hwnd and int(hwnd) == int(fg_hwnd)),
                    is_minimized=is_min,
                )
            )
            return True

        cb_func = WNDENUMPROC(_cb)
        self.user32.EnumWindows(cb_func, 0)
        return results

    def get_window(self, window_id: str) -> Optional[WindowInfo]:
        try:
            hwnd = int(window_id)
        except (TypeError, ValueError):
            return None
        if not self.user32.IsWindow(hwnd):
            return None
        rect = self.wintypes.RECT()
        self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = self.wintypes.DWORD(0)
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        fg_hwnd = self.user32.GetForegroundWindow()
        return WindowInfo(
            window_id=str(hwnd),
            pid=int(pid.value),
            app=self._get_process_name(pid.value),
            title=self._get_text(hwnd),
            left=int(rect.left),
            top=int(rect.top),
            width=max(1, int(rect.right - rect.left)),
            height=max(1, int(rect.bottom - rect.top)),
            is_foreground=bool(fg_hwnd and int(fg_hwnd) == hwnd),
            is_minimized=bool(self.user32.IsIconic(hwnd)),
        )

    def ensure_unminimized_silent(self, hwnd: int) -> None:
        """Restore minimized window WITHOUT activating it so PrintWindow stays live."""
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, self.SW_SHOWNOACTIVATE)
            self.user32.SetWindowPos(
                hwnd,
                self.HWND_BOTTOM,
                0,
                0,
                0,
                0,
                self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE,
            )
            time.sleep(0.08)

    def set_visibility(self, window_id: str, watch: bool) -> bool:
        hwnd = int(window_id)
        if not self.user32.IsWindow(hwnd):
            return False
        if watch:
            if self.user32.IsIconic(hwnd):
                self.user32.ShowWindow(hwnd, self.SW_RESTORE)
            else:
                self.user32.ShowWindow(hwnd, self.SW_SHOW)
            self.user32.SetWindowPos(
                hwnd,
                self.HWND_TOP,
                0,
                0,
                0,
                0,
                self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_SHOWWINDOW,
            )
            self.user32.SetForegroundWindow(hwnd)
        else:
            # Tuck behind active windows without minimizing so background rendering continues!
            if self.user32.IsIconic(hwnd):
                self.user32.ShowWindow(hwnd, self.SW_SHOWNOACTIVATE)
            self.user32.SetWindowPos(
                hwnd,
                self.HWND_BOTTOM,
                0,
                0,
                0,
                0,
                self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE,
            )
        return True

    def capture_window(self, window_id: str) -> Optional[Image.Image]:
        hwnd = int(window_id)
        if not self.user32.IsWindow(hwnd):
            return None
        self.ensure_unminimized_silent(hwnd)
        rect = self.wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 4 or h <= 4:
            return None

        hwnd_dc = self.user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = self.gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = self.gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        old_obj = self.gdi32.SelectObject(mem_dc, bmp)
        try:
            ok = self.user32.PrintWindow(hwnd, mem_dc, self.PW_RENDERFULLCONTENT)
            if not ok:
                ok = self.user32.PrintWindow(hwnd, mem_dc, 0)
            if not ok:
                return None

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", ctypes.c_uint32),
                    ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32),
                ]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # Top-down DIB
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0  # BI_RGB

            buf = ctypes.create_string_buffer(w * h * 4)
            lines = self.gdi32.GetDIBits(
                mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0
            )
            if lines != h:
                return None
            return Image.frombytes("RGB", (w, h), buf.raw, "raw", "BGRX")
        finally:
            self.gdi32.SelectObject(mem_dc, old_obj)
            self.gdi32.DeleteObject(bmp)
            self.gdi32.DeleteDC(mem_dc)
            self.user32.ReleaseDC(hwnd, hwnd_dc)

    def _resolve_child_target(
        self, root_hwnd: int, norm_x: float, norm_y: float
    ) -> Tuple[int, int, int, int, int]:
        """Resolve normalized [0,1000] window coords -> (child_hwnd, client_x, client_y, screen_x, screen_y)."""
        self.ensure_unminimized_silent(root_hwnd)
        rect = self.wintypes.RECT()
        self.user32.GetWindowRect(root_hwnd, ctypes.byref(rect))
        w = max(1, rect.right - rect.left)
        h = max(1, rect.bottom - rect.top)
        nx = max(0.0, min(1000.0, float(norm_x)))
        ny = max(0.0, min(1000.0, float(norm_y)))
        sx = int(round(rect.left + (nx / 1000.0) * max(1, w - 1)))
        sy = int(round(rect.top + (ny / 1000.0) * max(1, h - 1)))

        cur = root_hwnd
        for _ in range(12):
            pt = self.wintypes.POINT(sx, sy)
            self.user32.ScreenToClient(cur, ctypes.byref(pt))
            child = self.user32.ChildWindowFromPointEx(cur, pt, self.CWP_SKIPALL)
            if not child or int(child) == int(cur):
                break
            cur = int(child)

        final_pt = self.wintypes.POINT(sx, sy)
        self.user32.ScreenToClient(cur, ctypes.byref(final_pt))
        return cur, int(final_pt.x), int(final_pt.y), sx, sy

    def silent_click(
        self, window_id: str, norm_x: float, norm_y: float, button: str = "left", clicks: int = 1
    ) -> Dict[str, Any]:
        root_hwnd = int(window_id)
        target_hwnd, cx, cy, sx, sy = self._resolve_child_target(root_hwnd, norm_x, norm_y)
        lparam = self._makelparam(cx, cy)

        btn = button.lower().strip()
        if btn == "right":
            down_msg, up_msg, mk = self.WM_RBUTTONDOWN, self.WM_RBUTTONUP, self.MK_RBUTTON
        elif btn == "middle":
            down_msg, up_msg, mk = self.WM_MBUTTONDOWN, self.WM_MBUTTONUP, self.MK_MBUTTON
        else:
            down_msg, up_msg, mk = self.WM_LBUTTONDOWN, self.WM_LBUTTONUP, self.MK_LBUTTON

        # Wake internal control focus state without calling SetForegroundWindow (never steals OS focus!)
        self.user32.PostMessageW(root_hwnd, 0x0006, 1, 0)  # WM_ACTIVATE, WA_ACTIVE
        self.user32.PostMessageW(target_hwnd, 0x0007, 0, 0)  # WM_SETFOCUS
        self.user32.PostMessageW(target_hwnd, self.WM_MOUSEMOVE, 0, lparam)
        for idx in range(max(1, min(3, int(clicks)))):
            if idx == 1 and btn == "left":
                self.user32.PostMessageW(target_hwnd, self.WM_LBUTTONDBLCLK, mk, lparam)
            else:
                self.user32.PostMessageW(target_hwnd, down_msg, mk, lparam)
            time.sleep(0.012)
            self.user32.PostMessageW(target_hwnd, up_msg, 0, lparam)
            if idx + 1 < clicks:
                time.sleep(0.025)

        return {
            "ok": True,
            "mode": "silent_win32",
            "window_id": str(root_hwnd),
            "target_hwnd": str(target_hwnd),
            "client_x": cx,
            "client_y": cy,
            "screen_x": sx,
            "screen_y": sy,
        }

    def silent_move(self, window_id: str, norm_x: float, norm_y: float) -> Dict[str, Any]:
        root_hwnd = int(window_id)
        target_hwnd, cx, cy, sx, sy = self._resolve_child_target(root_hwnd, norm_x, norm_y)
        self.user32.PostMessageW(target_hwnd, self.WM_MOUSEMOVE, 0, self._makelparam(cx, cy))
        return {"ok": True, "mode": "silent_win32", "client_x": cx, "client_y": cy, "screen_x": sx, "screen_y": sy}

    def silent_scroll(
        self, window_id: str, direction: str, ticks: int, norm_x: float = 500.0, norm_y: float = 500.0
    ) -> Dict[str, Any]:
        root_hwnd = int(window_id)
        target_hwnd, _cx, _cy, sx, sy = self._resolve_child_target(root_hwnd, norm_x, norm_y)
        d = direction.lower().strip()
        delta = 120 * max(1, min(30, int(ticks)))
        if d in ("down", "left"):
            delta = -delta
        wparam = ((delta & 0xFFFF) << 16)
        # WM_MOUSEWHEEL expects screen coordinates in lParam
        lparam = self._makelparam(sx, sy)
        msg = self.WM_MOUSEHWHEEL if d in ("left", "right") else self.WM_MOUSEWHEEL
        self.user32.PostMessageW(target_hwnd, msg, wparam, lparam)
        return {"ok": True, "mode": "silent_win32", "direction": d, "ticks": ticks}

    def silent_drag(
        self, window_id: str, sx_norm: float, sy_norm: float, ex_norm: float, ey_norm: float
    ) -> Dict[str, Any]:
        root_hwnd = int(window_id)
        target_hwnd, cx1, cy1, _, _ = self._resolve_child_target(root_hwnd, sx_norm, sy_norm)
        _, cx2, cy2, _, _ = self._resolve_child_target(root_hwnd, ex_norm, ey_norm)
        self.user32.PostMessageW(target_hwnd, self.WM_MOUSEMOVE, 0, self._makelparam(cx1, cy1))
        self.user32.PostMessageW(target_hwnd, self.WM_LBUTTONDOWN, self.MK_LBUTTON, self._makelparam(cx1, cy1))
        steps = 6
        for i in range(1, steps + 1):
            ix = int(round(cx1 + (cx2 - cx1) * (i / steps)))
            iy = int(round(cy1 + (cy2 - cy1) * (i / steps)))
            self.user32.PostMessageW(
                target_hwnd, self.WM_MOUSEMOVE, self.MK_LBUTTON, self._makelparam(ix, iy)
            )
            time.sleep(0.012)
        self.user32.PostMessageW(target_hwnd, self.WM_LBUTTONUP, 0, self._makelparam(cx2, cy2))
        return {"ok": True, "mode": "silent_win32"}

    def _vk_for_token(self, token: str) -> int:
        t = token.strip().lower()
        if t in self.VK_MAP:
            return self.VK_MAP[t]
        if len(t) == 1:
            vk_scan = self.user32.VkKeyScanW(t)
            if vk_scan != -1:
                return vk_scan & 0xFF
            return ord(t.upper())
        return 0

    def silent_key(
        self, window_id: str, keys: str, norm_x: float = 500.0, norm_y: float = 500.0
    ) -> Dict[str, Any]:
        root_hwnd = int(window_id)
        target_hwnd, _, _, _, _ = self._resolve_child_target(root_hwnd, norm_x, norm_y)
        self.user32.PostMessageW(root_hwnd, 0x0006, 1, 0)
        self.user32.PostMessageW(target_hwnd, 0x0007, 0, 0)
        parts = [p.strip().lower() for p in keys.replace("-", "+").split("+") if p.strip()]
        vks = [self._vk_for_token(p) for p in parts if self._vk_for_token(p)]
        if not vks:
            return {"ok": False, "error": f"Unrecognized key: {keys}"}

        is_alt = 0x12 in vks
        down_msg = self.WM_SYSKEYDOWN if is_alt else self.WM_KEYDOWN
        up_msg = self.WM_SYSKEYUP if is_alt else self.WM_KEYUP

        for vk in vks[:-1]:
            self.user32.PostMessageW(target_hwnd, down_msg, vk, 1)
            time.sleep(0.01)
        last_vk = vks[-1]
        self.user32.PostMessageW(target_hwnd, down_msg, last_vk, 1)
        time.sleep(0.012)
        self.user32.PostMessageW(target_hwnd, up_msg, last_vk, 0xC0000001)
        for vk in reversed(vks[:-1]):
            self.user32.PostMessageW(target_hwnd, up_msg, vk, 0xC0000001)

        return {"ok": True, "mode": "silent_win32", "keys": "+".join(parts)}

    def silent_type(
        self,
        window_id: str,
        text: str,
        norm_x: Optional[float] = None,
        norm_y: Optional[float] = None,
        clear_first: bool = False,
        press_enter: bool = False,
        fallback_cursor_norm: Tuple[int, int] = (500, 500),
    ) -> Dict[str, Any]:
        tx = norm_x if norm_x is not None else float(fallback_cursor_norm[0])
        ty = norm_y if norm_y is not None else float(fallback_cursor_norm[1])
        if norm_x is not None and norm_y is not None:
            self.silent_click(window_id, tx, ty, button="left", clicks=1)
            time.sleep(0.03)

        root_hwnd = int(window_id)
        target_hwnd, _, _, _, _ = self._resolve_child_target(root_hwnd, tx, ty)
        self.user32.PostMessageW(root_hwnd, 0x0006, 1, 0)
        self.user32.PostMessageW(target_hwnd, 0x0007, 0, 0)

        if clear_first:
            self.silent_key(window_id, "ctrl+a", tx, ty)
            time.sleep(0.02)
            self.silent_key(window_id, "backspace", tx, ty)
            time.sleep(0.02)

        # Inject UTF-16LE code units directly via WM_CHAR — supports Chinese, ASCII & Emoji
        # without touching the user's OS clipboard or focus!
        if text:
            utf16 = text.encode("utf-16-le")
            for idx in range(0, len(utf16), 2):
                code_unit = int.from_bytes(utf16[idx : idx + 2], "little")
                self.user32.PostMessageW(target_hwnd, self.WM_CHAR, code_unit, 1)

        if press_enter:
            time.sleep(0.02)
            self.silent_key(window_id, "enter", tx, ty)

        return {"ok": True, "mode": "silent_win32_wm_char", "typed_length": len(text)}

    def get_elements(self, window_id: str, max_items: int = 35) -> List[UIElementInfo]:
        """Fast child-control + UIAutomation element discovery on Windows."""
        root_hwnd = int(window_id)
        if not self.user32.IsWindow(root_hwnd):
            return []
        root_rect = self.wintypes.RECT()
        self.user32.GetWindowRect(root_hwnd, ctypes.byref(root_rect))
        rw = max(1, root_rect.right - root_rect.left)
        rh = max(1, root_rect.bottom - root_rect.top)

        items: List[UIElementInfo] = []
        wt = self.wintypes
        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def _enum_child(chwnd: int, _lp: int) -> bool:
            if len(items) >= max_items:
                return False
            if not self.user32.IsWindowVisible(chwnd):
                return True
            crect = wt.RECT()
            if not self.user32.GetWindowRect(chwnd, ctypes.byref(crect)):
                return True
            cw = crect.right - crect.left
            ch = crect.bottom - crect.top
            if cw < 12 or ch < 12:
                return True
            name = self._get_text(chwnd)
            cls = self._get_class(chwnd)
            if not name and cls.lower() not in ("edit", "richedit50w", "richedit20w", "chrome_renderwidgethosthwnd"):
                return True
            cx = (crect.left + crect.right) / 2.0
            cy = (crect.top + crect.bottom) / 2.0
            nx = int(round(((cx - root_rect.left) / max(1, rw - 1)) * 1000.0))
            ny = int(round(((cy - root_rect.top) / max(1, rh - 1)) * 1000.0))
            if 0 <= nx <= 1000 and 0 <= ny <= 1000:
                items.append(
                    UIElementInfo(
                        index=len(items) + 1,
                        role=cls or "Control",
                        name=name[:60],
                        norm_x=nx,
                        norm_y=ny,
                        width=int(cw),
                        height=int(ch),
                        hwnd=int(chwnd),
                    )
                )
            return True

        self.user32.EnumChildWindows(root_hwnd, WNDENUMPROC(_enum_child), 0)
        return items


# ======================================================================
# macOS Native Backend (CoreGraphics / CoreFoundation + screencapture -l)
# ======================================================================
class _MacOSBackend:
    kCGWindowListOptionOnScreenOnly = 1 << 0
    kCGWindowListExcludeDesktopElements = 1 << 4
    kCGNullWindowID = 0

    kCGEventLeftMouseDown = 1
    kCGEventLeftMouseUp = 2
    kCGEventRightMouseDown = 3
    kCGEventRightMouseUp = 4
    kCGEventMouseMoved = 5
    kCGEventLeftMouseDragged = 6
    kCGEventKeyDown = 10
    kCGEventKeyUp = 11
    kCGEventScrollWheel = 22

    kCGMouseButtonLeft = 0
    kCGMouseButtonRight = 1
    kCGScrollEventUnitLine = 1

    MAC_KEYCODES: Dict[str, int] = {
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
        "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
        "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "enter": 36,
        "return": 36, "l": 37, "j": 38, "k": 40, ";": 41, ",": 43, "/": 44, "n": 45,
        "m": 46, ".": 47, "tab": 48, "space": 49, "backspace": 51, "esc": 53, "escape": 53,
        "command": 55, "cmd": 55, "shift": 56, "option": 58, "alt": 58, "ctrl": 59,
        "control": 59, "delete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
        "left": 123, "right": 124, "down": 125, "up": 126,
    }

    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    def __init__(self) -> None:
        self.cg = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        self.cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._setup_signatures()

    def _setup_signatures(self) -> None:
        cg, cf = self.cg, self.cf
        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p

        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        cf.CFRelease.restype = None

        cg.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, self.CGPoint, ctypes.c_uint32
        ]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
        cg.CGEventSetIntegerValueField.restype = None
        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventSetFlags.restype = None
        cg.CGEventKeyboardSetUnicodeString.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)
        ]
        cg.CGEventKeyboardSetUnicodeString.restype = None
        cg.CGEventPostToPid.argtypes = [ctypes.c_int32, ctypes.c_void_p]
        cg.CGEventPostToPid.restype = None

    def _cfstr(self, key: str) -> int:
        kCFStringEncodingUTF8 = 0x08000100
        return self.cf.CFStringCreateWithCString(None, key.encode("utf-8"), kCFStringEncodingUTF8)

    def _dict_str(self, d: int, key: str) -> str:
        k = self._cfstr(key)
        try:
            val = self.cf.CFDictionaryGetValue(d, k)
            if not val:
                return ""
            buf = ctypes.create_string_buffer(512)
            if self.cf.CFStringGetCString(val, buf, 512, 0x08000100):
                return buf.value.decode("utf-8", errors="replace")
            return ""
        finally:
            if k:
                self.cf.CFRelease(k)

    def _dict_int(self, d: int, key: str, default: int = 0) -> int:
        k = self._cfstr(key)
        try:
            val = self.cf.CFDictionaryGetValue(d, k)
            if not val:
                return default
            out = ctypes.c_int64(0)
            # kCFNumberSInt64Type = 4
            if self.cf.CFNumberGetValue(val, 4, ctypes.byref(out)):
                return int(out.value)
            return default
        finally:
            if k:
                self.cf.CFRelease(k)

    def _dict_float(self, d: int, key: str, default: float = 0.0) -> float:
        k = self._cfstr(key)
        try:
            val = self.cf.CFDictionaryGetValue(d, k)
            if not val:
                return default
            out = ctypes.c_double(0.0)
            # kCFNumberFloat64Type = 6
            if self.cf.CFNumberGetValue(val, 6, ctypes.byref(out)):
                return float(out.value)
            return default
        finally:
            if k:
                self.cf.CFRelease(k)

    def list_windows(self) -> List[WindowInfo]:
        opts = self.kCGWindowListOptionOnScreenOnly | self.kCGWindowListExcludeDesktopElements
        arr = self.cg.CGWindowListCopyWindowInfo(opts, self.kCGNullWindowID)
        if not arr:
            return []
        results: List[WindowInfo] = []
        skip_owners = {"Window Server", "Dock", "Control Center", "Notification Center", "Spotlight"}
        try:
            count = self.cf.CFArrayGetCount(arr)
            first_normal_seen = False
            for idx in range(count):
                item = self.cf.CFArrayGetValueAtIndex(arr, idx)
                if not item:
                    continue
                layer = self._dict_int(item, "kCGWindowLayer", 0)
                if layer != 0:
                    continue
                owner = self._dict_str(item, "kCGWindowOwnerName")
                if owner in skip_owners:
                    continue
                win_id = self._dict_int(item, "kCGWindowNumber", 0)
                pid = self._dict_int(item, "kCGWindowOwnerPID", 0)
                title = self._dict_str(item, "kCGWindowName")

                k_bounds = self._cfstr("kCGWindowBounds")
                bounds = self.cf.CFDictionaryGetValue(item, k_bounds)
                if k_bounds:
                    self.cf.CFRelease(k_bounds)
                if not bounds:
                    continue
                x = int(round(self._dict_float(bounds, "X", 0.0)))
                y = int(round(self._dict_float(bounds, "Y", 0.0)))
                w = int(round(self._dict_float(bounds, "Width", 0.0)))
                h = int(round(self._dict_float(bounds, "Height", 0.0)))
                if w < 100 or h < 80:
                    continue

                is_fg = not first_normal_seen
                first_normal_seen = True
                results.append(
                    WindowInfo(
                        window_id=str(win_id),
                        pid=pid,
                        app=owner,
                        title=title or owner,
                        left=x,
                        top=y,
                        width=w,
                        height=h,
                        is_foreground=is_fg,
                        is_minimized=False,
                    )
                )
            return results
        finally:
            self.cf.CFRelease(arr)

    def get_window(self, window_id: str) -> Optional[WindowInfo]:
        for w in self.list_windows():
            if w.window_id == str(window_id):
                return w
        return None

    def capture_window(self, window_id: str) -> Optional[Image.Image]:
        tmp_path = os.path.join(
            tempfile.gettempdir(), f"agy_cu_win_{os.getpid()}_{window_id}.jpg"
        )
        try:
            res = subprocess.run(
                ["/usr/sbin/screencapture", "-x", "-o", "-t", "jpg", "-l", str(window_id), tmp_path],
                capture_output=True,
                timeout=4,
            )
            if res.returncode != 0 or not os.path.exists(tmp_path):
                return None
            with Image.open(tmp_path) as im:
                return im.convert("RGB")
        except Exception:
            return None
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def set_visibility(self, window_id: str, watch: bool) -> bool:
        win = self.get_window(window_id)
        if not win:
            return False
        if watch:
            script = (
                f'tell application "System Events" to set frontmost of '
                f'(first process whose unix id is {win.pid}) to true'
            )
        else:
            # Send behind by activating Finder or previous app without hiding backing store
            script = (
                f'tell application "System Events"\n'
                f'  set others to every process whose visible is true and unix id is not {win.pid}\n'
                f'  if (count of others) > 0 then set frontmost of item 1 of others to true\n'
                f'end tell'
            )
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=3)
        return True

    def _norm_to_cgpoint(self, win: WindowInfo, norm_x: float, norm_y: float) -> Tuple[int, int]:
        nx = max(0.0, min(1000.0, float(norm_x)))
        ny = max(0.0, min(1000.0, float(norm_y)))
        sx = int(round(win.left + (nx / 1000.0) * max(1, win.width - 1)))
        sy = int(round(win.top + (ny / 1000.0) * max(1, win.height - 1)))
        return sx, sy

    def silent_click(
        self, window_id: str, norm_x: float, norm_y: float, button: str = "left", clicks: int = 1
    ) -> Dict[str, Any]:
        win = self.get_window(window_id)
        if not win:
            raise RuntimeError(f"Window {window_id} not found")
        sx, sy = self._norm_to_cgpoint(win, norm_x, norm_y)
        pt = self.CGPoint(float(sx), float(sy))
        is_right = button.lower().strip() == "right"
        down_t = self.kCGEventRightMouseDown if is_right else self.kCGEventLeftMouseDown
        up_t = self.kCGEventRightMouseUp if is_right else self.kCGEventLeftMouseUp
        btn_c = self.kCGMouseButtonRight if is_right else self.kCGMouseButtonLeft

        # kCGMouseEventClickState = 1
        for click_idx in range(1, max(1, min(3, int(clicks))) + 1):
            ev_down = self.cg.CGEventCreateMouseEvent(None, down_t, pt, btn_c)
            ev_up = self.cg.CGEventCreateMouseEvent(None, up_t, pt, btn_c)
            if ev_down and ev_up:
                self.cg.CGEventSetIntegerValueField(ev_down, 1, click_idx)
                self.cg.CGEventSetIntegerValueField(ev_up, 1, click_idx)
                self.cg.CGEventPostToPid(win.pid, ev_down)
                time.sleep(0.015)
                self.cg.CGEventPostToPid(win.pid, ev_up)
                self.cf.CFRelease(ev_down)
                self.cf.CFRelease(ev_up)
        return {"ok": True, "mode": "silent_macos_pid", "pid": win.pid, "screen_x": sx, "screen_y": sy}

    def silent_move(self, window_id: str, norm_x: float, norm_y: float) -> Dict[str, Any]:
        win = self.get_window(window_id)
        if not win:
            raise RuntimeError(f"Window {window_id} not found")
        sx, sy = self._norm_to_cgpoint(win, norm_x, norm_y)
        ev = self.cg.CGEventCreateMouseEvent(
            None, self.kCGEventMouseMoved, self.CGPoint(float(sx), float(sy)), 0
        )
        if ev:
            self.cg.CGEventPostToPid(win.pid, ev)
            self.cf.CFRelease(ev)
        return {"ok": True, "mode": "silent_macos_pid", "screen_x": sx, "screen_y": sy}

    def silent_scroll(
        self, window_id: str, direction: str, ticks: int, norm_x: float = 500.0, norm_y: float = 500.0
    ) -> Dict[str, Any]:
        win = self.get_window(window_id)
        if not win:
            raise RuntimeError(f"Window {window_id} not found")
        d = direction.lower().strip()
        val = max(1, min(30, int(ticks))) * 3
        if d in ("down", "right"):
            val = -val
        try:
            self.cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
            ev = self.cg.CGEventCreateScrollWheelEvent(
                None, self.kCGScrollEventUnitLine, 1, ctypes.c_int32(val)
            )
            if ev:
                self.cg.CGEventPostToPid(win.pid, ev)
                self.cf.CFRelease(ev)
        except Exception:
            pass
        return {"ok": True, "mode": "silent_macos_pid", "direction": d, "ticks": ticks}

    def silent_drag(
        self, window_id: str, sx_norm: float, sy_norm: float, ex_norm: float, ey_norm: float
    ) -> Dict[str, Any]:
        win = self.get_window(window_id)
        if not win:
            raise RuntimeError(f"Window {window_id} not found")
        sx1, sy1 = self._norm_to_cgpoint(win, sx_norm, sy_norm)
        sx2, sy2 = self._norm_to_cgpoint(win, ex_norm, ey_norm)
        ev_down = self.cg.CGEventCreateMouseEvent(
            None, self.kCGEventLeftMouseDown, self.CGPoint(float(sx1), float(sy1)), 0
        )
        ev_drag = self.cg.CGEventCreateMouseEvent(
            None, self.kCGEventLeftMouseDragged, self.CGPoint(float(sx2), float(sy2)), 0
        )
        ev_up = self.cg.CGEventCreateMouseEvent(
            None, self.kCGEventLeftMouseUp, self.CGPoint(float(sx2), float(sy2)), 0
        )
        for ev in (ev_down, ev_drag, ev_up):
            if ev:
                self.cg.CGEventPostToPid(win.pid, ev)
                time.sleep(0.02)
                self.cf.CFRelease(ev)
        return {"ok": True, "mode": "silent_macos_pid"}

    def silent_key(
        self, window_id: str, keys: str, norm_x: float = 500.0, norm_y: float = 500.0
    ) -> Dict[str, Any]:
        win = self.get_window(window_id)
        if not win:
            raise RuntimeError(f"Window {window_id} not found")
        parts = [p.strip().lower() for p in keys.replace("-", "+").split("+") if p.strip()]
        flags = 0
        for mod in parts[:-1]:
            if mod in ("cmd", "command", "super", "win"):
                flags |= 1 << 20  # kCGEventFlagMaskCommand
            elif mod in ("shift",):
                flags |= 1 << 17  # kCGEventFlagMaskShift
            elif mod in ("alt", "opt", "option"):
                flags |= 1 << 19  # kCGEventFlagMaskAlternate
            elif mod in ("ctrl", "control"):
                flags |= 1 << 18  # kCGEventFlagMaskControl

        kc = self.MAC_KEYCODES.get(parts[-1], 36)
        ev_d = self.cg.CGEventCreateKeyboardEvent(None, kc, True)
        ev_u = self.cg.CGEventCreateKeyboardEvent(None, kc, False)
        if ev_d and ev_u:
            if flags:
                self.cg.CGEventSetFlags(ev_d, flags)
                self.cg.CGEventSetFlags(ev_u, flags)
            self.cg.CGEventPostToPid(win.pid, ev_d)
            time.sleep(0.015)
            self.cg.CGEventPostToPid(win.pid, ev_u)
            self.cf.CFRelease(ev_d)
            self.cf.CFRelease(ev_u)
        return {"ok": True, "mode": "silent_macos_pid", "keys": "+".join(parts)}

    def silent_type(
        self,
        window_id: str,
        text: str,
        norm_x: Optional[float] = None,
        norm_y: Optional[float] = None,
        clear_first: bool = False,
        press_enter: bool = False,
        fallback_cursor_norm: Tuple[int, int] = (500, 500),
    ) -> Dict[str, Any]:
        win = self.get_window(window_id)
        if not win:
            raise RuntimeError(f"Window {window_id} not found")
        if norm_x is not None and norm_y is not None:
            self.silent_click(window_id, norm_x, norm_y, "left", 1)
            time.sleep(0.03)
        if clear_first:
            self.silent_key(window_id, "cmd+a")
            time.sleep(0.02)
            self.silent_key(window_id, "backspace")
            time.sleep(0.02)
        if text:
            utf16 = text.encode("utf-16-le")
            count = len(utf16) // 2
            arr = (ctypes.c_uint16 * count).from_buffer_copy(utf16)
            ev_d = self.cg.CGEventCreateKeyboardEvent(None, 0, True)
            ev_u = self.cg.CGEventCreateKeyboardEvent(None, 0, False)
            if ev_d and ev_u:
                self.cg.CGEventKeyboardSetUnicodeString(ev_d, count, arr)
                self.cg.CGEventKeyboardSetUnicodeString(ev_u, count, arr)
                self.cg.CGEventPostToPid(win.pid, ev_d)
                self.cg.CGEventPostToPid(win.pid, ev_u)
                self.cf.CFRelease(ev_d)
                self.cf.CFRelease(ev_u)
        if press_enter:
            time.sleep(0.02)
            self.silent_key(window_id, "enter")
        return {"ok": True, "mode": "silent_macos_unicode", "typed_length": len(text)}

    def get_elements(self, window_id: str, max_items: int = 35) -> List[UIElementInfo]:
        return []


# ======================================================================
# Linux / X11 Backend (python-xlib — enables full Xvfb silent testing!)
# ======================================================================
class _LinuxX11Backend:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    def _open_display(self) -> Any:
        from Xlib import display

        return display.Display()

    def _get_win_title(self, d: Any, w: Any) -> str:
        from Xlib import Xatom

        for prop_name in ("_NET_WM_NAME", "WM_NAME"):
            try:
                atom = d.intern_atom(prop_name)
                prop = w.get_full_property(atom, 0)
                if prop and prop.value:
                    val = prop.value
                    if isinstance(val, bytes):
                        return val.decode("utf-8", errors="replace").strip("\x00").strip()
                    return str(val).strip("\x00").strip()
            except Exception:
                continue
        return ""

    def _get_win_pid(self, d: Any, w: Any) -> int:
        try:
            atom = d.intern_atom("_NET_WM_PID")
            prop = w.get_full_property(atom, 0)
            if prop and prop.value:
                return int(prop.value[0])
        except Exception:
            pass
        return 0

    def _get_win_class(self, w: Any) -> str:
        try:
            cls = w.get_wm_class()
            if cls:
                return str(cls[-1])
        except Exception:
            pass
        return ""

    def _abs_geometry(self, root: Any, w: Any) -> Optional[Tuple[int, int, int, int, bool]]:
        from Xlib import X

        try:
            attrs = w.get_attributes()
            if attrs.map_state != X.IsViewable:
                return None
            geom = w.get_geometry()
            coords = root.translate_coords(w, 0, 0)
            # Note: Xlib root.translate_coords(src_win, 0, 0) translates (0,0) in src_win to root!
            return int(coords.x), int(coords.y), int(geom.width), int(geom.height), True
        except Exception:
            return None

    def list_windows(self) -> List[WindowInfo]:
        with self._lock:
            d = self._open_display()
            try:
                root = d.screen().root
                results: List[WindowInfo] = []
                seen: set = set()

                def _walk(parent: Any, depth: int = 0) -> None:
                    if depth > 4:
                        return
                    try:
                        children = parent.query_tree().children
                    except Exception:
                        return
                    # Iterate top-most last -> reverse for front-to-back order
                    for ch in reversed(children):
                        wid = int(ch.id)
                        if wid in seen:
                            continue
                        title = self._get_win_title(d, ch)
                        g = self._abs_geometry(root, ch)
                        if title and g and g[2] >= 60 and g[3] >= 50:
                            seen.add(wid)
                            pid = self._get_win_pid(d, ch)
                            app = self._get_win_class(ch)
                            results.append(
                                WindowInfo(
                                    window_id=str(wid),
                                    pid=pid,
                                    app=app,
                                    title=title,
                                    left=g[0],
                                    top=g[1],
                                    width=g[2],
                                    height=g[3],
                                    is_foreground=(len(results) == 0),
                                    is_minimized=False,
                                )
                            )
                        else:
                            _walk(ch, depth + 1)

                _walk(root, 0)
                return results
            finally:
                d.close()

    def get_window(self, window_id: str) -> Optional[WindowInfo]:
        for w in self.list_windows():
            if w.window_id == str(window_id):
                return w
        return None

    def set_visibility(self, window_id: str, watch: bool) -> bool:
        from Xlib import X

        with self._lock:
            d = self._open_display()
            try:
                w = d.create_resource_object("window", int(window_id))
                if watch:
                    w.configure(stack_mode=X.Above)
                    w.set_input_focus(X.RevertToParent, X.CurrentTime)
                else:
                    w.configure(stack_mode=X.Below)
                d.sync()
                return True
            except Exception:
                return False
            finally:
                d.close()

    def capture_window(self, window_id: str) -> Optional[Image.Image]:
        from Xlib import X

        with self._lock:
            d = self._open_display()
            try:
                w = d.create_resource_object("window", int(window_id))
                geom = w.get_geometry()
                width, height = int(geom.width), int(geom.height)
                if width <= 2 or height <= 2:
                    return None
                raw = w.get_image(0, 0, width, height, X.ZPixmap, 0xFFFFFFFF)
                data = raw.data
                if isinstance(data, str):
                    data = data.encode("latin1")
                return Image.frombytes("RGB", (width, height), data, "raw", "BGRX")
            except Exception:
                return None
            finally:
                d.close()

    def _send_button(
        self, window_id: str, norm_x: float, norm_y: float, button_num: int, clicks: int = 1
    ) -> Dict[str, Any]:
        from Xlib import X
        from Xlib.protocol import event

        with self._lock:
            d = self._open_display()
            try:
                root = d.screen().root
                w = d.create_resource_object("window", int(window_id))
                geom = w.get_geometry()
                width, height = max(1, int(geom.width)), max(1, int(geom.height))
                nx = max(0.0, min(1000.0, float(norm_x)))
                ny = max(0.0, min(1000.0, float(0.0 if norm_y is None else norm_y)))
                cx = int(round((nx / 1000.0) * max(1, width - 1)))
                cy = int(round((ny / 1000.0) * max(1, height - 1)))
                abs_pt = root.translate_coords(w, cx, cy)
                rx, ry = int(abs_pt.x), int(abs_pt.y)

                # Find deepest subwindow at (cx, cy)
                target_w = w
                tx, ty = cx, cy
                for _ in range(8):
                    tr = target_w.translate_coords(w, cx, cy)
                    if not getattr(tr, "child", 0):
                        break
                    child_w = tr.child
                    c_tr = child_w.translate_coords(w, cx, cy)
                    target_w = child_w
                    tx, ty = int(c_tr.x), int(c_tr.y)

                motion = event.MotionNotify(
                    time=X.CurrentTime,
                    root=root,
                    window=target_w,
                    same_screen=1,
                    child=X.NONE,
                    root_x=rx,
                    root_y=ry,
                    event_x=tx,
                    event_y=ty,
                    state=0,
                    detail=0,
                )
                target_w.send_event(motion, propagate=True)

                for _ in range(max(1, min(3, int(clicks)))):
                    press = event.ButtonPress(
                        time=X.CurrentTime,
                        root=root,
                        window=target_w,
                        same_screen=1,
                        child=X.NONE,
                        root_x=rx,
                        root_y=ry,
                        event_x=tx,
                        event_y=ty,
                        state=0,
                        detail=button_num,
                    )
                    release = event.ButtonRelease(
                        time=X.CurrentTime,
                        root=root,
                        window=target_w,
                        same_screen=1,
                        child=X.NONE,
                        root_x=rx,
                        root_y=ry,
                        event_x=tx,
                        event_y=ty,
                        state=1 << (7 + min(5, button_num)),
                        detail=button_num,
                    )
                    target_w.send_event(press, propagate=True)
                    d.flush()
                    time.sleep(0.01)
                    target_w.send_event(release, propagate=True)
                    d.flush()

                d.sync()
                return {
                    "ok": True,
                    "mode": "silent_x11_xsendevent",
                    "client_x": tx,
                    "client_y": ty,
                    "screen_x": rx,
                    "screen_y": ry,
                }
            finally:
                d.close()

    def silent_click(
        self, window_id: str, norm_x: float, norm_y: float, button: str = "left", clicks: int = 1
    ) -> Dict[str, Any]:
        btn_map = {"left": 1, "middle": 2, "right": 3}
        b = btn_map.get(button.lower().strip(), 1)
        return self._send_button(window_id, norm_x, norm_y, b, clicks=clicks)

    def silent_move(self, window_id: str, norm_x: float, norm_y: float) -> Dict[str, Any]:
        from Xlib import X
        from Xlib.protocol import event

        with self._lock:
            d = self._open_display()
            try:
                root = d.screen().root
                w = d.create_resource_object("window", int(window_id))
                geom = w.get_geometry()
                cx = int(round((max(0.0, min(1000.0, float(norm_x))) / 1000.0) * max(1, geom.width - 1)))
                cy = int(round((max(0.0, min(1000.0, float(norm_y))) / 1000.0) * max(1, geom.height - 1)))
                abs_pt = root.translate_coords(w, cx, cy)
                ev = event.MotionNotify(
                    time=X.CurrentTime,
                    root=root,
                    window=w,
                    same_screen=1,
                    child=X.NONE,
                    root_x=int(abs_pt.x),
                    root_y=int(abs_pt.y),
                    event_x=cx,
                    event_y=cy,
                    state=0,
                    detail=0,
                )
                w.send_event(ev, propagate=True)
                d.sync()
                return {"ok": True, "mode": "silent_x11_xsendevent", "client_x": cx, "client_y": cy}
            finally:
                d.close()

    def silent_scroll(
        self, window_id: str, direction: str, ticks: int, norm_x: float = 500.0, norm_y: float = 500.0
    ) -> Dict[str, Any]:
        btn_map = {"up": 4, "down": 5, "left": 6, "right": 7}
        b = btn_map.get(direction.lower().strip(), 5)
        for _ in range(max(1, min(20, int(ticks)))):
            self._send_button(window_id, norm_x, norm_y, b, clicks=1)
        return {"ok": True, "mode": "silent_x11_xsendevent", "direction": direction, "ticks": ticks}

    def silent_drag(
        self, window_id: str, sx_norm: float, sy_norm: float, ex_norm: float, ey_norm: float
    ) -> Dict[str, Any]:
        self._send_button(window_id, sx_norm, sy_norm, 1, clicks=1)
        self.silent_move(window_id, ex_norm, ey_norm)
        return {"ok": True, "mode": "silent_x11_xsendevent"}

    def _keysym_for_name(self, key_name: str) -> int:
        from Xlib import XK

        aliases = {
            "enter": "Return",
            "return": "Return",
            "esc": "Escape",
            "escape": "Escape",
            "backspace": "BackSpace",
            "tab": "Tab",
            "space": "space",
            "delete": "Delete",
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "home": "Home",
            "end": "End",
            "pageup": "Prior",
            "pagedown": "Next",
        }
        sym_name = aliases.get(key_name.lower(), key_name)
        ks = XK.string_to_keysym(sym_name)
        if ks == 0 and len(key_name) == 1:
            ks = ord(key_name)
        return ks

    def silent_key(
        self, window_id: str, keys: str, norm_x: float = 500.0, norm_y: float = 500.0
    ) -> Dict[str, Any]:
        from Xlib import X
        from Xlib.protocol import event

        parts = [p.strip() for p in keys.replace("-", "+").split("+") if p.strip()]
        if not parts:
            return {"ok": False, "error": "Empty key"}

        with self._lock:
            d = self._open_display()
            try:
                root = d.screen().root
                w = d.create_resource_object("window", int(window_id))
                mod_mask = 0
                for m in parts[:-1]:
                    ml = m.lower()
                    if ml in ("shift",):
                        mod_mask |= X.ShiftMask
                    elif ml in ("ctrl", "control"):
                        mod_mask |= X.ControlMask
                    elif ml in ("alt", "option"):
                        mod_mask |= X.Mod1Mask
                    elif ml in ("win", "super", "cmd", "command"):
                        mod_mask |= X.Mod4Mask

                ks = self._keysym_for_name(parts[-1])
                kc = d.keysym_to_keycode(ks) if ks else 0
                if kc:
                    kp = event.KeyPress(
                        time=X.CurrentTime,
                        root=root,
                        window=w,
                        same_screen=1,
                        child=X.NONE,
                        root_x=1,
                        root_y=1,
                        event_x=1,
                        event_y=1,
                        state=mod_mask,
                        detail=kc,
                    )
                    kr = event.KeyRelease(
                        time=X.CurrentTime,
                        root=root,
                        window=w,
                        same_screen=1,
                        child=X.NONE,
                        root_x=1,
                        root_y=1,
                        event_x=1,
                        event_y=1,
                        state=mod_mask,
                        detail=kc,
                    )
                    w.send_event(kp, propagate=True)
                    w.send_event(kr, propagate=True)
                    d.sync()
                return {"ok": True, "mode": "silent_x11_xsendevent", "keys": "+".join(parts)}
            finally:
                d.close()

    def silent_type(
        self,
        window_id: str,
        text: str,
        norm_x: Optional[float] = None,
        norm_y: Optional[float] = None,
        clear_first: bool = False,
        press_enter: bool = False,
        fallback_cursor_norm: Tuple[int, int] = (500, 500),
    ) -> Dict[str, Any]:
        if norm_x is not None and norm_y is not None:
            self.silent_click(window_id, norm_x, norm_y, "left", 1)
            time.sleep(0.02)
        if clear_first:
            self.silent_key(window_id, "ctrl+a")
            self.silent_key(window_id, "backspace")
        for ch in text:
            if ch == "\n":
                self.silent_key(window_id, "enter")
            elif ch == " ":
                self.silent_key(window_id, "space")
            elif ch.isupper():
                self.silent_key(window_id, f"shift+{ch.lower()}")
            else:
                self.silent_key(window_id, ch)
        if press_enter:
            self.silent_key(window_id, "enter")
        return {"ok": True, "mode": "silent_x11_xsendevent", "typed_length": len(text)}

    def get_elements(self, window_id: str, max_items: int = 35) -> List[UIElementInfo]:
        return []


# ======================================================================
# Unified Cross-Platform WindowManager Facade
# ======================================================================
class WindowManager:
    """High-level cross-platform silent window session manager (OS Native + Chromium CDP)."""

    def __init__(self) -> None:
        self._backend: Any = None
        self._cdp_sessions: Dict[str, Any] = {}
        self._cdp_pids: Dict[str, int] = {}
        self._cdp_port: Optional[int] = None
        if sys.platform == "win32":
            self._backend = _WindowsBackend()
        elif sys.platform == "darwin":
            try:
                self._backend = _MacOSBackend()
            except Exception:
                self._backend = None
        elif sys.platform.startswith("linux"):
            self._backend = _LinuxX11Backend()

    @property
    def supported(self) -> bool:
        return self._backend is not None

    def _get_cdp(self, window_id: str) -> Optional[Any]:
        return self._cdp_sessions.get(str(window_id))

    @staticmethod
    def _find_cdp_browser_pid(cdp_port: int) -> int:
        if sys.platform == "win32" or cdp_port <= 0:
            return 0
        needle = f"--remote-debugging-port={cdp_port}"
        try:
            out = subprocess.check_output(
                ["ps", "-ax", "-o", "pid=,command="],
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).decode("utf-8", errors="replace")
            for line in out.splitlines():
                if needle in line and "--type=" not in line and "grep" not in line:
                    parts = line.strip().split(None, 1)
                    if parts and parts[0].isdigit():
                        return int(parts[0])
        except Exception:
            pass
        return 0

    def get_hud_metadata(self, window_id: Optional[str]) -> Dict[str, Any]:
        wid = str(window_id or "").strip()
        meta: Dict[str, Any] = {
            "pid": 0,
            "window_id_int": int(wid) if wid.isdigit() else 0,
            "app": "",
            "bounds": None,
            "inset_top": 0.0,
            "content_size": None,
        }
        if not wid:
            return meta

        win = self._backend.get_window(wid) if (self._backend and not wid.startswith("cdp-")) else None
        if win is not None:
            meta["pid"] = int(win.pid)
            meta["app"] = win.app or win.title or "应用窗口"
            if win.width > 20 and win.height > 20:
                meta["bounds"] = [float(win.left), float(win.top), float(win.width), float(win.height)]

        if meta["pid"] <= 0 and wid in self._cdp_pids:
            meta["pid"] = int(self._cdp_pids[wid])

        cdp = self._get_cdp(wid)
        if cdp is not None:
            if not meta["app"]:
                meta["app"] = "Chrome"
            m = cdp.get_window_screen_metrics()
            if m is not None:
                meta["bounds"] = [m["sx"], m["sy"], m["ow"], m["oh"]]
                meta["inset_top"] = m["inset_top"]
                meta["content_size"] = [m["iw"], m["ih"]]
        return meta

    def check_global_stop_hotkey(self) -> bool:
        if self._backend and hasattr(self._backend, "check_global_stop_hotkey"):
            return bool(self._backend.check_global_stop_hotkey())
        return False

    def list_windows(self) -> List[WindowInfo]:
        wins: List[WindowInfo] = []
        if self._backend:
            try:
                wins = self._backend.list_windows()
            except Exception:
                wins = []
        existing_ids = {w.window_id for w in wins}
        for wid, cdp in list(self._cdp_sessions.items()):
            if wid.startswith("cdp-") and wid not in existing_ids:
                vw, vh = cdp.viewport_size
                wins.insert(
                    0,
                    WindowInfo(
                        window_id=wid,
                        pid=self._cdp_pids.get(wid, 0),
                        app="Chrome",
                        title=f"后台静默浏览器 ({vw}x{vh})",
                        left=0,
                        top=0,
                        width=vw,
                        height=vh,
                        is_foreground=False,
                        is_minimized=False,
                    ),
                )
        return wins

    def get_window(self, window_id: str) -> Optional[WindowInfo]:
        wid = str(window_id or "")
        if not wid:
            return None
        cdp = self._get_cdp(wid)
        if wid.startswith("cdp-") and cdp is not None:
            vw, vh = cdp.viewport_size
            return WindowInfo(
                window_id=wid,
                pid=self._cdp_pids.get(wid, 0),
                app="Chrome",
                title=f"后台静默浏览器 ({vw}x{vh})",
                left=0,
                top=0,
                width=vw,
                height=vh,
                is_foreground=False,
                is_minimized=False,
            )
        if not self._backend:
            return None
        try:
            win = self._backend.get_window(wid)
            if win and cdp is not None:
                vw, vh = cdp.viewport_size
                win.width = vw
                win.height = vh
            return win
        except Exception:
            return None

    def find_window_by_query(self, query: str) -> Optional[WindowInfo]:
        q = query.strip().lower()
        if not q:
            return None
        for w in self.list_windows():
            if (
                w.window_id == query.strip()
                or q in w.title.lower()
                or q in w.app.lower()
            ):
                return w
        return None

    def capture_window(self, window_id: str) -> Optional[Image.Image]:
        wid = str(window_id or "")
        if not wid:
            return None
        cdp = self._get_cdp(wid)
        if cdp is not None:
            try:
                img = cdp.capture_screenshot()
                if img is not None:
                    return img
            except Exception:
                pass
        if not self._backend or wid.startswith("cdp-"):
            return None
        return self._backend.capture_window(wid)

    def set_visibility(self, window_id: str, watch: bool) -> bool:
        wid = str(window_id or "")
        if not wid:
            return False
        cdp = self._get_cdp(wid)
        if cdp is not None and watch:
            try:
                cdp.call("Page.bringToFront")
            except Exception:
                pass
        if not self._backend or wid.startswith("cdp-"):
            return cdp is not None
        return bool(self._backend.set_visibility(wid, watch=watch))

    def get_elements(self, window_id: str, max_items: int = 35) -> List[UIElementInfo]:
        wid = str(window_id or "")
        if not wid:
            return []
        cdp = self._get_cdp(wid)
        if cdp is not None:
            try:
                dom_elems = cdp.get_interactive_elements(max_items=max_items)
                if dom_elems:
                    return [
                        UIElementInfo(
                            index=int(e["index"]),
                            role=str(e["role"]),
                            name=str(e["name"]),
                            norm_x=int(e["x"]),
                            norm_y=int(e["y"]),
                            width=int(e["width"]),
                            height=int(e["height"]),
                        )
                        for e in dom_elems
                    ]
            except Exception:
                pass
        if not self._backend or wid.startswith("cdp-"):
            return []
        try:
            return self._backend.get_elements(wid, max_items=max_items)
        except Exception:
            return []

    def silent_click(
        self, window_id: str, x: float, y: float, button: str = "left", clicks: int = 1
    ) -> Dict[str, Any]:
        wid = str(window_id)
        cdp = self._get_cdp(wid)
        if cdp is not None:
            try:
                return cdp.click(x, y, button=button, clicks=clicks)
            except Exception:
                pass
        return self._backend.silent_click(wid, x, y, button=button, clicks=clicks)

    def silent_move(self, window_id: str, x: float, y: float) -> Dict[str, Any]:
        wid = str(window_id)
        cdp = self._get_cdp(wid)
        if cdp is not None:
            try:
                return cdp.move(x, y)
            except Exception:
                pass
        return self._backend.silent_move(wid, x, y)

    def silent_scroll(
        self, window_id: str, direction: str, ticks: int, x: float = 500.0, y: float = 500.0
    ) -> Dict[str, Any]:
        wid = str(window_id)
        cdp = self._get_cdp(wid)
        if cdp is not None:
            try:
                return cdp.scroll(direction, ticks, norm_x=x, norm_y=y)
            except Exception:
                pass
        return self._backend.silent_scroll(wid, direction, ticks, norm_x=x, norm_y=y)

    def silent_drag(
        self, window_id: str, start_x: float, start_y: float, end_x: float, end_y: float
    ) -> Dict[str, Any]:
        wid = str(window_id)
        cdp = self._get_cdp(wid)
        if cdp is not None:
            try:
                return cdp.drag(start_x, start_y, end_x, end_y)
            except Exception:
                pass
        return self._backend.silent_drag(wid, start_x, start_y, end_x, end_y)

    def silent_key(
        self, window_id: str, keys: str, x: float = 500.0, y: float = 500.0
    ) -> Dict[str, Any]:
        wid = str(window_id)
        cdp = self._get_cdp(wid)
        if cdp is not None:
            try:
                return cdp.press_key(keys)
            except Exception:
                pass
        return self._backend.silent_key(wid, keys, norm_x=x, norm_y=y)

    def silent_type(
        self,
        window_id: str,
        text: str,
        x: Optional[float] = None,
        y: Optional[float] = None,
        clear_first: bool = False,
        press_enter: bool = False,
        fallback_cursor_norm: Tuple[int, int] = (500, 500),
    ) -> Dict[str, Any]:
        wid = str(window_id)
        cdp = self._get_cdp(wid)
        if cdp is not None:
            try:
                return cdp.type_text(
                    text=text,
                    norm_x=x,
                    norm_y=y,
                    clear_first=clear_first,
                    press_enter=press_enter,
                )
            except Exception:
                pass
        return self._backend.silent_type(
            wid,
            text,
            norm_x=x,
            norm_y=y,
            clear_first=clear_first,
            press_enter=press_enter,
            fallback_cursor_norm=fallback_cursor_norm,
        )

    def detect_auth_gate(self, window_id: Optional[str]) -> Dict[str, Any]:
        if not window_id:
            return {"needed": False, "kind": "", "title": "", "reason": "", "url": ""}
        cdp = self._get_cdp(str(window_id))
        if cdp is not None:
            return cdp.detect_auth_gate()
        return {"needed": False, "kind": "", "title": "", "reason": "", "url": ""}

    @staticmethod
    def _find_browser_binary() -> Optional[str]:
        if sys.platform == "win32":
            candidates = [
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            ]
            for c in candidates:
                if os.path.isfile(c):
                    return c
        elif sys.platform == "darwin":
            for app in (
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            ):
                if os.path.isfile(app):
                    return app
        else:
            for b in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "firefox"):
                found = shutil.which(b)
                if found:
                    return found
        return None

    def launch_silent_window(
        self, target: str, watch: bool = False, wait_timeout: float = 4.0
    ) -> Dict[str, Any]:
        """Launch a URL (with zero-dependency CDP kernel control on Chrome/Edge) or desktop app
        into its own window WITHOUT stealing focus, then bind and return its `window_id`.
        """
        from core.cdp_browser import CDPSilentSession

        cleaned = target.strip()
        if not cleaned:
            return {"ok": False, "error": "Target is empty"}

        before_ids = {w.window_id for w in self.list_windows()}
        spawned_pid: Optional[int] = None
        is_url = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", cleaned)) or (
            "." in cleaned and " " not in cleaned and not os.path.exists(cleaned) and "/" not in cleaned[:3]
        )
        if is_url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", cleaned):
            cleaned = "https://" + cleaned

        # Fast reuse if an active CDP silent browser window is already alive
        if is_url and self._cdp_sessions:
            for existing_wid, existing_cdp in list(self._cdp_sessions.items()):
                try:
                    existing_cdp.navigate(cleaned)
                    win = self.get_window(existing_wid)
                    if win is not None:
                        if not watch:
                            self.set_visibility(existing_wid, watch=False)
                        return {
                            "ok": True,
                            "target": cleaned,
                            "watch": watch,
                            "cdp_attached": True,
                            "window": win.to_dict(),
                            "window_id": existing_wid,
                        }
                except Exception:
                    self._cdp_sessions.pop(existing_wid, None)

        profile_dir = Path.home() / ".gemini" / "agy-computer-use" / "silent-browser-profile"
        cdp_port: Optional[int] = None
        chromium_flags: List[str] = []

        if is_url:
            browser_bin = self._find_browser_binary()
            if browser_bin and "firefox" not in browser_bin.lower():
                cdp_port = self._cdp_port or CDPSilentSession.allocate_free_port()
                self._cdp_port = cdp_port
                chromium_flags = [
                    f"--user-data-dir={profile_dir}",
                    f"--remote-debugging-port={cdp_port}",
                    "--remote-allow-origins=*",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows",
                    "--window-size=1280,840",
                    "--new-window",
                    cleaned,
                ]
        else:
            browser_bin = None

        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 1 if watch else 4  # SW_SHOWNOACTIVATE = 4

            if is_url:
                if browser_bin and chromium_flags:
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    proc = subprocess.Popen([browser_bin, *chromium_flags], startupinfo=si)
                    spawned_pid = proc.pid
                else:
                    os.startfile(cleaned)  # type: ignore[attr-defined]
            else:
                try:
                    proc = subprocess.Popen(cleaned, startupinfo=si, shell=False)
                    spawned_pid = proc.pid
                except Exception:
                    subprocess.Popen(["cmd", "/c", "start", "", cleaned], startupinfo=si)

        elif sys.platform == "darwin":
            if is_url:
                if browser_bin and chromium_flags and "Google Chrome" in browser_bin:
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    cmd = ["open"] + ([] if watch else ["-g"]) + ["-na", "Google Chrome", "--args", *chromium_flags]
                    subprocess.Popen(cmd)
                elif browser_bin and chromium_flags and "Microsoft Edge" in browser_bin:
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    cmd = ["open"] + ([] if watch else ["-g"]) + ["-na", "Microsoft Edge", "--args", *chromium_flags]
                    subprocess.Popen(cmd)
                else:
                    cmd = ["open"] + ([] if watch else ["-g"]) + [cleaned]
                    subprocess.Popen(cmd)
            else:
                cmd = ["open"] + ([] if watch else ["-g"])
                if os.path.exists(cleaned):
                    cmd.append(cleaned)
                else:
                    cmd.extend(["-a", cleaned])
                subprocess.Popen(cmd)

        else:
            if is_url:
                if browser_bin and chromium_flags:
                    profile_dir.mkdir(parents=True, exist_ok=True)
                    proc = subprocess.Popen(
                        [browser_bin, *chromium_flags],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    spawned_pid = proc.pid
                else:
                    subprocess.Popen(["xdg-open", cleaned])
            else:
                proc = subprocess.Popen(
                    cleaned if isinstance(cleaned, list) else cleaned.split(),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                spawned_pid = proc.pid

        if cdp_port is not None and not spawned_pid:
            for _ in range(8):
                spawned_pid = self._find_cdp_browser_pid(cdp_port) or None
                if spawned_pid:
                    break
                time.sleep(0.1)

        detected: Optional[WindowInfo] = None
        deadline = time.time() + max(0.3, wait_timeout)
        while time.time() < deadline:
            time.sleep(0.15)
            if cdp_port is not None and not spawned_pid:
                spawned_pid = self._find_cdp_browser_pid(cdp_port) or None
            wins = self.list_windows()
            for w in wins:
                if spawned_pid and w.pid == spawned_pid:
                    detected = w
                    break
                if w.window_id not in before_ids:
                    detected = w
                    break
            if detected is not None:
                break

        cdp_attached = False
        if cdp_port is not None:
            cdp_sess = CDPSilentSession(cdp_port)
            if cdp_sess.wait_until_ready(timeout=3.0):
                cdp_attached = True
                if not spawned_pid:
                    spawned_pid = self._find_cdp_browser_pid(cdp_port) or None
                wid_key = detected.window_id if detected is not None else f"cdp-{cdp_port}"
                self._cdp_sessions[wid_key] = cdp_sess
                if spawned_pid:
                    self._cdp_pids[wid_key] = int(spawned_pid)
                if detected is None:
                    detected = self.get_window(wid_key)

        if detected is not None and not watch:
            self.set_visibility(detected.window_id, watch=False)

        return {
            "ok": True,
            "target": cleaned,
            "watch": watch,
            "cdp_attached": cdp_attached,
            "window": detected.to_dict() if detected else None,
            "window_id": detected.window_id if detected else None,
        }

