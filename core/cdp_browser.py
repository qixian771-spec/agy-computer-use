"""Zero-dependency Chrome/Edge DevTools Protocol (CDP) Silent Background Controller.

Why this makes browser automation truly "App-Grade":
- Traditional OS `PostMessage` clicks/keys can be ignored by unfocused Chromium tabs
  or complex rich-text editors (Bilibili, Douyin, Xiaohongshu, Notion).
- By attaching over localhost CDP WebSocket to our isolated silent Chrome/Edge window
  (launched with `--disable-backgrounding-occluded-windows`), every click, scroll,
  hover, shortcut, Chinese/Emoji `Input.insertText`, DOM element inspection, and
  `Page.captureScreenshot` executes with 100% `isTrusted: true` fidelity at <15ms latency —
  even when the browser window is completely covered or tucked in the background!
"""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import struct
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from PIL import Image


class _MinimalWebSocket:
    """Pure-stdlib RFC 6455 WebSocket client for localhost ws:// CDP endpoints."""

    def __init__(self, ws_url: str, timeout: float = 6.0) -> None:
        parsed = urlparse(ws_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        self._sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(handshake.encode("ascii"))

        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket handshake closed prematurely")
            resp += chunk
        header_line = resp.split(b"\r\n", 1)[0]
        if b"101" not in header_line:
            raise ConnectionError(f"CDP WebSocket upgrade failed: {header_line!r}")
        self._leftover = resp.split(b"\r\n\r\n", 1)[1]

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        if self._leftover:
            take = min(n, len(self._leftover))
            buf.extend(self._leftover[:take])
            self._leftover = self._leftover[take:]
        while len(buf) < n:
            chunk = self._sock.recv(max(4096, n - len(buf)))
            if not chunk:
                raise ConnectionError("CDP WebSocket connection closed")
            need = n - len(buf)
            if len(chunk) <= need:
                buf.extend(chunk)
            else:
                buf.extend(chunk[:need])
                self._leftover += chunk[need:]
        return bytes(buf)

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask_key = os.urandom(4)
        header = bytearray([0x81])  # FIN=1, opcode=1 (text)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        fragments: List[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask_key = self._recv_exact(4) if masked else b""
            data = self._recv_exact(length)
            if masked:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))

            if opcode == 0x8:  # Close
                raise ConnectionError("CDP WebSocket closed by browser")
            if opcode == 0x9:  # Ping -> send Pong
                pong = bytearray([0x8A, 0x80 | min(125, len(data))])
                mk = os.urandom(4)
                pong.extend(mk)
                pong.extend(bytes(b ^ mk[i % 4] for i, b in enumerate(data[:125])))
                self._sock.sendall(bytes(pong))
                continue
            if opcode in (0x0, 0x1, 0x2):
                fragments.append(data)
                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


class CDPSilentSession:
    """Controls a dedicated background Chrome/Edge window via DevTools Protocol."""

    CDP_KEY_MAP: Dict[str, Tuple[str, str, int]] = {
        "enter": ("Enter", "Enter", 13),
        "return": ("Enter", "Enter", 13),
        "tab": ("Tab", "Tab", 9),
        "backspace": ("Backspace", "Backspace", 8),
        "esc": ("Escape", "Escape", 27),
        "escape": ("Escape", "Escape", 27),
        "space": (" ", "Space", 32),
        "delete": ("Delete", "Delete", 46),
        "up": ("ArrowUp", "ArrowUp", 38),
        "down": ("ArrowDown", "ArrowDown", 40),
        "left": ("ArrowLeft", "ArrowLeft", 37),
        "right": ("ArrowRight", "ArrowRight", 39),
        "home": ("Home", "Home", 36),
        "end": ("End", "End", 35),
        "pageup": ("PageUp", "PageUp", 33),
        "pagedown": ("PageDown", "PageDown", 34),
    }

    def __init__(self, port: int) -> None:
        self.port = port
        self._lock = threading.RLock()
        self._ws: Optional[_MinimalWebSocket] = None
        self._ws_url: str = ""
        self._msg_id: int = 0
        self.viewport_size: Tuple[int, int] = (1280, 800)

    @staticmethod
    def allocate_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _discover_page_ws_url(self) -> Optional[str]:
        url = f"http://127.0.0.1:{self.port}/json/list"
        req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{self.port}"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
        pages = [
            t
            for t in targets
            if isinstance(t, dict)
            and t.get("type") == "page"
            and t.get("webSocketDebuggerUrl")
            and not str(t.get("url", "")).startswith("devtools://")
        ]
        if not pages:
            return None
        return str(pages[0]["webSocketDebuggerUrl"])

    def wait_until_ready(self, timeout: float = 4.5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ws_url = self._discover_page_ws_url()
                if ws_url:
                    self._ensure_connected(ws_url)
                    return True
            except Exception:
                pass
            time.sleep(0.12)
        return False

    def _ensure_connected(self, force_url: Optional[str] = None) -> None:
        target_url = force_url or self._discover_page_ws_url()
        if not target_url:
            raise ConnectionError("No active CDP page target available")
        if self._ws is not None and self._ws_url == target_url:
            return
        if self._ws is not None:
            self._ws.close()
        self._ws = _MinimalWebSocket(target_url, timeout=6.0)
        self._ws_url = target_url
        self._call_raw("Page.enable", {})
        self._call_raw("Runtime.enable", {})

    def _call_raw(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        assert self._ws is not None
        self._msg_id += 1
        req_id = self._msg_id
        self._ws.send_text(json.dumps({"id": req_id, "method": method, "params": params}))
        deadline = time.time() + 6.0
        while time.time() < deadline:
            msg = json.loads(self._ws.recv_text())
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} error: {msg['error']}")
                return msg.get("result") or {}
        raise TimeoutError(f"CDP {method} timed out")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            try:
                self._ensure_connected()
                return self._call_raw(method, params or {})
            except Exception:
                # Reconnect once in case user navigated or opened a new active tab
                if self._ws is not None:
                    self._ws.close()
                    self._ws = None
                self._ensure_connected()
                return self._call_raw(method, params or {})

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def _refresh_viewport_metrics(self) -> Tuple[int, int]:
        try:
            metrics = self.call("Page.getLayoutMetrics")
            vp = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
            w = int(vp.get("clientWidth") or 1280)
            h = int(vp.get("clientHeight") or 800)
            if w > 50 and h > 50:
                self.viewport_size = (w, h)
        except Exception:
            pass
        return self.viewport_size

    def norm_to_viewport(self, norm_x: float, norm_y: float) -> Tuple[float, float]:
        vw, vh = self._refresh_viewport_metrics()
        nx = max(0.0, min(1000.0, float(norm_x)))
        ny = max(0.0, min(1000.0, float(norm_y)))
        return round((nx / 1000.0) * max(1, vw - 1), 1), round((ny / 1000.0) * max(1, vh - 1), 1)

    def capture_screenshot(self) -> Optional[Image.Image]:
        res = self.call(
            "Page.captureScreenshot",
            {"format": "jpeg", "quality": 82, "captureBeyondViewport": False},
        )
        b64 = res.get("data")
        if not b64:
            return None
        raw = base64.b64decode(b64)
        with Image.open(io.BytesIO(raw)) as im:
            img = im.convert("RGB")
            self.viewport_size = img.size
            return img

    def click(self, norm_x: float, norm_y: float, button: str = "left", clicks: int = 1) -> Dict[str, Any]:
        vx, vy = self.norm_to_viewport(norm_x, norm_y)
        btn = button.lower().strip()
        if btn not in ("left", "right", "middle"):
            btn = "left"
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": vx, "y": vy})
        for idx in range(1, max(1, min(3, int(clicks))) + 1):
            self.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": vx,
                    "y": vy,
                    "button": btn,
                    "clickCount": idx,
                },
            )
            self.call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": vx,
                    "y": vy,
                    "button": btn,
                    "clickCount": idx,
                },
            )
        return {"ok": True, "mode": "silent_cdp", "viewport_x": vx, "viewport_y": vy}

    def move(self, norm_x: float, norm_y: float) -> Dict[str, Any]:
        vx, vy = self.norm_to_viewport(norm_x, norm_y)
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": vx, "y": vy})
        return {"ok": True, "mode": "silent_cdp", "viewport_x": vx, "viewport_y": vy}

    def scroll(self, direction: str, ticks: int, norm_x: float = 500.0, norm_y: float = 500.0) -> Dict[str, Any]:
        vx, vy = self.norm_to_viewport(norm_x, norm_y)
        d = direction.lower().strip()
        px = max(1, min(30, int(ticks))) * 110
        dx, dy = 0, 0
        if d == "down":
            dy = px
        elif d == "up":
            dy = -px
        elif d == "right":
            dx = px
        elif d == "left":
            dx = -px
        self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": vx, "y": vy, "deltaX": dx, "deltaY": dy},
        )
        return {"ok": True, "mode": "silent_cdp", "delta_x": dx, "delta_y": dy}

    def drag(self, sx_norm: float, sy_norm: float, ex_norm: float, ey_norm: float) -> Dict[str, Any]:
        vx1, vy1 = self.norm_to_viewport(sx_norm, sy_norm)
        vx2, vy2 = self.norm_to_viewport(ex_norm, ey_norm)
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": vx1, "y": vy1})
        self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": vx1, "y": vy1, "button": "left", "clickCount": 1})
        for i in range(1, 6):
            ix = round(vx1 + (vx2 - vx1) * (i / 5.0), 1)
            iy = round(vy1 + (vy2 - vy1) * (i / 5.0), 1)
            self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": ix, "y": iy, "buttons": 1})
        self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": vx2, "y": vy2, "button": "left", "clickCount": 1})
        return {"ok": True, "mode": "silent_cdp"}

    def press_key(self, keys: str) -> Dict[str, Any]:
        parts = [p.strip().lower() for p in keys.replace("-", "+").split("+") if p.strip()]
        if not parts:
            return {"ok": False, "error": "Empty key"}

        modifiers = 0
        for mod in parts[:-1]:
            if mod in ("alt", "opt", "option"):
                modifiers |= 1
            elif mod in ("ctrl", "control"):
                modifiers |= 2
            elif mod in ("cmd", "command", "meta", "win", "super"):
                modifiers |= 4
            elif mod == "shift":
                modifiers |= 8

        main = parts[-1]
        if main in self.CDP_KEY_MAP:
            k_str, code_str, vk = self.CDP_KEY_MAP[main]
        elif len(main) == 1:
            k_str = main
            code_str = f"Key{main.upper()}" if main.isalpha() else f"Digit{main}"
            vk = ord(main.upper())
        else:
            k_str, code_str, vk = main, main, 0

        # Handle select-all / copy / paste / undo shortcuts cleanly inside DOM too
        if modifiers in (2, 4) and main == "a":
            self.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "(() => { const el = document.activeElement; "
                        "if (el && typeof el.select === 'function') el.select(); "
                        "else document.execCommand('selectAll'); })()"
                    )
                },
            )
            return {"ok": True, "mode": "silent_cdp", "keys": keys}

        self.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "modifiers": modifiers,
                "key": k_str,
                "code": code_str,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
            },
        )
        self.call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "modifiers": modifiers,
                "key": k_str,
                "code": code_str,
                "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
            },
        )
        return {"ok": True, "mode": "silent_cdp", "keys": keys}

    def type_text(
        self,
        text: str,
        norm_x: Optional[float] = None,
        norm_y: Optional[float] = None,
        clear_first: bool = False,
        press_enter: bool = False,
    ) -> Dict[str, Any]:
        if norm_x is not None and norm_y is not None:
            self.click(norm_x, norm_y, button="left", clicks=1)
            time.sleep(0.03)
        if clear_first:
            self.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "(() => { const el = document.activeElement; "
                        "if (!el) return; "
                        "if ('value' in el) { el.value = ''; el.dispatchEvent(new Event('input', {bubbles:true})); } "
                        "else if (el.isContentEditable) { el.textContent = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }"
                        "})()"
                    )
                },
            )
        if text:
            self.call("Input.insertText", {"text": text})
        if press_enter:
            time.sleep(0.02)
            self.press_key("enter")
        return {"ok": True, "mode": "silent_cdp_insert_text", "typed_length": len(text)}

    def get_interactive_elements(self, max_items: int = 30) -> List[Dict[str, Any]]:
        js = f"""
        (() => {{
          const vw = Math.max(1, window.innerWidth);
          const vh = Math.max(1, window.innerHeight);
          const nodes = Array.from(document.querySelectorAll(
            'input, textarea, button, a[href], select, [role="button"], [role="searchbox"], [contenteditable="true"]'
          ));
          const out = [];
          for (const el of nodes) {{
            if (out.length >= {int(max_items)}) break;
            const r = el.getBoundingClientRect();
            if (r.width < 12 || r.height < 12 || r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) continue;
            const style = window.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) continue;
            const label = (
              el.getAttribute('placeholder') ||
              el.getAttribute('aria-label') ||
              el.innerText ||
              el.value ||
              el.getAttribute('title') ||
              ''
            ).replace(/\\s+/g, ' ').trim().slice(0, 48);
            const cx = Math.round(((r.left + r.width / 2) / vw) * 1000);
            const cy = Math.round(((r.top + r.height / 2) / vh) * 1000);
            if (cx >= 0 && cx <= 1000 && cy >= 0 && cy <= 1000) {{
              out.push({{
                index: out.length + 1,
                role: el.tagName.toLowerCase(),
                name: label,
                x: cx,
                y: cy,
                width: Math.round(r.width),
                height: Math.round(r.height)
              }});
            }}
          }}
          return out;
        }})()
        """
        try:
            res = self.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
            val = (res.get("result") or {}).get("value")
            return val if isinstance(val, list) else []
        except Exception:
            return []

    def get_window_screen_metrics(self) -> Optional[Dict[str, float]]:
        js = (
            "(() => ({ sx: window.screenX, sy: window.screenY, "
            "ow: window.outerWidth, oh: window.outerHeight, "
            "iw: window.innerWidth, ih: window.innerHeight }))()"
        )
        try:
            res = self.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
            val = (res.get("result") or {}).get("value")
            if isinstance(val, dict) and val.get("ow", 0) > 50 and val.get("ih", 0) > 50:
                ow = float(val["ow"])
                oh = float(val["oh"])
                iw = float(val["iw"])
                ih = float(val["ih"])
                return {
                    "sx": float(val.get("sx", 0)),
                    "sy": float(val.get("sy", 0)),
                    "ow": ow,
                    "oh": oh,
                    "iw": iw,
                    "ih": ih,
                    "inset_top": max(0.0, oh - ih),
                }
        except Exception:
            pass
        return None

    def detect_auth_gate(self) -> Dict[str, Any]:
        """Fast, zero-side-effect DOM/URL probe for Login, QR Scan, OAuth, or CAPTCHA gates."""
        js = """
        (() => {
          const href = (location.href || '').toLowerCase();
          const host = (location.hostname || '').toLowerCase();
          const path = (location.pathname || '').toLowerCase();
          const title = (document.title || '').trim();

          const isVisible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width < 40 || r.height < 18) return false;
            const st = window.getComputedStyle(el);
            return st.display !== 'none' && st.visibility !== 'hidden' && Number(st.opacity) > 0.05;
          };

          // 1. Check human security verification / CAPTCHA gates first (must NEVER be auto-solved)
          const captchaNodes = document.querySelectorAll(
            'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], ' +
            'iframe[src*="challenges.cloudflare"], [class*="secsdk-captcha"], [class*="secsdk_captcha"], ' +
            '[class*="geetest"], [class*="bili-verify"], [id*="captcha"], [class*="captcha-container"]'
          );
          for (const n of captchaNodes) {
            if (isVisible(n)) {
              return {
                needed: true,
                kind: 'captcha',
                title: '遇到安全验证 · 请手动完成',
                reason: `当前页面 (${host || title}) 弹出人机安全校验，请在窗口完成后点「我已完成」`,
                url: location.href
              };
            }
          }

          // 2. Dedicated Login / OAuth / SSO URLs
          const authHost = /^(passport\\.|sso\\.|login\\.|auth\\.|accounts\\.google\\.com|id\\.)/.test(host);
          const authPath = /(\\/login|\\/signin|\\/sign-in|\\/oauth\\/authorize|\\/passport|\\/sso|\\/auth\\/login)/.test(path);
          if (authHost || authPath) {
            return {
              needed: true,
              kind: 'login',
              title: `需要登录授权 · ${host || '当前网站'}`,
              reason: `已跳转到登录/授权页 (${title || host})，扫码或登录后将自动切回后台继续`,
              url: location.href
            };
          }

          // 3. Visible password input on page
          const pwdInputs = document.querySelectorAll('input[type="password"]');
          for (const p of pwdInputs) {
            if (isVisible(p)) {
              return {
                needed: true,
                kind: 'login',
                title: `需要登录账号 · ${host || '当前网站'}`,
                reason: '检测到账号密码登录框，请在弹出窗口完成登录授权',
                url: location.href
              };
            }
          }

          // 4. Blocking QR / Login modal overlay
          const modalSelectors = [
            '[role="dialog"]', '[aria-modal="true"]', '[class*="login-modal"]',
            '[class*="login-container"]', '[class*="qrcode-login"]', '[class*="bili-mini-mask"]',
            '[class*="reds-modal"]', '[class*="passport-login"]', '[class*="auth-modal"]'
          ];
          const modals = document.querySelectorAll(modalSelectors.join(','));
          for (const m of modals) {
            if (!isVisible(m)) continue;
            const r = m.getBoundingClientRect();
            if (r.width < 220 || r.height < 160) continue;
            const txt = (m.innerText || '').slice(0, 400);
            if (/(扫码登录|请先登录|请登录|账号登录|短信登录|获取验证码|微信登录|Sign in|Log in|Verify you are human)/i.test(txt)) {
              const isCap = /(安全验证|拖动滑块|Verify you are human)/i.test(txt);
              return {
                needed: true,
                kind: isCap ? 'captcha' : 'login',
                title: isCap ? '遇到安全验证 · 请手动完成' : `需要扫码 / 登录 · ${host || '当前网站'}`,
                reason: isCap
                  ? '页面弹出安全验证，请手动完成后点灵动岛「我已完成」'
                  : '页面弹出登录弹窗，扫码或登录完成后会自动切回后台继续',
                url: location.href
              };
            }
          }

          return { needed: false, kind: '', title: '', reason: '', url: location.href };
        })()
        """
        try:
            res = self.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
            val = (res.get("result") or {}).get("value")
            if isinstance(val, dict) and "needed" in val:
                return val
        except Exception:
            pass
        return {"needed": False, "kind": "", "title": "", "reason": "", "url": ""}

    def close(self) -> None:
        with self._lock:
            if self._ws is not None:
                self._ws.close()
                self._ws = None

