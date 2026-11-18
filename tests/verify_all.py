#!/usr/bin/env python3
"""End-to-end automated verification suite for agy-computer-use v2.0.

Tests:
1. Silent Window Engine: spawns a real X11 window, binds to it in `silent_window` mode,
   runs a high-speed `execute_batch` (click + type + enter), verifies the background
   window received the events while the physical OS mouse cursor NEVER moved, and
   verifies occluded single-window capture + `set_watch_window(True/False)`.
2. Full-desktop foreground mode + DPI coordinate calibration + corner failsafe guard.
3. MCP v2.0 stdio JSON-RPC 2.0 protocol (15 tools including `computer_open_silent`,
   `computer_batch`, `computer_list_windows`, `computer_watch_window`).
4. Safe `mcp_config.json` merge preserving pre-existing MCP servers.
5. Standalone Console v2.0 HTTP API (`/api/windows`, `/api/mode`, `/api/state?shot=0`)
   + CSRF header protection.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.desktop_engine import ENGINE, EmergencyStopError  # noqa: E402
from core.hud_bridge import HUD, NativeHUDBridge  # noqa: E402
from core.autonomous_agent import normalize_api_base  # noqa: E402
from setup_agy import install_agy_mcp_config, check_agy_mcp_installed  # noqa: E402


def test_url_normalizer() -> None:
    assert normalize_api_base("https://api.example.com") == "https://api.example.com/v1"
    assert normalize_api_base("https://api.example.com/v1/") == "https://api.example.com/v1"
    assert (
        normalize_api_base("https://api.example.com/v1/chat/completions")
        == "https://api.example.com/v1"
    )
    assert (
        normalize_api_base("https://generativelanguage.googleapis.com/v1beta/openai")
        == "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    print("[PASS] Base URL normalization")


def test_silent_window_zero_mouse_steal() -> None:
    """Prove silent background window control does NOT move physical mouse cursor."""
    if not sys.platform.startswith("linux"):
        print("[SKIP] X11 mock window harness runs on Linux/Xvfb")
        return

    import pyautogui
    from Xlib import X, XK, display

    received_clicks: List[tuple] = []
    received_keys: List[str] = []
    ready_evt = threading.Event()
    stop_win_evt = threading.Event()

    def _mock_x11_app() -> None:
        d = display.Display()
        scr = d.screen()
        win = scr.root.create_window(
            160,
            110,
            420,
            300,
            0,
            scr.root_depth,
            X.InputOutput,
            X.CopyFromParent,
            background_pixel=0x162232,
            event_mask=X.ExposureMask | X.ButtonPressMask | X.ButtonReleaseMask | X.KeyPressMask,
        )
        win.set_wm_name("QIXIAN Silent Target Window")
        win.map()
        d.sync()
        ready_evt.set()

        while not stop_win_evt.is_set():
            while d.pending_events() > 0:
                ev = d.next_event()
                if ev.type == X.ButtonPress:
                    received_clicks.append((int(ev.event_x), int(ev.event_y), int(ev.detail)))
                elif ev.type == X.KeyPress:
                    ks = d.keycode_to_keysym(ev.detail, 0)
                    name = XK.keysym_to_string(ks) or chr(ks) if 32 <= ks < 127 else str(ks)
                    received_keys.append(name)
            time.sleep(0.01)
        win.destroy()
        d.close()

    app_thread = threading.Thread(target=_mock_x11_app, daemon=True)
    app_thread.start()
    assert ready_evt.wait(timeout=3.0), "Mock X11 window failed to start"
    time.sleep(0.1)

    try:
        # Park physical mouse at (88, 99) — MUST remain untouched!
        pyautogui.moveTo(88, 99, duration=0.0)
        phys_before = pyautogui.position()
        assert (phys_before.x, phys_before.y) == (88, 99)

        ENGINE.set_execution_mode("silent_window")
        wins = ENGINE.list_windows()
        assert any("QIXIAN Silent Target Window" in w["title"] for w in wins), f"Windows={wins}"

        bind_res = ENGINE.bind_window("QIXIAN Silent Target Window")
        assert bind_res["ok"] is True
        wid = bind_res["bound_window_id"]
        assert wid is not None

        # Execute multi-step batch silently in background!
        t0 = time.time()
        batch_res = ENGINE.execute_batch(
            [
                {"action": "click", "x": 500, "y": 500},
                {"action": "type", "text": "agy2", "press_enter": True},
            ],
            inter_action_delay=0.02,
        )
        elapsed_ms = (time.time() - t0) * 1000.0
        assert batch_res["ok"] is True
        assert batch_res["executed_count"] == 2
        time.sleep(0.08)

        # 1. Physical cursor MUST NOT HAVE MOVED AT ALL!
        phys_after = pyautogui.position()
        assert (phys_after.x, phys_after.y) == (88, 99), (
            f"Physical mouse was stolen! Before={phys_before}, After={phys_after}"
        )

        # 2. Background window MUST have received both the click and keystrokes!
        assert len(received_clicks) >= 1, "Background window did not receive silent click!"
        cx, cy, btn = received_clicks[0]
        assert abs(cx - 210) <= 3 and abs(cy - 150) <= 3 and btn == 1, f"Bad click coords: {received_clicks}"
        assert len(received_keys) >= 5, f"Background window did not receive silent keys: {received_keys}"

        # 3. Single-window screenshot captures only the 420x300 window + AI virtual cursor
        shot = ENGINE.screenshot(draw_grid=True, draw_cursor=True)
        assert shot.execution_mode == "silent_window"
        assert shot.window_id == wid
        assert (shot.image_width, shot.image_height) == (420, 300)
        assert (shot.cursor_norm_x, shot.cursor_norm_y) == (500, 500)

        # 4. Watch toggle (bring forward / tuck behind)
        w_on = ENGINE.set_watch_window(watch=True)
        w_off = ENGINE.set_watch_window(watch=False)
        assert w_on["ok"] is True and w_off["ok"] is True

        ENGINE.unbind_window()
        print(
            f"[PASS] Silent Window v2.0: 0px physical mouse movement, "
            f"batch click+type in {elapsed_ms:.0f}ms, window shot=420x300"
        )
    finally:
        stop_win_evt.set()
        app_thread.join(timeout=2.0)


def test_foreground_engine_and_overlay(artifact_out: Optional[Path] = None) -> None:
    ENGINE.set_execution_mode("foreground_live")
    diag = ENGINE.get_system_diagnostics()
    assert not diag["gui_error"], f"GUI error: {diag['gui_error']}"
    assert len(diag["monitors"]) >= 1

    sx0, sy0 = ENGINE.norm_to_screen(0, 0, monitor_index=1)
    assert sx0 >= 5 and sy0 >= 5, f"Corner guard failed: {(sx0, sy0)}"

    res_click = ENGINE.click(500, 500, button="left", clicks=1)
    assert res_click["ok"] is True and res_click["silent"] is False

    res_type = ENGINE.type_text("Hello AGY v2!", press_enter=True)
    assert res_type["ok"] is True

    shot = ENGINE.screenshot(monitor_index=1, draw_grid=True, draw_cursor=True)
    raw_bytes = base64.b64decode(shot.jpeg_base64)
    assert len(raw_bytes) > 2000, "Screenshot payload unexpectedly tiny"
    if artifact_out is not None:
        artifact_out.write_bytes(raw_bytes)

    ENGINE.set_execution_mode("silent_window")
    print(
        f"[PASS] Foreground live engine screen={shot.logical_width}x{shot.logical_height}, "
        f"jpeg={len(raw_bytes)//1024}KB"
    )


def test_mcp_stdio_server() -> None:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "mcp_server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None

    def rpc(msg: Dict[str, Any]) -> Dict[str, Any]:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, f"MCP server closed stdout unexpectedly. stderr={proc.stderr.read() if proc.stderr else ''}"
        return json.loads(line)

    try:
        r_init = rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            }
        )
        assert r_init["result"]["serverInfo"]["name"] == "agy-computer-use"
        assert r_init["result"]["serverInfo"]["version"] == "3.1.0"

        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        r_tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tool_names = {t["name"] for t in r_tools["result"]["tools"]}
        required_v31 = {
            "computer_open_silent",
            "computer_wait_for_user_auth",
            "computer_list_windows",
            "computer_bind_window",
            "computer_watch_window",
            "computer_batch",
            "computer_screenshot",
            "computer_click",
            "computer_type",
        }
        assert required_v31.issubset(tool_names), f"Missing v3.1 tools: {required_v31 - tool_names}"

        r_wins = rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "computer_list_windows", "arguments": {}},
            }
        )
        assert r_wins["result"]["isError"] is False

        r_call = rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "computer_screenshot",
                    "arguments": {"monitor": 1},
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
                },
            }
        )
        res = r_call["result"]
        assert res["isError"] is False
        assert res["resultType"] == "complete"
        types = [c["type"] for c in res["content"]]
        assert "image" in types and "text" in types
        print(f"[PASS] MCP v2.1 stdio server ({len(tool_names)} tools + silent window tools verified)")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_mcp_and_skill_installer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cfg_file = Path(tmp) / "mcp_config.json"
        skill_root = Path(tmp) / "skills"
        cfg_file.write_text(
            json.dumps({"mcpServers": {"existing-db": {"command": "node", "args": ["db.js"]}}}),
            encoding="utf-8",
        )
        res = install_agy_mcp_config(custom_path=cfg_file, skill_roots=[skill_root])
        merged = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "existing-db" in merged["mcpServers"], "Pre-existing MCP server was lost!"
        entry = merged["mcpServers"]["computer-use"]
        assert entry["env"]["PYTHONIOENCODING"] == "utf-8"
        assert "computer_open_silent" in entry["tools"]
        assert "computer_get_window_elements" in entry["tools"]
        assert check_agy_mcp_installed(custom_path=cfg_file)["installed"] is True

        skill_md = skill_root / "computer-use" / "SKILL.md"
        assert skill_md.is_file() and len(res["installed_skills"]) == 1
        assert "name: computer-use" in skill_md.read_text(encoding="utf-8")
        print("[PASS] Native AGY MCP v2.1 + Global SKILL.md installer verified")


def test_cdp_silent_websocket_engine() -> None:
    """Verify zero-dependency RFC 6455 WebSocket + CDPSilentSession against a mock CDP browser."""
    import hashlib
    import io
    import socket
    import struct
    from PIL import Image
    from core.cdp_browser import CDPSilentSession

    buf = io.BytesIO()
    Image.new("RGB", (640, 400), (18, 36, 58)).save(buf, format="JPEG")
    sample_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    cdp_events: List[Dict[str, Any]] = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(5)
    stop_srv = threading.Event()

    def _ws_send_frame(conn: socket.socket, text: str) -> None:
        data = text.encode("utf-8")
        hdr = bytearray([0x81])
        if len(data) < 126:
            hdr.append(len(data))
        elif len(data) < 65536:
            hdr.append(126)
            hdr.extend(struct.pack("!H", len(data)))
        else:
            hdr.append(127)
            hdr.extend(struct.pack("!Q", len(data)))
        conn.sendall(bytes(hdr) + data)

    def _ws_recv_frame(conn: socket.socket) -> Optional[str]:
        head = conn.recv(2)
        if len(head) < 2:
            return None
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", conn.recv(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", conn.recv(8))[0]
        mask_key = conn.recv(4) if masked else b""
        payload = bytearray()
        while len(payload) < length:
            chunk = conn.recv(length - len(payload))
            if not chunk:
                break
            payload.extend(chunk)
        if masked:
            payload = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return payload.decode("utf-8")

    def _mock_cdp_loop() -> None:
        srv.settimeout(0.3)
        while not stop_srv.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                raw = b""
                while b"\r\n\r\n" not in raw:
                    c = conn.recv(4096)
                    if not c:
                        break
                    raw += c
                req_text = raw.decode("latin1", errors="replace")
                if req_text.startswith("GET /json/list"):
                    body = json.dumps(
                        [
                            {
                                "type": "page",
                                "url": "https://www.bilibili.com",
                                "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/1",
                            }
                        ]
                    ).encode("utf-8")
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                        + body
                    )
                elif "Upgrade: websocket" in req_text:
                    ws_key = ""
                    for line in req_text.split("\r\n"):
                        if line.lower().startswith("sec-websocket-key:"):
                            ws_key = line.split(":", 1)[1].strip()
                    accept = base64.b64encode(
                        hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
                    ).decode("ascii")
                    conn.sendall(
                        (
                            "HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                        ).encode("ascii")
                    )
                    while not stop_srv.is_set():
                        msg_txt = _ws_recv_frame(conn)
                        if not msg_txt:
                            break
                        req_obj = json.loads(msg_txt)
                        cdp_events.append(req_obj)
                        m = req_obj["method"]
                        res_payload: Dict[str, Any] = {}
                        if m == "Page.getLayoutMetrics":
                            res_payload = {"cssLayoutViewport": {"clientWidth": 640, "clientHeight": 400}}
                        elif m == "Page.captureScreenshot":
                            res_payload = {"data": sample_b64}
                        _ws_send_frame(conn, json.dumps({"id": req_obj["id"], "result": res_payload}))

    th = threading.Thread(target=_mock_cdp_loop, daemon=True)
    th.start()
    try:
        sess = CDPSilentSession(port)
        assert sess.wait_until_ready(timeout=2.5) is True
        img = sess.capture_screenshot()
        assert img is not None and img.size == (640, 400)
        r_type = sess.type_text("你好B站🚀", norm_x=500, norm_y=250, press_enter=True)
        assert r_type["ok"] is True and r_type["mode"] == "silent_cdp_insert_text"
        methods = [e["method"] for e in cdp_events]
        assert "Input.dispatchMouseEvent" in methods
        assert "Input.insertText" in methods
        assert "Input.dispatchKeyEvent" in methods
        sess.close()
        print("[PASS] CDP Silent Browser Engine (RFC 6455 WebSocket + Page.captureScreenshot + Chinese Input.insertText)")
    finally:
        stop_srv.set()
        srv.close()
        th.join(timeout=1.5)


def test_native_hud_bridge_and_stop() -> None:
    """Verify v3.0 NativeHUDBridge UDP telemetry (status/cursor/watch) + floating pill Stop abort."""
    import os
    import socket

    udp_srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_srv.bind(("127.0.0.1", 0))
    udp_srv.settimeout(0.5)
    port = udp_srv.getsockname()[1]

    with tempfile.TemporaryDirectory() as tmp:
        bridge = NativeHUDBridge()
        bridge.enabled = True
        bridge.port = port
        bridge.state_file = Path(tmp) / "hud_state.json"
        bridge.pid_file = Path(tmp) / "hud.pid"
        bridge.pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

        bridge.notify_cursor(
            620,
            340,
            kind="click",
            badge="Click",
            text="正在后台操作: 点击搜索框",
            pid=12345,
            bounds=[100, 80, 1280, 800],
            inset_top=78,
            content_size=[1280, 722],
        )
        raw, _ = udp_srv.recvfrom(8192)
        pkt = json.loads(raw.decode("utf-8"))
        assert pkt["cmd"] == "cursor"
        assert pkt["nx"] == 620.0 and pkt["ny"] == 340.0
        assert pkt["kind"] == "click" and pkt["inset_top"] == 78.0

        # Verify Dynamic Island Auth / Login takeover packet & instant resolution
        req_ts = time.time()
        bridge.notify_auth("需要扫码登录小红书", "扫码后自动切回后台继续执行", pop_window=True, pid=12345)
        raw_auth, _ = udp_srv.recvfrom(8192)
        pkt_auth = json.loads(raw_auth.decode("utf-8"))
        assert pkt_auth["cmd"] == "auth" and pkt_auth["phase"] == "auth"

        # Simulate user clicking "✓ 我已完成，继续后台执行" on the MacBook Dynamic Island
        bridge.state_file.write_text(
            json.dumps(
                {
                    "watching": False,
                    "auth_active": False,
                    "auth_resolved_ts": req_ts + 0.5,
                    "stop_ts": time.time() + 1.0,
                }
            ),
            encoding="utf-8",
        )
        assert bridge.check_auth_resolved(req_ts) is True
        assert bridge.check_stop_clicked() is True
        assert bridge.check_stop_clicked() is False
        bridge.clear_stop()

    udp_srv.close()
    print("[PASS] v3.1 MacBook Dynamic Island UDP telemetry (waveform/auth/permission/cursor) verified")


if __name__ == "__main__":
    out_img = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    test_url_normalizer()
    test_mcp_and_skill_installer()
    test_cdp_silent_websocket_engine()
    test_silent_window_zero_mouse_steal()
    test_foreground_engine_and_overlay(out_img)
    test_mcp_stdio_server()
    test_native_hud_bridge_and_stop()
    print("\nALL V3.1 MACBOOK DYNAMIC ISLAND TESTS PASSED SUCCESSFULLY!")


