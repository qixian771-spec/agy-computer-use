"""Cross-platform Desktop & Silent-Window Computer Use Engine v2.0 (Windows + macOS + Linux).

Core v2.0 upgrades:
1. Default `silent_window` mode: launches or locks onto a target application/browser window
   and executes clicks, scrolls, shortcuts, and Chinese/Unicode text IN THE BACKGROUND
   without moving the user's physical mouse or stealing active window focus.
2. High-speed turbo pipeline: sub-15ms occluded window capture (`PrintWindow` / `screencapture -l`
   / `Xlib`), fast bilinear scaling, adaptive JPEG encoding, zero artificial mouse glide lag,
   and `execute_batch()` multi-action bursts.
3. "Watch Live Anytime" toggle: bring the silent target window to the front (`watch=True`)
   or tuck it back behind active windows (`watch=False`) at any moment, plus virtual AI cursor
   overlay for live Picture-in-Picture streaming in the Cyber Console.
4. Safety guardrails: global `Ctrl+Alt+Q` hardware stop hotkey (Windows), Console `ESC` / Stop
   button, and 4-corner mouse slam failsafe when in `foreground_live` mode.
"""

from __future__ import annotations

import base64
import ctypes
import io
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple


def _enable_windows_dpi_awareness() -> None:
    """Enable Per-Monitor V2 DPI awareness on Windows before importing GUI libs."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_windows_dpi_awareness()

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from core.hud_bridge import HUD  # noqa: E402
from core.window_manager import UIElementInfo, WindowInfo, WindowManager  # noqa: E402


@dataclass
class MonitorGeometry:
    index: int
    left: int
    top: int
    width: int
    height: int
    logical_left: float
    logical_top: float
    logical_width: float
    logical_height: float
    scale_x: float
    scale_y: float


@dataclass
class ScreenshotResult:
    jpeg_base64: str
    image_width: int
    image_height: int
    logical_width: int
    logical_height: int
    monitor_index: int
    cursor_norm_x: int
    cursor_norm_y: int
    timestamp: float
    execution_mode: str = "silent_window"
    window_id: Optional[str] = None
    window_title: str = ""
    auth_gate: Optional[Dict[str, Any]] = None


class EmergencyStopError(RuntimeError):
    """Raised when emergency stop, Ctrl+Alt+Q hotkey, or corner failsafe is triggered."""


class DesktopEngine:
    """Thread-safe cross-platform Computer Use execution engine (Silent Window + Foreground Live)."""

    def __init__(self, max_image_dim: int = 1280, jpeg_quality: int = 76) -> None:
        self.max_image_dim = max_image_dim
        self.jpeg_quality = jpeg_quality
        self.window_manager = WindowManager()

        # Default mode: silent window execution (zero mouse/focus stealing)
        self.execution_mode: str = "silent_window"  # "silent_window" | "foreground_live"
        self.bound_window_id: Optional[str] = None
        self.watching_window: bool = False
        self.virtual_cursor_norm: Tuple[int, int] = (500, 500)

        self._stop_event = threading.Event()
        self._hud_stop_until: float = 0.0
        self._lock = threading.RLock()
        self._pyautogui: Any = None
        self._mss_mod: Any = None
        self._history: Deque[Dict[str, Any]] = deque(maxlen=80)
        self._last_marker: Optional[Tuple[int, int, str, float]] = None
        self._cached_elements: List[UIElementInfo] = []

    def _emit_hud(
        self,
        text: str,
        *,
        phase: str = "work",
        kind: Optional[str] = None,
        badge: str = "AI",
        nx: Optional[float] = None,
        ny: Optional[float] = None,
        window_id: Optional[str] = None,
        done: bool = False,
    ) -> None:
        wid = str(window_id or self.bound_window_id or "").strip()
        meta = self.window_manager.get_hud_metadata(wid)
        self.watching_window = HUD.is_watching(self.watching_window)
        if kind is not None and nx is not None and ny is not None:
            HUD.notify_cursor(
                nx,
                ny,
                kind=kind,
                badge=badge,
                text=text,
                phase=phase,
                pid=meta["pid"],
                window_id=meta["window_id_int"],
                app=meta["app"],
                bounds=meta["bounds"],
                inset_top=meta["inset_top"],
                content_size=meta["content_size"],
            )
        else:
            HUD.notify_status(
                text,
                phase=phase,
                pid=meta["pid"],
                window_id=meta["window_id_int"],
                app=meta["app"],
                bounds=meta["bounds"],
                inset_top=meta["inset_top"],
                content_size=meta["content_size"],
                done=done,
            )

    # ------------------------------------------------------------------
    # Lazy GUI initialization & Emergency Stop
    # ------------------------------------------------------------------
    def _ensure_gui(self) -> Any:
        if self._pyautogui is None:
            import pyautogui  # Lazy import so MCP handshake stays instant

            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.01
            self._pyautogui = pyautogui
        if self._mss_mod is None:
            import mss

            self._mss_mod = mss
        return self._pyautogui

    def trigger_emergency_stop(self, reason: str = "Manual stop triggered") -> None:
        self._stop_event.set()
        self._record("emergency_stop", {"reason": reason})

    def reset_emergency_stop(self) -> None:
        self._stop_event.clear()
        self._hud_stop_until = 0.0
        HUD.clear_stop()

    @property
    def is_stopped(self) -> bool:
        return self._stop_event.is_set() or (time.time() < self._hud_stop_until)

    def check_not_stopped(self) -> None:
        if HUD.check_stop_clicked():
            self._hud_stop_until = time.time() + 3.0
            self._record("emergency_stop", {"reason": "HUD pill stop clicked"})
        if time.time() < self._hud_stop_until:
            raise EmergencyStopError("你已点击悬浮条「停止」，本次 AI 操作已立即中止。")
        if self.window_manager.check_global_stop_hotkey():
            self.trigger_emergency_stop("Global Ctrl+Alt+Q emergency hotkey pressed")
        if self._stop_event.is_set():
            raise EmergencyStopError(
                "已触发紧急停止（Emergency Stop）。如需继续操作，请先解除急停状态。"
            )

    def _record(self, action: str, detail: Dict[str, Any]) -> None:
        entry = {
            "time": round(time.time(), 3),
            "action": action,
            **detail,
        }
        self._history.append(entry)

    def get_recent_history(self, limit: int = 25) -> List[Dict[str, Any]]:
        items = list(self._history)
        return items[-limit:]

    # ------------------------------------------------------------------
    # Silent Window Binding, Mode Switching & Live Watch Controls
    # ------------------------------------------------------------------
    def set_execution_mode(self, mode: str) -> Dict[str, Any]:
        with self._lock:
            cleaned = mode.strip().lower()
            if cleaned not in ("silent_window", "foreground_live"):
                raise ValueError("execution_mode 仅支持 'silent_window' 或 'foreground_live'")
            self.execution_mode = cleaned
            self._record("set_mode", {"execution_mode": cleaned})
            return self.get_session_state()

    def list_windows(self) -> List[Dict[str, Any]]:
        return [w.to_dict() for w in self.window_manager.list_windows()]

    def bind_window(self, window_id_or_query: Optional[str]) -> Dict[str, Any]:
        with self._lock:
            if not window_id_or_query or str(window_id_or_query).strip() in ("", "none", "desktop", "0"):
                self.bound_window_id = None
                self.watching_window = False
                self._cached_elements = []
                return {"ok": True, "bound_window": None, **self.get_session_state()}

            q = str(window_id_or_query).strip()
            win = self.window_manager.get_window(q) or self.window_manager.find_window_by_query(q)
            if not win:
                return {"ok": False, "error": f"未找到匹配的窗口: {q}"}

            self.bound_window_id = win.window_id
            self.virtual_cursor_norm = (500, 500)
            if self.execution_mode == "silent_window" and not self.watching_window:
                self.window_manager.set_visibility(win.window_id, watch=False)
            self._emit_hud(f"正在后台操作 {win.app or win.title or '窗口'}", window_id=win.window_id)
            self._record("bind_window", {"window_id": win.window_id, "title": win.title})
            return {"ok": True, "bound_window": win.to_dict(), **self.get_session_state()}

    def unbind_window(self) -> Dict[str, Any]:
        return self.bind_window(None)

    def set_watch_window(self, watch: bool, window_id: Optional[str] = None) -> Dict[str, Any]:
        """Bring the silent window to the foreground so the user can watch directly (`watch=True`),
        or send it quietly back behind active windows (`watch=False`).
        """
        with self._lock:
            wid = str(window_id or self.bound_window_id or "").strip()
            if not wid:
                return {"ok": False, "error": "当前尚未绑定目标窗口，无法切换窗口前后台"}
            ok = self.window_manager.set_visibility(wid, watch=bool(watch))
            self.watching_window = bool(watch) and ok
            win = self.window_manager.get_window(wid)
            meta = self.window_manager.get_hud_metadata(wid)
            HUD.notify_watch(
                watching=bool(watch),
                text=f"{'正在前台展示' if watch else '已切回后台静默'} · {(win.app if win else '') or '目标窗口'}",
                pid=meta["pid"],
                window_id=meta["window_id_int"],
                app=meta["app"],
                bounds=meta["bounds"],
                inset_top=meta["inset_top"],
                content_size=meta["content_size"],
            )
            self._record("watch_window", {"window_id": wid, "watch": bool(watch), "ok": ok})
            return {
                "ok": ok,
                "watch": self.watching_window,
                "window_id": wid,
                "window": win.to_dict() if win else None,
            }

    def _resolve_active_silent_window(self, explicit_window_id: Optional[str] = None) -> Optional[WindowInfo]:
        wid = str(explicit_window_id).strip() if explicit_window_id else None
        if wid:
            return self.window_manager.get_window(wid)
        if self.execution_mode == "silent_window" and self.bound_window_id:
            win = self.window_manager.get_window(self.bound_window_id)
            if win is None:
                # Target window was closed; clear stale binding gracefully
                self.bound_window_id = None
                self.watching_window = False
            return win
        return None

    def get_window_elements(self, window_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            win = self._resolve_active_silent_window(window_id)
            if not win:
                return []
            elems = self.window_manager.get_elements(win.window_id)
            self._cached_elements = elems
            return [e.to_dict() for e in elems]

    def _coords_from_element_index(
        self, element_index: Optional[int], window_id: Optional[str] = None
    ) -> Optional[Tuple[float, float]]:
        if element_index is None:
            return None
        idx = int(element_index)
        if not self._cached_elements:
            self.get_window_elements(window_id=window_id)
        for e in self._cached_elements:
            if e.index == idx:
                return float(e.norm_x), float(e.norm_y)
        return None

    def get_session_state(self) -> Dict[str, Any]:
        win = (
            self.window_manager.get_window(self.bound_window_id)
            if self.bound_window_id
            else None
        )
        if self.bound_window_id and win is None:
            self.bound_window_id = None
            self.watching_window = False
        return {
            "execution_mode": self.execution_mode,
            "bound_window_id": self.bound_window_id,
            "bound_window": win.to_dict() if win else None,
            "watching_window": self.watching_window,
            "virtual_cursor": {
                "x": self.virtual_cursor_norm[0],
                "y": self.virtual_cursor_norm[1],
            },
        }

    # ------------------------------------------------------------------
    # Monitor & DPI Coordinate Calibration (0..1000 <-> Logical OS pixels)
    # ------------------------------------------------------------------
    def list_monitors(self) -> List[MonitorGeometry]:
        pag = self._ensure_gui()
        logical_primary_w, logical_primary_h = pag.size()

        with self._mss_mod.mss() as sct:
            raw_monitors = sct.monitors
            if len(raw_monitors) < 2:
                return [
                    MonitorGeometry(
                        index=1,
                        left=0,
                        top=0,
                        width=logical_primary_w,
                        height=logical_primary_h,
                        logical_left=0.0,
                        logical_top=0.0,
                        logical_width=float(logical_primary_w),
                        logical_height=float(logical_primary_h),
                        scale_x=1.0,
                        scale_y=1.0,
                    )
                ]

            mon1 = raw_monitors[1]
            ratio_x = mon1["width"] / float(logical_primary_w) if logical_primary_w else 1.0
            ratio_y = mon1["height"] / float(logical_primary_h) if logical_primary_h else 1.0

            result: List[MonitorGeometry] = []
            for idx, m in enumerate(raw_monitors[1:], start=1):
                result.append(
                    MonitorGeometry(
                        index=idx,
                        left=int(m["left"]),
                        top=int(m["top"]),
                        width=int(m["width"]),
                        height=int(m["height"]),
                        logical_left=m["left"] / ratio_x,
                        logical_top=m["top"] / ratio_y,
                        logical_width=m["width"] / ratio_x,
                        logical_height=m["height"] / ratio_y,
                        scale_x=ratio_x,
                        scale_y=ratio_y,
                    )
                )
            return result

    def get_monitor(self, monitor_index: int = 1) -> MonitorGeometry:
        monitors = self.list_monitors()
        for m in monitors:
            if m.index == monitor_index:
                return m
        return monitors[0]

    def norm_to_screen(
        self, norm_x: float, norm_y: float, monitor_index: int = 1
    ) -> Tuple[int, int]:
        """Convert normalized 0..1000 coordinates into OS logical click coordinates."""
        mon = self.get_monitor(monitor_index)
        nx = max(0.0, min(1000.0, float(norm_x)))
        ny = max(0.0, min(1000.0, float(norm_y)))

        raw_x = mon.logical_left + (nx / 1000.0) * max(1.0, mon.logical_width - 1.0)
        raw_y = mon.logical_top + (ny / 1000.0) * max(1.0, mon.logical_height - 1.0)
        sx = int(round(raw_x))
        sy = int(round(raw_y))

        left_edge = int(round(mon.logical_left))
        top_edge = int(round(mon.logical_top))
        right_edge = int(round(mon.logical_left + mon.logical_width - 1))
        bottom_edge = int(round(mon.logical_top + mon.logical_height - 1))

        near_x_corner = sx <= left_edge + 4 or sx >= right_edge - 4
        near_y_corner = sy <= top_edge + 4 or sy >= bottom_edge - 4
        if near_x_corner and near_y_corner:
            sx = max(left_edge + 5, min(right_edge - 5, sx))
            sy = max(top_edge + 5, min(bottom_edge - 5, sy))

        return sx, sy

    def screen_to_norm(
        self, screen_x: float, screen_y: float, monitor_index: int = 1
    ) -> Tuple[int, int]:
        mon = self.get_monitor(monitor_index)
        nx = ((screen_x - mon.logical_left) / max(1.0, mon.logical_width - 1.0)) * 1000.0
        ny = ((screen_y - mon.logical_top) / max(1.0, mon.logical_height - 1.0)) * 1000.0
        return int(round(max(0.0, min(1000.0, nx)))), int(round(max(0.0, min(1000.0, ny))))

    def get_cursor_norm(self, monitor_index: int = 1) -> Tuple[int, int]:
        if self._resolve_active_silent_window() is not None:
            return self.virtual_cursor_norm
        pag = self._ensure_gui()
        cx, cy = pag.position()
        return self.screen_to_norm(cx, cy, monitor_index=monitor_index)

    # ------------------------------------------------------------------
    # Screen / Occluded Window Capture + 0..1000 Cyber Coordinate Overlay
    # ------------------------------------------------------------------
    def _grab_raw_image(self, mon: MonitorGeometry) -> Image.Image:
        """Capture monitor image via mss with OS-native fallback."""
        try:
            with self._mss_mod.mss() as sct:
                target = {
                    "left": mon.left,
                    "top": mon.top,
                    "width": mon.width,
                    "height": mon.height,
                }
                shot = sct.grab(target)
                return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as mss_err:
            if sys.platform == "darwin":
                tmp_file = os.path.join(
                    tempfile.gettempdir(), f"agy_cu_shot_{os.getpid()}.jpg"
                )
                try:
                    subprocess.run(
                        ["/usr/sbin/screencapture", "-x", "-t", "jpg", tmp_file],
                        check=True,
                        timeout=5,
                    )
                    with Image.open(tmp_file) as im:
                        return im.convert("RGB")
                except Exception as sc_err:
                    HUD.notify_permission(
                        title="需要开启 macOS「屏幕录制」权限",
                        text="点击「打开系统隐私设置」勾选 Antigravity 后点「已开启，继续」",
                        pref_url="x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
                    )
                    raise RuntimeError(
                        "macOS 屏幕录制权限未开启（已在顶部灵动岛弹出快捷开启卡片，请勾选 Antigravity 后继续）。"
                    ) from sc_err
                finally:
                    if os.path.exists(tmp_file):
                        try:
                            os.remove(tmp_file)
                        except OSError:
                            pass
            elif sys.platform == "win32":
                from PIL import ImageGrab

                return ImageGrab.grab().convert("RGB")
            raise RuntimeError(
                f"截屏失败: {mss_err}。若在 macOS 上，请前往「系统设置 -> 隐私与安全性 -> 屏幕录制」勾选当前终端或 Antigravity。"
            ) from mss_err

    def _draw_coordinate_overlay(
        self,
        img: Image.Image,
        cursor_norm: Tuple[int, int],
        draw_grid: bool = True,
        draw_cursor: bool = True,
        is_silent_window: bool = False,
    ) -> Image.Image:
        w, h = img.size
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font = ImageFont.load_default()

        if draw_grid:
            grid_color = (0, 229, 255, 32)
            tick_color = (0, 229, 255, 130)
            pill_bg = (10, 15, 26, 175)
            pill_fg = (56, 239, 255, 245)

            for step in range(50, 1000, 50):
                px = int(round((step / 1000.0) * (w - 1)))
                py = int(round((step / 1000.0) * (h - 1)))
                is_major = step % 100 == 0
                tick_len = 9 if is_major else 5

                draw.line([(px, 0), (px, tick_len)], fill=tick_color, width=1)
                draw.line([(px, h - 1 - tick_len), (px, h - 1)], fill=tick_color, width=1)
                draw.line([(0, py), (tick_len, py)], fill=tick_color, width=1)
                draw.line([(w - 1 - tick_len, py), (w - 1, py)], fill=tick_color, width=1)

                if is_major:
                    draw.line([(px, tick_len), (px, h - 1 - tick_len)], fill=grid_color, width=1)
                    draw.line([(tick_len, py), (w - 1 - tick_len, py)], fill=grid_color, width=1)

                    label = str(step)
                    tw = len(label) * 6 + 6
                    tx = max(2, min(w - tw - 2, px - tw // 2))
                    draw.rectangle([tx, 10, tx + tw, 22], fill=pill_bg)
                    draw.text((tx + 3, 11), label, fill=pill_fg, font=font)

                    ty = max(2, min(h - 14, py - 6))
                    draw.rectangle([10, ty, 10 + tw, ty + 12], fill=pill_bg)
                    draw.text((13, ty + 1), label, fill=pill_fg, font=font)

        # Draw recent action ripple if fresh (< 8 seconds)
        if self._last_marker is not None:
            mx_norm, my_norm, _mlabel, mtime = self._last_marker
            if time.time() - mtime < 8.0:
                mx = int(round((mx_norm / 1000.0) * (w - 1)))
                my = int(round((my_norm / 1000.0) * (h - 1)))
                r = 16
                draw.ellipse(
                    [mx - r, my - r, mx + r, my + r],
                    outline=(16, 185, 129, 225),
                    width=2,
                )
                draw.ellipse([mx - 3, my - 3, mx + 3, my + 3], fill=(16, 185, 129, 240))

        if draw_cursor:
            cx = int(round((cursor_norm[0] / 1000.0) * (w - 1)))
            cy = int(round((cursor_norm[1] / 1000.0) * (h - 1)))
            ring = 11
            # Cyan/emerald AI virtual pointer in silent window mode; coral for physical cursor
            c_outline = (34, 211, 238, 245) if is_silent_window else (255, 77, 109, 235)
            c_badge_bg = (8, 24, 34, 210) if is_silent_window else (24, 10, 16, 195)
            c_badge_fg = (103, 232, 249, 255) if is_silent_window else (255, 130, 150, 255)

            draw.ellipse(
                [cx - ring, cy - ring, cx + ring, cy + ring],
                outline=c_outline,
                width=2,
            )
            draw.line([(cx - 16, cy), (cx + 16, cy)], fill=c_outline, width=1)
            draw.line([(cx, cy - 16), (cx, cy + 16)], fill=c_outline, width=1)
            prefix = "AI" if is_silent_window else ""
            badge = f"{prefix}({cursor_norm[0]},{cursor_norm[1]})"
            bw = len(badge) * 6 + 6
            bx = cx + 14 if cx + 14 + bw < w else max(2, cx - 14 - bw)
            by = cy + 10 if cy + 24 < h else max(2, cy - 22)
            draw.rectangle([bx, by, bx + bw, by + 13], fill=c_badge_bg)
            draw.text((bx + 3, by + 1), badge, fill=c_badge_fg, font=font)

        return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    def screenshot(
        self,
        monitor_index: int = 1,
        draw_grid: bool = True,
        draw_cursor: bool = True,
        window_id: Optional[str] = None,
        force_desktop: bool = False,
    ) -> ScreenshotResult:
        with self._lock:
            target_win = None if force_desktop else self._resolve_active_silent_window(window_id)
            raw_img: Optional[Image.Image] = None
            logical_w = 0
            logical_h = 0
            cursor_norm = (500, 500)
            is_silent_shot = False

            auth_gate: Optional[Dict[str, Any]] = None
            if target_win is not None:
                self._emit_hud(
                    f"正在读取画面 · {target_win.app or target_win.title[:18] or '后台窗口'}",
                    phase="scan",
                    window_id=target_win.window_id,
                )
                raw_img = self.window_manager.capture_window(target_win.window_id)
                if raw_img is not None:
                    logical_w = target_win.width
                    logical_h = target_win.height
                    cursor_norm = self.virtual_cursor_norm
                    is_silent_shot = True
                gate = self.window_manager.detect_auth_gate(target_win.window_id)
                if gate.get("needed"):
                    auth_gate = gate

            if raw_img is None:
                self._ensure_gui()
                mon = self.get_monitor(monitor_index)
                raw_img = self._grab_raw_image(mon)
                logical_w = int(round(mon.logical_width))
                logical_h = int(round(mon.logical_height))
                cursor_norm = self.get_cursor_norm(monitor_index=monitor_index)
                monitor_idx = mon.index
            else:
                monitor_idx = monitor_index

            # Fast proportional resize using BILINEAR (~4x faster than LANCZOS)
            orig_w, orig_h = raw_img.size
            max_side = max(orig_w, orig_h)
            if max_side > self.max_image_dim:
                scale = self.max_image_dim / float(max_side)
                new_w = max(1, int(round(orig_w * scale)))
                new_h = max(1, int(round(orig_h * scale)))
                raw_img = raw_img.resize((new_w, new_h), Image.Resampling.BILINEAR)

            if draw_grid or draw_cursor:
                final_img = self._draw_coordinate_overlay(
                    raw_img,
                    cursor_norm=cursor_norm,
                    draw_grid=draw_grid,
                    draw_cursor=draw_cursor,
                    is_silent_window=is_silent_shot,
                )
            else:
                final_img = raw_img

            buf = io.BytesIO()
            final_img.save(buf, format="JPEG", quality=self.jpeg_quality)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")

            return ScreenshotResult(
                jpeg_base64=b64,
                image_width=final_img.size[0],
                image_height=final_img.size[1],
                logical_width=logical_w,
                logical_height=logical_h,
                monitor_index=monitor_idx,
                cursor_norm_x=cursor_norm[0],
                cursor_norm_y=cursor_norm[1],
                timestamp=time.time(),
                execution_mode="silent_window" if is_silent_shot else "foreground_live",
                window_id=target_win.window_id if is_silent_shot and target_win else None,
                window_title=target_win.title if is_silent_shot and target_win else "",
                auth_gate=auth_gate,
            )

    # ------------------------------------------------------------------
    # Mouse, Keyboard, Clipboard, Silent Window & Batch Actions
    # ------------------------------------------------------------------
    def _wrap_failsafe(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self.check_not_stopped()
        pag = self._ensure_gui()
        try:
            return fn(*args, **kwargs)
        except pag.FailSafeException as exc:
            self.trigger_emergency_stop("Corner mouse slam failsafe")
            raise EmergencyStopError(
                "检测到鼠标甩入屏幕四角，已触发物理安全急停（FailSafe）！"
            ) from exc

    def move(
        self,
        x: float,
        y: float,
        monitor_index: int = 1,
        duration: float = 0.03,
        window_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self.check_not_stopped()
            nx, ny = int(round(max(0.0, min(1000.0, float(x))))), int(round(max(0.0, min(1000.0, float(y)))))
            target_win = self._resolve_active_silent_window(window_id)
            if target_win is not None:
                res = self.window_manager.silent_move(target_win.window_id, nx, ny)
                self.virtual_cursor_norm = (nx, ny)
                self._emit_hud(
                    f"正在操作 {target_win.app or '窗口'} · 移动光标",
                    kind="move",
                    badge="AI",
                    nx=nx,
                    ny=ny,
                    window_id=target_win.window_id,
                )
                self._record("silent_move", {"window_id": target_win.window_id, "x": nx, "y": ny})
                return {"ok": True, "action": "move", "silent": True, "window_id": target_win.window_id, "x": nx, "y": ny, **res}

            sx, sy = self.norm_to_screen(nx, ny, monitor_index=monitor_index)
            pag = self._ensure_gui()
            self._wrap_failsafe(pag.moveTo, sx, sy, duration=max(0.0, min(0.8, duration)))
            self._record("move", {"x": nx, "y": ny, "screen_x": sx, "screen_y": sy})
            return {"ok": True, "action": "move", "silent": False, "x": nx, "y": ny, "screen_x": sx, "screen_y": sy}

    def click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        button: str = "left",
        clicks: int = 1,
        monitor_index: int = 1,
        window_id: Optional[str] = None,
        element_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self.check_not_stopped()
            if element_index is not None and (x is None or y is None):
                resolved = self._coords_from_element_index(element_index, window_id=window_id)
                if resolved is None:
                    raise ValueError(f"未找到编号为 #{element_index} 的窗口控件，请先刷新窗口元素或传入 (x, y)")
                x, y = resolved

            if x is None or y is None:
                x, y = float(self.virtual_cursor_norm[0]), float(self.virtual_cursor_norm[1])

            btn = button.lower().strip()
            if btn not in ("left", "right", "middle"):
                btn = "left"
            num_clicks = max(1, min(3, int(clicks)))
            nx = int(round(max(0.0, min(1000.0, float(x)))))
            ny = int(round(max(0.0, min(1000.0, float(y)))))

            target_win = self._resolve_active_silent_window(window_id)
            if target_win is not None:
                res = self.window_manager.silent_click(
                    target_win.window_id, nx, ny, button=btn, clicks=num_clicks
                )
                self.virtual_cursor_norm = (nx, ny)
                self._last_marker = (nx, ny, f"{btn}x{num_clicks}", time.time())
                click_kind = "right_click" if btn == "right" else ("double_click" if num_clicks >= 2 else "click")
                click_label = "右键" if btn == "right" else ("双击" if num_clicks >= 2 else "点击")
                self._emit_hud(
                    f"正在操作 {target_win.app or '窗口'} · {click_label}",
                    kind=click_kind,
                    badge=f"AI · {click_label}",
                    nx=nx,
                    ny=ny,
                    window_id=target_win.window_id,
                )
                self._record(
                    "silent_click",
                    {"window_id": target_win.window_id, "x": nx, "y": ny, "button": btn, "clicks": num_clicks},
                )
                return {
                    "ok": True,
                    "action": "click",
                    "silent": True,
                    "window_id": target_win.window_id,
                    "x": nx,
                    "y": ny,
                    "button": btn,
                    "clicks": num_clicks,
                    **res,
                }

            sx, sy = self.norm_to_screen(nx, ny, monitor_index=monitor_index)
            pag = self._ensure_gui()
            self._wrap_failsafe(pag.moveTo, sx, sy, duration=0.03)
            self.check_not_stopped()
            self._wrap_failsafe(
                pag.click, x=sx, y=sy, clicks=num_clicks, interval=0.04, button=btn
            )
            self._last_marker = (nx, ny, f"{btn}x{num_clicks}", time.time())
            self._record(
                "click",
                {"x": nx, "y": ny, "button": btn, "clicks": num_clicks, "screen_x": sx, "screen_y": sy},
            )
            return {
                "ok": True,
                "action": "click",
                "silent": False,
                "x": nx,
                "y": ny,
                "button": btn,
                "clicks": num_clicks,
                "screen_x": sx,
                "screen_y": sy,
            }

    def drag(
        self,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        duration: float = 0.25,
        monitor_index: int = 1,
        window_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self.check_not_stopped()
            ex, ey = int(round(end_x)), int(round(end_y))
            target_win = self._resolve_active_silent_window(window_id)
            if target_win is not None:
                self.window_manager.silent_drag(
                    target_win.window_id, start_x, start_y, end_x, end_y
                )
                self.virtual_cursor_norm = (ex, ey)
                self._last_marker = (ex, ey, "drag", time.time())
                self._emit_hud(
                    f"正在操作 {target_win.app or '窗口'} · 拖拽元素",
                    kind="click",
                    badge="AI · 拖拽",
                    nx=ex,
                    ny=ey,
                    window_id=target_win.window_id,
                )
                self._record("silent_drag", {"window_id": target_win.window_id, "end_x": ex, "end_y": ey})
                return {"ok": True, "action": "drag", "silent": True, "window_id": target_win.window_id, "end_x": ex, "end_y": ey}

            sx1, sy1 = self.norm_to_screen(start_x, start_y, monitor_index=monitor_index)
            sx2, sy2 = self.norm_to_screen(end_x, end_y, monitor_index=monitor_index)
            pag = self._ensure_gui()
            self._wrap_failsafe(pag.moveTo, sx1, sy1, duration=0.04)
            self.check_not_stopped()
            self._wrap_failsafe(
                pag.dragTo, sx2, sy2, duration=max(0.1, min(2.0, duration)), button="left"
            )
            self._last_marker = (ex, ey, "drag", time.time())
            self._record(
                "drag",
                {"start_x": int(round(start_x)), "start_y": int(round(start_y)), "end_x": ex, "end_y": ey},
            )
            return {"ok": True, "action": "drag", "silent": False, "end_x": ex, "end_y": ey}

    def scroll(
        self,
        direction: str = "down",
        ticks: int = 5,
        x: Optional[float] = None,
        y: Optional[float] = None,
        monitor_index: int = 1,
        window_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self.check_not_stopped()
            t = max(1, min(30, int(ticks)))
            d = direction.lower().strip()
            target_win = self._resolve_active_silent_window(window_id)
            if target_win is not None:
                sx_norm = float(x) if x is not None else float(self.virtual_cursor_norm[0])
                sy_norm = float(y) if y is not None else float(self.virtual_cursor_norm[1])
                self.window_manager.silent_scroll(
                    target_win.window_id, d, t, x=sx_norm, y=sy_norm
                )
                self.virtual_cursor_norm = (int(round(sx_norm)), int(round(sy_norm)))
                dir_cn = {"down": "向下滚动", "up": "向上滚动", "left": "向左滚动", "right": "向右滚动"}.get(d, "滚动页面")
                self._emit_hud(
                    f"正在操作 {target_win.app or '窗口'} · {dir_cn}",
                    kind="scroll",
                    badge=f"AI · {dir_cn[:2]}",
                    nx=sx_norm,
                    ny=sy_norm,
                    window_id=target_win.window_id,
                )
                self._record("silent_scroll", {"window_id": target_win.window_id, "direction": d, "ticks": t})
                return {"ok": True, "action": "scroll", "silent": True, "window_id": target_win.window_id, "direction": d, "ticks": t}

            pag = self._ensure_gui()
            if x is not None and y is not None:
                sx, sy = self.norm_to_screen(x, y, monitor_index=monitor_index)
                self._wrap_failsafe(pag.moveTo, sx, sy, duration=0.03)

            unit = 120 if sys.platform == "win32" else 3
            amount = t * unit if d in ("up", "left") else -(t * unit)

            if d in ("left", "right") and hasattr(pag, "hscroll"):
                self._wrap_failsafe(pag.hscroll, amount if d == "right" else -amount)
            else:
                self._wrap_failsafe(pag.scroll, amount)

            self._record("scroll", {"direction": d, "ticks": t, "x": x, "y": y})
            return {"ok": True, "action": "scroll", "silent": False, "direction": d, "ticks": t}

    @staticmethod
    def _normalize_key_name(raw_key: str) -> str:
        k = raw_key.strip().lower()
        is_mac = sys.platform == "darwin"
        aliases = {
            "cmd": "command" if is_mac else "win",
            "command": "command" if is_mac else "win",
            "super": "command" if is_mac else "win",
            "meta": "command" if is_mac else "win",
            "win": "command" if is_mac else "win",
            "windows": "command" if is_mac else "win",
            "ctrl": "ctrl",
            "control": "ctrl",
            "opt": "option" if is_mac else "alt",
            "option": "option" if is_mac else "alt",
            "alt": "option" if is_mac else "alt",
            "esc": "esc",
            "escape": "esc",
            "return": "enter",
            "enter": "enter",
            "bksp": "backspace",
            "del": "delete",
            "pgup": "pageup",
            "pgdn": "pagedown",
            "spacebar": "space",
        }
        return aliases.get(k, k)

    def press_key(self, keys: str, window_id: Optional[str] = None) -> Dict[str, Any]:
        """Press a single key ('enter', 'esc') or combo ('cmd+space', 'ctrl+l')."""
        with self._lock:
            self.check_not_stopped()
            target_win = self._resolve_active_silent_window(window_id)
            if target_win is not None:
                res = self.window_manager.silent_key(
                    target_win.window_id,
                    keys,
                    x=float(self.virtual_cursor_norm[0]),
                    y=float(self.virtual_cursor_norm[1]),
                )
                self._emit_hud(
                    f"正在操作 {target_win.app or '窗口'} · 按键 {keys}",
                    window_id=target_win.window_id,
                )
                self._record("silent_key", {"window_id": target_win.window_id, "keys": keys})
                return {"ok": bool(res.get("ok", True)), "action": "key", "silent": True, "window_id": target_win.window_id, "keys": res.get("keys", keys)}

            pag = self._ensure_gui()
            parts = [
                self._normalize_key_name(p)
                for p in keys.replace("-", "+").split("+")
                if p.strip()
            ] if "+" in keys else [self._normalize_key_name(keys)]

            if not parts:
                return {"ok": False, "error": "Empty key specification"}

            try:
                if len(parts) == 1:
                    self._wrap_failsafe(pag.press, parts[0])
                else:
                    modifiers = parts[:-1]
                    final_key = parts[-1]
                    for mod in modifiers:
                        pag.keyDown(mod)
                        time.sleep(0.025)
                    try:
                        pag.press(final_key)
                        time.sleep(0.025)
                    finally:
                        for mod in reversed(modifiers):
                            pag.keyUp(mod)
            except pag.FailSafeException as exc:
                self.trigger_emergency_stop("Corner failsafe")
                raise EmergencyStopError("触发四角物理急停！") from exc

            self._record("key", {"keys": "+".join(parts)})
            return {"ok": True, "action": "key", "silent": False, "keys": "+".join(parts)}

    @staticmethod
    def _set_clipboard(text: str) -> Optional[str]:
        prev: Optional[str] = None
        try:
            import pyperclip

            try:
                prev = pyperclip.paste()
            except Exception:
                prev = None
            pyperclip.copy(text)
            return prev
        except Exception:
            if sys.platform == "darwin":
                proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                proc.communicate(text.encode("utf-8"))
                return None
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $input"],
                    stdin=subprocess.PIPE,
                )
                proc.communicate(text.encode("utf-8"))
                return None
            raise

    @staticmethod
    def _restore_clipboard(prev: Optional[str]) -> None:
        if prev is None:
            return
        try:
            import pyperclip

            pyperclip.copy(prev)
        except Exception:
            pass

    def type_text(
        self,
        text: str,
        x: Optional[float] = None,
        y: Optional[float] = None,
        clear_first: bool = False,
        press_enter: bool = False,
        monitor_index: int = 1,
        window_id: Optional[str] = None,
        element_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Type Unicode/Chinese/ASCII text silently into bound window or foreground."""
        with self._lock:
            self.check_not_stopped()
            if element_index is not None and (x is None or y is None):
                resolved = self._coords_from_element_index(element_index, window_id=window_id)
                if resolved is not None:
                    x, y = resolved

            target_win = self._resolve_active_silent_window(window_id)
            if target_win is not None:
                if x is not None and y is not None:
                    self.virtual_cursor_norm = (
                        int(round(max(0.0, min(1000.0, float(x))))),
                        int(round(max(0.0, min(1000.0, float(y))))),
                    )
                self.window_manager.silent_type(
                    target_win.window_id,
                    text=text,
                    x=x,
                    y=y,
                    clear_first=clear_first,
                    press_enter=press_enter,
                    fallback_cursor_norm=self.virtual_cursor_norm,
                )
                preview = text if len(text) <= 40 else text[:37] + "..."
                short_txt = text if len(text) <= 14 else text[:12] + "…"
                self._emit_hud(
                    f"正在操作 {target_win.app or '窗口'} · 输入「{short_txt}」",
                    kind="click" if (x is not None and y is not None) else "move",
                    badge="AI · 输入",
                    nx=float(self.virtual_cursor_norm[0]),
                    ny=float(self.virtual_cursor_norm[1]),
                    window_id=target_win.window_id,
                )
                self._record(
                    "silent_type",
                    {"window_id": target_win.window_id, "text": preview, "press_enter": press_enter},
                )
                return {
                    "ok": True,
                    "action": "type",
                    "silent": True,
                    "window_id": target_win.window_id,
                    "typed_length": len(text),
                    "press_enter": press_enter,
                }

            self._ensure_gui()
            if x is not None and y is not None:
                self.click(x, y, button="left", clicks=1, monitor_index=monitor_index)
                time.sleep(0.08)

            paste_mod = "command" if sys.platform == "darwin" else "ctrl"
            if clear_first:
                self.press_key(f"{paste_mod}+a")
                time.sleep(0.04)
                self.press_key("backspace")
                time.sleep(0.04)

            if text:
                try:
                    prev_clip = self._set_clipboard(text)
                    time.sleep(0.03)
                    self.press_key(f"{paste_mod}+v")
                    time.sleep(0.08)
                    self._restore_clipboard(prev_clip)
                except Exception:
                    if all(32 <= ord(ch) < 127 or ch in "\n\t" for ch in text):
                        self._wrap_failsafe(self._pyautogui.write, text, interval=0.01)
                    else:
                        raise RuntimeError(
                            "剪贴板不可用，无法输入中文文本。Windows/macOS 原生支持，Linux 请安装 xclip 或 xsel。"
                        )

            if press_enter:
                time.sleep(0.04)
                self.press_key("enter")

            preview = text if len(text) <= 40 else text[:37] + "..."
            self._record(
                "type",
                {"text": preview, "x": x, "y": y, "press_enter": press_enter},
            )
            return {
                "ok": True,
                "action": "type",
                "silent": False,
                "typed_length": len(text),
                "press_enter": press_enter,
            }

    def open_target(
        self, target: str, silent: Optional[bool] = None, watch: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Open a URL or desktop app.

        In `silent_window` mode (default), launches an independent background window
        WITHOUT stealing focus or hijacking the user's active browser tab, and automatically
        binds `bound_window_id` to it.
        """
        with self._lock:
            self.check_not_stopped()
            cleaned = target.strip()
            if not cleaned:
                return {"ok": False, "error": "Target is empty"}

            use_silent = (self.execution_mode == "silent_window") if silent is None else bool(silent)
            if use_silent:
                self.watching_window = HUD.is_watching(self.watching_window)
                want_watch = self.watching_window if watch is None else bool(watch)
                short_target = cleaned.replace("https://", "").replace("http://", "").split("/")[0][:28]
                HUD.notify_status(f"正在后台打开 {short_target}...", phase="open")
                res = self.window_manager.launch_silent_window(cleaned, watch=want_watch)
                auth_gate: Optional[Dict[str, Any]] = None
                if res.get("window_id"):
                    self.bound_window_id = str(res["window_id"])
                    self.virtual_cursor_norm = (500, 500)
                    gate = self.window_manager.detect_auth_gate(self.bound_window_id)
                    if gate.get("needed"):
                        auth_gate = gate
                        meta = self.window_manager.get_hud_metadata(self.bound_window_id)
                        HUD.notify_auth(
                            gate.get("title") or f"需要登录授权 · {short_target}",
                            gate.get("reason") or "请在弹出窗口完成扫码/登录，完成后自动切回后台",
                            pop_window=True,
                            pid=meta["pid"],
                            window_id=meta["window_id_int"],
                            app=meta["app"],
                            bounds=meta["bounds"],
                            inset_top=meta["inset_top"],
                            content_size=meta["content_size"],
                        )
                    else:
                        self._emit_hud(
                            f"正在后台操作 · {short_target}",
                            phase="open",
                            kind="move",
                            badge="AI",
                            nx=500,
                            ny=500,
                            window_id=self.bound_window_id,
                        )
                self._record(
                    "open_silent",
                    {"target": cleaned, "window_id": res.get("window_id"), "watch": want_watch},
                )
                return {
                    "ok": True,
                    "action": "open",
                    "silent": True,
                    "target": res.get("target", cleaned),
                    "window_id": res.get("window_id"),
                    "window": res.get("window"),
                    "auth_gate": auth_gate,
                }

            if cleaned.startswith(("http://", "https://")):
                webbrowser.open(cleaned)
            elif sys.platform == "darwin":
                if os.path.exists(cleaned):
                    subprocess.Popen(["open", cleaned])
                else:
                    subprocess.Popen(["open", "-a", cleaned])
            elif sys.platform == "win32":
                try:
                    os.startfile(cleaned)  # type: ignore[attr-defined]
                except Exception:
                    subprocess.Popen(["cmd", "/c", "start", "", cleaned], shell=False)
            else:
                subprocess.Popen(["xdg-open", cleaned])

            self._record("open", {"target": cleaned})
            return {"ok": True, "action": "open", "silent": False, "target": cleaned}

    def wait_for_user_auth(
        self,
        reason: str = "请在弹出的窗口完成扫码登录或安全验证，完成后点灵动岛「我已完成」继续",
        title: str = "需要你完成登录 / 授权",
        auto_pop_window: bool = True,
        timeout_sec: float = 180.0,
        window_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Trigger the MacBook Dynamic Island Login/Auth Takeover Card, pop out the target
        window so the user can scan QR / enter credentials / complete verification, and
        automatically tuck the window back to silent background once completed!
        """
        self.check_not_stopped()
        req_ts = time.time()
        with self._lock:
            wid = str(window_id or self.bound_window_id or "").strip()
            meta = self.window_manager.get_hud_metadata(wid)
            initial_gate = self.window_manager.detect_auth_gate(wid) if wid else {"needed": False}
            eff_title = title.strip() or initial_gate.get("title") or "需要你完成登录 / 授权"
            eff_reason = reason.strip() or initial_gate.get("reason") or "完成后点灵动岛「我已完成」自动切回后台继续"
            if wid and auto_pop_window and sys.platform != "darwin":
                self.window_manager.set_visibility(wid, watch=True)
            HUD.notify_auth(
                eff_title,
                eff_reason,
                pop_window=auto_pop_window,
                pid=meta["pid"],
                window_id=meta["window_id_int"],
                app=meta["app"],
                bounds=meta["bounds"],
                inset_top=meta["inset_top"],
                content_size=meta["content_size"],
            )
            self._record("wait_for_user_auth", {"window_id": wid, "title": eff_title, "reason": eff_reason})

        had_initial_gate = bool(initial_gate.get("needed"))
        clear_streak = 0
        last_probe_ts = time.time()
        deadline = req_ts + max(5.0, min(600.0, float(timeout_sec)))

        while time.time() < deadline:
            self.check_not_stopped()
            if HUD.check_auth_resolved(req_ts):
                if wid and sys.platform != "darwin":
                    self.window_manager.set_visibility(wid, watch=False)
                self.watching_window = False
                return {
                    "ok": True,
                    "resolved": True,
                    "resolved_by": "dynamic_island_button",
                    "window_id": wid or None,
                    "elapsed_sec": round(time.time() - req_ts, 1),
                }

            now = time.time()
            if wid and had_initial_gate and (now - last_probe_ts >= 1.25):
                last_probe_ts = now
                gate_now = self.window_manager.detect_auth_gate(wid)
                if not gate_now.get("needed"):
                    clear_streak += 1
                    if clear_streak >= 2:
                        HUD.notify_auth_done(
                            "✓ 检测到扫码 / 登录成功 · 已自动切回后台继续！",
                            pid=meta["pid"],
                            window_id=meta["window_id_int"],
                            app=meta["app"],
                        )
                        if sys.platform != "darwin":
                            self.window_manager.set_visibility(wid, watch=False)
                        self.watching_window = False
                        return {
                            "ok": True,
                            "resolved": True,
                            "resolved_by": "auto_detected_login_success",
                            "window_id": wid,
                            "url": gate_now.get("url", ""),
                            "elapsed_sec": round(now - req_ts, 1),
                        }
                else:
                    clear_streak = 0

            time.sleep(0.25)

        return {
            "ok": False,
            "resolved": False,
            "timeout": True,
            "window_id": wid or None,
            "message": "等待登录/授权超时，灵动岛卡片仍保留，你完成后可随时让 AI 继续。",
        }

    def execute_batch(
        self,
        actions: List[Dict[str, Any]],
        monitor_index: int = 1,
        window_id: Optional[str] = None,
        inter_action_delay: float = 0.05,
    ) -> Dict[str, Any]:
        """Execute a sequence of deterministic UI actions in a single fast burst."""
        if not isinstance(actions, list) or not actions:
            return {"ok": False, "error": "actions list cannot be empty"}

        results: List[Dict[str, Any]] = []
        t0 = time.time()
        total = min(12, len(actions))
        for idx, step in enumerate(actions[:12], start=1):
            self.check_not_stopped()
            if not isinstance(step, dict):
                continue
            act = str(step.get("action") or "").lower().strip()
            wid = str(step.get("window_id") or window_id or "").strip() or None

            if act == "click":
                r = self.click(
                    x=float(step["x"]) if step.get("x") is not None else None,
                    y=float(step["y"]) if step.get("y") is not None else None,
                    button=str(step.get("button", "left")),
                    clicks=int(step.get("clicks", 1)),
                    monitor_index=monitor_index,
                    window_id=wid,
                    element_index=int(step["element_index"]) if step.get("element_index") is not None else None,
                )
            elif act == "type":
                r = self.type_text(
                    text=str(step.get("text", "")),
                    x=float(step["x"]) if step.get("x") is not None else None,
                    y=float(step["y"]) if step.get("y") is not None else None,
                    clear_first=bool(step.get("clear_first", False)),
                    press_enter=bool(step.get("press_enter", False)),
                    monitor_index=monitor_index,
                    window_id=wid,
                    element_index=int(step["element_index"]) if step.get("element_index") is not None else None,
                )
            elif act == "key":
                r = self.press_key(str(step.get("keys", "")), window_id=wid)
            elif act == "scroll":
                r = self.scroll(
                    direction=str(step.get("direction", "down")),
                    ticks=int(step.get("ticks", 5)),
                    x=float(step["x"]) if step.get("x") is not None else None,
                    y=float(step["y"]) if step.get("y") is not None else None,
                    monitor_index=monitor_index,
                    window_id=wid,
                )
            elif act == "move":
                r = self.move(
                    x=float(step["x"]),
                    y=float(step["y"]),
                    monitor_index=monitor_index,
                    window_id=wid,
                )
            elif act == "drag":
                r = self.drag(
                    start_x=float(step["start_x"]),
                    start_y=float(step["start_y"]),
                    end_x=float(step["end_x"]),
                    end_y=float(step["end_y"]),
                    monitor_index=monitor_index,
                    window_id=wid,
                )
            elif act == "open":
                r = self.open_target(
                    str(step.get("target", "")),
                    silent=step.get("silent"),
                    watch=step.get("watch"),
                )
            elif act == "wait":
                secs = max(0.05, min(8.0, float(step.get("seconds", 0.3))))
                time.sleep(secs)
                r = {"ok": True, "action": "wait", "seconds": secs}
            else:
                r = {"ok": False, "action": act, "error": f"Unsupported batch action: {act}"}

            results.append({"step": idx, "total": total, **r})
            step_delay = (
                max(inter_action_delay, 0.22)
                if HUD.is_watching(self.watching_window)
                else inter_action_delay
            )
            if idx < total and step_delay > 0:
                time.sleep(step_delay)

        elapsed_ms = int(round((time.time() - t0) * 1000))
        self._emit_hud(f"✓ 已完成 {len(results)} 步后台连招", done=True)
        return {
            "ok": True,
            "executed_count": len(results),
            "elapsed_ms": elapsed_ms,
            "results": results,
        }

    def get_system_diagnostics(self) -> Dict[str, Any]:
        monitors_info: List[Dict[str, Any]] = []
        gui_error: Optional[str] = None
        cursor = (0, 0)
        try:
            monitors_info = [asdict(m) for m in self.list_monitors()]
            cursor = self.get_cursor_norm(1)
        except Exception as exc:
            gui_error = str(exc)

        return {
            "version": "3.1.0",
            "platform": sys.platform,
            "os_name": platform.platform(),
            "python_version": platform.python_version(),
            "stopped": self.is_stopped,
            "monitors": monitors_info,
            "cursor_norm": {"x": cursor[0], "y": cursor[1]},
            "session": self.get_session_state(),
            "windows_count": len(self.list_windows()),
            "gui_error": gui_error,
        }


# Shared singleton instance
ENGINE = DesktopEngine()

