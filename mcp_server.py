#!/usr/bin/env python3
"""AGY Computer Use v2.0 — Silent-Window & High-Speed MCP stdio Server.

Compatible with:
- Google Antigravity App / Antigravity IDE / Antigravity CLI (~/.gemini/config/mcp_config.json)
- Claude Desktop / Claude Code / Cursor / any standard MCP client
- Both handshake-based MCP (2024-11-05 .. 2025-11-25) and stateless MCP (2026-07-28)
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

# CRITICAL FOR MCP STDIO: reserve real stdout exclusively for newline-delimited JSON-RPC.
if hasattr(sys.stdout, "buffer"):
    _MCP_STDOUT = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", newline="\n", write_through=True
    )
else:
    _MCP_STDOUT = sys.stdout
sys.stdout = sys.stderr

_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)

from core.desktop_engine import ENGINE, EmergencyStopError  # noqa: E402

SERVER_NAME = "agy-computer-use"
SERVER_VERSION = "3.1.0"
SUPPORTED_VERSIONS = [
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
]

SERVER_INSTRUCTIONS = (
    "AGY Computer Use v3.1 (MacBook Dynamic Island Edition) gives you fast, silent-by-default window & "
    "browser control (macOS, Windows & Linux) with a live hardware-fused MacBook Dynamic Island HUD.\n"
    "CORE WORKFLOW & RULES:\n"
    "1. DEFAULT SILENT MODE (DO NOT STEAL THE USER'S MOUSE): Prefer `computer_open_silent` "
    "(launches URLs in an isolated CDP-attached Chrome/Edge silent window, or apps in a background OS window) "
    "or `computer_list_windows` + `computer_bind_window`. While working, the MacBook Dynamic Island "
    "automatically animates what you are doing!\n"
    "2. LOGIN / QR SCAN / CAPTCHA / AUTHORIZATION HANDOFF: Whenever a page or app requires login, QR scan, "
    "2FA, OAuth approval, or human security verification (CAPTCHA), NEVER attempt to bypass or solve it "
    "yourself! Immediately call `computer_wait_for_user_auth(title=..., reason=...)` — the MacBook Dynamic "
    "Island will glow amber, pop out the real window for the user, and automatically tuck it back into "
    "silent background & resume your execution as soon as login/verification succeeds!\n"
    "3. WATCH ANYTIME: Call `computer_watch_window(watch=True)` if the user asks to watch live, or "
    "`computer_watch_window(watch=False)` to tuck back behind Antigravity.\n"
    "4. BLAZING SPEED WITH BATCH: Combine predictable multi-step actions (click -> type -> enter) into a "
    "single `computer_batch` call.\n"
    "5. Coordinate System: ALL (x, y) coordinates are normalized integers in [0, 1000] relative to the "
    "captured image: (0,0)=top-left, (500,500)=center, (1000,1000)=bottom-right."
)

_STOP_TRIGGERED_AT: float = 0.0
_AUTO_COOLDOWN_SECONDS: float = 8.0


TOOLS_SCHEMA: List[Dict[str, Any]] = [
    {
        "name": "computer_open_silent",
        "description": (
            "DEFAULT START TOOL: Open a URL (in an isolated background browser window) or launch "
            "a desktop app silently WITHOUT stealing focus or moving the user's mouse, bind to "
            "its window, and return a screenshot + window_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "URL (e.g. 'https://www.bilibili.com') or app name/path (e.g. 'notepad').",
                },
                "watch": {
                    "type": "boolean",
                    "description": "False (default) = silent background execution; True = show window in front so user can watch.",
                    "default": False,
                },
                "return_screenshot": {"type": "boolean", "default": True},
            },
            "required": ["target"],
        },
    },
    {
        "name": "computer_wait_for_user_auth",
        "description": (
            "LOGIN / QR SCAN / OAUTH / CAPTCHA HANDOFF VIA MACBOOK DYNAMIC ISLAND: Call this whenever "
            "the target website or app requires user login, QR code scan, SMS code, OAuth consent, or "
            "human verification. Expands the MacBook Dynamic Island with an amber glow, brings the "
            "window forward for the user, waits until they click '我已完成' on the Dynamic Island (or "
            "auto-detects login completion!), tucks the window back into silent background, and returns "
            "a fresh screenshot so you can immediately continue the task!"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short Dynamic Island headline, e.g. '需要扫码登录 B 站' or '请完成安全验证'.",
                },
                "reason": {
                    "type": "string",
                    "description": "Clear 1-line instruction shown inside the Dynamic Island card.",
                },
                "auto_pop_window": {
                    "type": "boolean",
                    "description": "True (default) automatically brings the target login window to the front.",
                    "default": True,
                },
                "timeout_sec": {
                    "type": "number",
                    "description": "Max seconds to wait for user login/auth completion (default 180).",
                    "default": 180,
                },
                "window_id": {"type": "string"},
            },
        },
    },
    {
        "name": "computer_list_windows",
        "description": (
            "List all open application windows (window_id, app, title, size) and current "
            "silent/foreground session binding."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "computer_bind_window",
        "description": (
            "Lock onto a specific open window by `window_id` or title/app keyword (e.g. 'Chrome', "
            "'Notepad') for silent background control without touching the user's physical mouse. "
            "Pass 'desktop' to unbind and switch to full-screen foreground control."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_window": {
                    "type": "string",
                    "description": "Window ID, title/app substring, or 'desktop' to unbind.",
                },
                "execution_mode": {
                    "type": "string",
                    "enum": ["silent_window", "foreground_live"],
                    "default": "silent_window",
                },
                "return_screenshot": {"type": "boolean", "default": True},
            },
            "required": ["target_window"],
        },
    },
    {
        "name": "computer_watch_window",
        "description": (
            "Bring the bound silent window to the front so the user can watch the live process "
            "(`watch=true`), or tuck it back behind active windows for silent execution (`watch=false`)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "watch": {
                    "type": "boolean",
                    "description": "True = bring window to front to watch; False = send back to silent background.",
                },
                "window_id": {"type": "string", "description": "Optional explicit window_id."},
            },
            "required": ["watch"],
        },
    },
    {
        "name": "computer_batch",
        "description": (
            "HIGH-SPEED MULTI-STEP EXECUTION: Run 2..12 UI actions in one rapid burst (e.g. "
            "click input -> type text -> press enter -> wait) and return a single post-batch screenshot."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "description": (
                        "Ordered action objects, e.g. ["
                        "{'action':'click','x':480,'y':160}, "
                        "{'action':'type','text':'AI 短视频','press_enter':true}, "
                        "{'action':'wait','seconds':0.6}]"
                    ),
                    "items": {"type": "object"},
                },
                "window_id": {"type": "string"},
                "monitor": {"type": "integer", "default": 1},
                "return_screenshot": {"type": "boolean", "default": True},
            },
            "required": ["actions"],
        },
    },
    {
        "name": "computer_screenshot",
        "description": (
            "Capture a live screenshot (captures ONLY the bound silent window if bound, or full "
            "monitor if unbound/force_desktop=true) with 0..1000 coordinate ruler & AI cursor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "monitor": {"type": "integer", "default": 1},
                "window_id": {"type": "string", "description": "Optional specific window_id to capture."},
                "force_desktop": {
                    "type": "boolean",
                    "description": "Set True to capture the entire monitor instead of the bound window.",
                    "default": False,
                },
                "draw_grid": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "computer_click",
        "description": (
            "Click, double-click, or right-click at normalized (x, y) in [0, 1000] (or by "
            "`element_index`). Runs silently in background when a window is bound."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "Horizontal coordinate in [0, 1000]."},
                "y": {"type": "number", "description": "Vertical coordinate in [0, 1000]."},
                "element_index": {
                    "type": "integer",
                    "description": "Optional UI element index from window element list.",
                },
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                "clicks": {"type": "integer", "default": 1},
                "window_id": {"type": "string"},
                "monitor": {"type": "integer", "default": 1},
                "return_screenshot": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "computer_type",
        "description": (
            "Type text (Chinese, English, symbols, Emoji). In silent window mode, injects Unicode "
            "directly into the background window WITHOUT touching the user's clipboard or focus!"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text content to type."},
                "x": {"type": "number", "description": "Optional [0, 1000] X to click first."},
                "y": {"type": "number", "description": "Optional [0, 1000] Y to click first."},
                "element_index": {"type": "integer"},
                "clear_first": {"type": "boolean", "default": False},
                "press_enter": {"type": "boolean", "default": False},
                "window_id": {"type": "string"},
                "monitor": {"type": "integer", "default": 1},
                "return_screenshot": {"type": "boolean", "default": True},
            },
            "required": ["text"],
        },
    },
    {
        "name": "computer_key",
        "description": "Press a single key or shortcut combo ('enter', 'esc', 'tab', 'ctrl+l', 'cmd+t').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keys": {"type": "string"},
                "window_id": {"type": "string"},
                "monitor": {"type": "integer", "default": 1},
                "return_screenshot": {"type": "boolean", "default": True},
            },
            "required": ["keys"],
        },
    },
    {
        "name": "computer_scroll",
        "description": "Scroll wheel up, down, left, or right (silently in bound window or at x, y).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["down", "up", "left", "right"], "default": "down"},
                "ticks": {"type": "integer", "default": 5},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "window_id": {"type": "string"},
                "monitor": {"type": "integer", "default": 1},
                "return_screenshot": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "computer_drag",
        "description": "Drag from (start_x, start_y) to (end_x, end_y) in [0, 1000].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_x": {"type": "number"},
                "start_y": {"type": "number"},
                "end_x": {"type": "number"},
                "end_y": {"type": "number"},
                "window_id": {"type": "string"},
                "monitor": {"type": "integer", "default": 1},
                "return_screenshot": {"type": "boolean", "default": True},
            },
            "required": ["start_x", "start_y", "end_x", "end_y"],
        },
    },
    {
        "name": "computer_move",
        "description": "Move cursor to hover at normalized (x, y) in [0, 1000] without clicking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"},
                "y": {"type": "number"},
                "window_id": {"type": "string"},
                "monitor": {"type": "integer", "default": 1},
                "return_screenshot": {"type": "boolean", "default": False},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "computer_open",
        "description": (
            "Open a URL or desktop application (uses silent background window by default unless "
            "`silent=false` is passed)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "silent": {"type": "boolean", "default": True},
                "watch": {"type": "boolean", "default": False},
                "monitor": {"type": "integer", "default": 1},
                "return_screenshot": {"type": "boolean", "default": True},
            },
            "required": ["target"],
        },
    },
    {
        "name": "computer_wait",
        "description": "Wait N seconds for UI/page loading to settle, then return a fresh screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "default": 0.8},
                "window_id": {"type": "string"},
                "monitor": {"type": "integer", "default": 1},
            },
        },
    },
    {
        "name": "computer_screen_info",
        "description": "Inspect connected monitors, open windows count, silent session state, and DPI scaling.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _append_screenshot_blocks(
    blocks: List[Dict[str, Any]],
    monitor: int = 1,
    draw_grid: bool = True,
    window_id: Optional[str] = None,
    force_desktop: bool = False,
) -> None:
    shot = ENGINE.screenshot(
        monitor_index=monitor,
        draw_grid=draw_grid,
        draw_cursor=True,
        window_id=window_id,
        force_desktop=force_desktop,
    )
    auth_hint = ""
    if shot.auth_gate and shot.auth_gate.get("needed"):
        auth_hint = (
            f"\n[AUTH/VERIFY GATE DETECTED ({shot.auth_gate.get('kind')}): {shot.auth_gate.get('reason')} — "
            f"DO NOT enter passwords or solve CAPTCHAs yourself! Call `computer_wait_for_user_auth` immediately "
            f"to wake the user via the MacBook Dynamic Island and auto-resume once completed!]"
        )

    if shot.window_id:
        elems = ENGINE.get_window_elements(window_id=shot.window_id)[:15]
        elem_hint = ""
        if elems:
            compact = ", ".join(
                f"[{e['index']}]{e['role']}:'{e['name']}'@({e['x']},{e['y']})" for e in elems
            )
            elem_hint = f"\n[Detected Controls: {compact}]"
        blocks.append(
            {
                "type": "text",
                "text": (
                    f"[Silent Window #{shot.window_id} '{shot.window_title}': "
                    f"size={shot.logical_width}x{shot.logical_height}, "
                    f"image={shot.image_width}x{shot.image_height}, "
                    f"AI_virtual_cursor=({shot.cursor_norm_x}, {shot.cursor_norm_y}) in 0..1000]"
                    f"{elem_hint}{auth_hint}"
                ),
            }
        )
    else:
        blocks.append(
            {
                "type": "text",
                "text": (
                    f"[Desktop Screen #{shot.monitor_index}: logical={shot.logical_width}x{shot.logical_height}, "
                    f"image={shot.image_width}x{shot.image_height}, "
                    f"cursor=({shot.cursor_norm_x}, {shot.cursor_norm_y}) in 0..1000]{auth_hint}"
                ),
            }
        )
    blocks.append(
        {
            "type": "image",
            "data": shot.jpeg_base64,
            "mimeType": "image/jpeg",
        }
    )


def _check_cooldown() -> None:
    global _STOP_TRIGGERED_AT
    if ENGINE.is_stopped:
        elapsed = time.time() - _STOP_TRIGGERED_AT
        if _STOP_TRIGGERED_AT > 0 and elapsed >= _AUTO_COOLDOWN_SECONDS:
            ENGINE.reset_emergency_stop()
            _STOP_TRIGGERED_AT = 0.0
        else:
            remain = max(1, int(round(_AUTO_COOLDOWN_SECONDS - elapsed)))
            raise EmergencyStopError(
                f"检测到紧急急停，自动化已锁定（剩余冷却 {remain}s）。请等待用户新指令。"
            )


def _execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    global _STOP_TRIGGERED_AT
    monitor = int(args.get("monitor", 1) or 1)
    window_id = str(args.get("window_id") or "").strip() or None
    want_shot = bool(args.get("return_screenshot", True))

    try:
        if name == "computer_screen_info":
            info = ENGINE.get_system_diagnostics()
            return {
                "content": [{"type": "text", "text": json.dumps(info, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        if name == "computer_list_windows":
            payload = {
                "session": ENGINE.get_session_state(),
                "windows": ENGINE.list_windows(),
            }
            return {
                "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        _check_cooldown()
        blocks: List[Dict[str, Any]] = []

        if name == "computer_open_silent":
            ENGINE.set_execution_mode("silent_window")
            watch = bool(args.get("watch", False))
            res = ENGINE.open_target(str(args["target"]), silent=True, watch=watch)
            gate_note = ""
            if res.get("auth_gate") and res["auth_gate"].get("needed"):
                gate_note = (
                    f"\n[AUTH GATE DETECTED: {res['auth_gate'].get('reason')} — "
                    f"Dynamic Island has notified the user! Call `computer_wait_for_user_auth` now to wait for completion.]"
                )
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"Opened in {'visible foreground' if watch else 'silent background'}: "
                        f"target={res.get('target')}, bound_window_id={res.get('window_id')}{gate_note}"
                    ),
                }
            )
            if want_shot:
                time.sleep(0.45)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=res.get("window_id"))

        elif name == "computer_wait_for_user_auth":
            res = ENGINE.wait_for_user_auth(
                reason=str(args.get("reason") or "请在弹出的窗口完成扫码登录或授权，完成后点灵动岛「我已完成」继续"),
                title=str(args.get("title") or "需要你完成登录 / 授权"),
                auto_pop_window=bool(args.get("auto_pop_window", True)),
                timeout_sec=float(args.get("timeout_sec", 180.0)),
                window_id=window_id,
            )
            blocks.append({"type": "text", "text": json.dumps(res, ensure_ascii=False)})
            if res.get("ok") and want_shot:
                time.sleep(0.25)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=res.get("window_id") or window_id)
            return {"content": blocks, "isError": not bool(res.get("ok"))}

        elif name == "computer_bind_window":
            target_win = str(args.get("target_window") or "").strip()
            mode = str(args.get("execution_mode") or "silent_window").strip()
            if target_win.lower() in ("desktop", "none", "0"):
                ENGINE.unbind_window()
                ENGINE.set_execution_mode("foreground_live")
                blocks.append({"type": "text", "text": "Unbound window -> switched to full-desktop foreground mode."})
            else:
                ENGINE.set_execution_mode(mode)
                res = ENGINE.bind_window(target_win)
                if not res.get("ok"):
                    return {"content": [{"type": "text", "text": str(res.get("error"))}], "isError": True}
                blocks.append(
                    {
                        "type": "text",
                        "text": f"Bound silent window #{res['bound_window_id']} ({res['bound_window']['display_name']})",
                    }
                )
            if want_shot:
                _append_screenshot_blocks(blocks, monitor=monitor)

        elif name == "computer_watch_window":
            res = ENGINE.set_watch_window(watch=bool(args["watch"]), window_id=window_id)
            return {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                "isError": not bool(res.get("ok")),
            }

        elif name == "computer_batch":
            actions = args.get("actions") or []
            res = ENGINE.execute_batch(actions, monitor_index=monitor, window_id=window_id)
            blocks.append(
                {
                    "type": "text",
                    "text": f"Batch executed {res.get('executed_count', 0)} actions in {res.get('elapsed_ms', 0)}ms.",
                }
            )
            if want_shot:
                time.sleep(0.12)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=window_id)

        elif name == "computer_screenshot":
            draw_grid = bool(args.get("draw_grid", True))
            force_desktop = bool(args.get("force_desktop", False))
            _append_screenshot_blocks(
                blocks,
                monitor=monitor,
                draw_grid=draw_grid,
                window_id=window_id,
                force_desktop=force_desktop,
            )

        elif name == "computer_click":
            res = ENGINE.click(
                x=float(args["x"]) if args.get("x") is not None else None,
                y=float(args["y"]) if args.get("y") is not None else None,
                button=str(args.get("button", "left")),
                clicks=int(args.get("clicks", 1)),
                monitor_index=monitor,
                window_id=window_id,
                element_index=int(args["element_index"]) if args.get("element_index") is not None else None,
            )
            tag = "Silently clicked" if res.get("silent") else "Clicked"
            blocks.append(
                {"type": "text", "text": f"{tag} ({res['x']}, {res['y']}) [{res['button']} x{res['clicks']}]"}
            )
            if want_shot:
                time.sleep(0.10)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=window_id)

        elif name == "computer_type":
            res = ENGINE.type_text(
                text=str(args["text"]),
                x=float(args["x"]) if args.get("x") is not None else None,
                y=float(args["y"]) if args.get("y") is not None else None,
                clear_first=bool(args.get("clear_first", False)),
                press_enter=bool(args.get("press_enter", False)),
                monitor_index=monitor,
                window_id=window_id,
                element_index=int(args["element_index"]) if args.get("element_index") is not None else None,
            )
            tag = "Silently typed" if res.get("silent") else "Typed"
            blocks.append(
                {
                    "type": "text",
                    "text": f"{tag} {res['typed_length']} chars (press_enter={res['press_enter']})",
                }
            )
            if want_shot:
                time.sleep(0.10)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=window_id)

        elif name == "computer_key":
            res = ENGINE.press_key(str(args["keys"]), window_id=window_id)
            blocks.append({"type": "text", "text": f"Pressed key: {res.get('keys')} (silent={res.get('silent')})"})
            if want_shot:
                time.sleep(0.10)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=window_id)

        elif name == "computer_scroll":
            res = ENGINE.scroll(
                direction=str(args.get("direction", "down")),
                ticks=int(args.get("ticks", 5)),
                x=float(args["x"]) if args.get("x") is not None else None,
                y=float(args["y"]) if args.get("y") is not None else None,
                monitor_index=monitor,
                window_id=window_id,
            )
            blocks.append(
                {"type": "text", "text": f"Scrolled {res['direction']} ({res['ticks']} ticks, silent={res.get('silent')})"}
            )
            if want_shot:
                time.sleep(0.10)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=window_id)

        elif name == "computer_drag":
            res = ENGINE.drag(
                start_x=float(args["start_x"]),
                start_y=float(args["start_y"]),
                end_x=float(args["end_x"]),
                end_y=float(args["end_y"]),
                monitor_index=monitor,
                window_id=window_id,
            )
            blocks.append(
                {"type": "text", "text": f"Dragged to ({res['end_x']}, {res['end_y']})"}
            )
            if want_shot:
                time.sleep(0.12)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=window_id)

        elif name == "computer_move":
            res = ENGINE.move(
                x=float(args["x"]),
                y=float(args["y"]),
                monitor_index=monitor,
                window_id=window_id,
            )
            blocks.append({"type": "text", "text": f"Moved cursor to ({res['x']}, {res['y']})"})
            if bool(args.get("return_screenshot", False)):
                time.sleep(0.08)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=window_id)

        elif name == "computer_open":
            res = ENGINE.open_target(
                str(args["target"]),
                silent=args.get("silent", True),
                watch=args.get("watch", False),
            )
            blocks.append(
                {
                    "type": "text",
                    "text": f"Opened target: {res.get('target')} (silent={res.get('silent')}, window_id={res.get('window_id')})",
                }
            )
            if want_shot:
                time.sleep(0.5)
                _append_screenshot_blocks(blocks, monitor=monitor, window_id=res.get("window_id"))

        elif name == "computer_wait":
            secs = max(0.05, min(15.0, float(args.get("seconds", 0.8))))
            time.sleep(secs)
            blocks.append({"type": "text", "text": f"Waited {secs:.2f}s"})
            _append_screenshot_blocks(blocks, monitor=monitor, window_id=window_id)

        else:
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            }

        return {"content": blocks, "isError": False}

    except EmergencyStopError as stop_err:
        _STOP_TRIGGERED_AT = time.time()
        return {
            "content": [{"type": "text", "text": f"🛑 EMERGENCY STOP: {stop_err}"}],
            "isError": True,
        }
    except Exception as exc:
        sys.stderr.write(f"[agy-computer-use] Tool {name} error: {traceback.format_exc()}\n")
        return {
            "content": [{"type": "text", "text": f"Tool execution error: {exc}"}],
            "isError": True,
        }


def _send_jsonrpc(payload: Dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    _MCP_STDOUT.write(line + "\n")
    _MCP_STDOUT.flush()


def _decorate_modern_result(
    result: Dict[str, Any], params: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    if isinstance(params, dict):
        meta = params.get("_meta")
        if isinstance(meta, dict) and meta.get("io.modelcontextprotocol/protocolVersion") == "2026-07-28":
            result.setdefault("resultType", "complete")
            result.setdefault(
                "_meta",
                {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    }
                },
            )
    return result


def handle_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if req_id is None:
        return None

    if method == "initialize":
        requested_ver = str(params.get("protocolVersion") or "2025-06-18")
        negotiated = requested_ver if requested_ver in SUPPORTED_VERSIONS else "2025-06-18"
        res = {
            "protocolVersion": negotiated,
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
            "instructions": SERVER_INSTRUCTIONS,
        }
        return {"jsonrpc": "2.0", "id": req_id, "result": res}

    if method == "ping":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": _decorate_modern_result({}, params),
        }

    if method == "tools/list":
        res = _decorate_modern_result({"tools": TOOLS_SCHEMA}, params)
        return {"jsonrpc": "2.0", "id": req_id, "result": res}

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        tool_args = params.get("arguments") or {}
        if not isinstance(tool_args, dict):
            tool_args = {}
        call_res = _execute_tool(tool_name, tool_args)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": _decorate_modern_result(call_res, params),
        }

    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": _decorate_modern_result({"resources": []}, params),
        }

    if method == "resources/templates/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": _decorate_modern_result({"resourceTemplates": []}, params),
        }

    if method == "prompts/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": _decorate_modern_result({"prompts": []}, params),
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> None:
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8")
        except Exception:
            pass

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as err:
            _send_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {err}"},
                }
            )
            continue

        try:
            resp = handle_message(msg)
            if resp is not None:
                _send_jsonrpc(resp)
        except Exception as err:
            sys.stderr.write(f"[agy-computer-use] Fatal handler error: {traceback.format_exc()}\n")
            if isinstance(msg, dict) and msg.get("id") is not None:
                _send_jsonrpc(
                    {
                        "jsonrpc": "2.0",
                        "id": msg["id"],
                        "error": {"code": -32603, "message": f"Internal error: {err}"},
                    }
                )


if __name__ == "__main__":
    main()
