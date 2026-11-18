#!/usr/bin/env python3
"""1-Click Auto-Configurator & System Doctor for AGY Computer Use (Windows + macOS).

Safely registers `computer-use` inside `~/.gemini/config/mcp_config.json`
without overwriting existing MCP servers, and verifies screen DPI calibration.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT_DIR = Path(__file__).resolve().parent
MCP_SERVER_SCRIPT = ROOT_DIR / "mcp_server.py"
SKILL_SOURCE_FILE = ROOT_DIR / "skills" / "computer-use" / "SKILL.md"


def get_mcp_config_paths(custom_path: Optional[Path] = None) -> List[Path]:
    """Return all official Antigravity MCP config paths (App + IDE + CLI)."""
    if custom_path is not None:
        return [custom_path]
    home = Path.home()
    paths = [
        # Official unified Antigravity App + IDE global path
        home / ".gemini" / "config" / "mcp_config.json",
        # Official Antigravity IDE legacy / macOS global path (always write both!)
        home / ".gemini" / "antigravity" / "mcp_config.json",
    ]
    cli_dir = home / ".gemini" / "antigravity-cli"
    if cli_dir.exists():
        paths.append(cli_dir / "mcp_config.json")
    return paths


def get_skill_target_paths(custom_roots: Optional[List[Path]] = None) -> List[Path]:
    """Return official Antigravity Global Skill paths so AGY natively understands silent workflow."""
    if custom_roots is not None:
        return [r / "computer-use" / "SKILL.md" for r in custom_roots]
    home = Path.home()
    return [
        home / ".gemini" / "config" / "skills" / "computer-use" / "SKILL.md",
        home / ".gemini" / "antigravity" / "skills" / "computer-use" / "SKILL.md",
    ]


def build_server_entry(python_exe: Optional[str] = None) -> Dict[str, Any]:
    py = python_exe or sys.executable
    env_cfg: Dict[str, str] = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    if sys.platform == "darwin":
        # Fix macOS Dock/Spotlight GUI apps missing Homebrew /usr/local paths
        env_cfg["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    return {
        "command": str(Path(py).resolve()),
        "args": [str(MCP_SERVER_SCRIPT.resolve())],
        "env": env_cfg,
        "timeoutSeconds": 300,
        "tools": {
            "computer_open_silent": {"eager": True},
            "computer_wait_for_user_auth": {"eager": True},
            "computer_batch": {"eager": True},
            "computer_list_windows": {"eager": True},
            "computer_bind_window": {"eager": True},
            "computer_watch_window": {"eager": True},
            "computer_get_window_elements": {"eager": True},
            "computer_screenshot": {"eager": True},
            "computer_click": {"eager": True},
            "computer_type": {"eager": True},
            "computer_key": {"eager": True},
            "computer_scroll": {"eager": True},
        },
    }


def check_agy_mcp_installed(custom_path: Optional[Path] = None) -> Dict[str, Any]:
    target = get_mcp_config_paths(custom_path)[0]
    skill_installed = (
        get_skill_target_paths()[0].exists() if custom_path is None else True
    )
    if not target.exists():
        return {"installed": False, "skill_installed": skill_installed, "config_path": str(target)}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        entry = servers.get("computer-use")
        return {
            "installed": isinstance(entry, dict) and bool(entry.get("command")),
            "skill_installed": skill_installed,
            "config_path": str(target),
            "entry": entry,
        }
    except Exception as exc:
        return {"installed": False, "skill_installed": skill_installed, "config_path": str(target), "error": str(exc)}


def install_agy_skill(custom_roots: Optional[List[Path]] = None) -> List[str]:
    installed: List[str] = []
    if not SKILL_SOURCE_FILE.is_file():
        return installed
    content = SKILL_SOURCE_FILE.read_text(encoding="utf-8")
    for dst in get_skill_target_paths(custom_roots):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        installed.append(str(dst))
    return installed


def install_agy_mcp_config(
    custom_path: Optional[Path] = None,
    python_exe: Optional[str] = None,
    skill_roots: Optional[List[Path]] = None,
) -> Dict[str, Any]:
    entry = build_server_entry(python_exe=python_exe)
    updated_files: List[str] = []

    for cfg_path in get_mcp_config_paths(custom_path):
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        existing: Dict[str, Any] = {}
        if cfg_path.exists():
            raw = cfg_path.read_text(encoding="utf-8").strip()
            if raw:
                backup = cfg_path.with_suffix(f".json.bak.{int(time.time())}")
                shutil.copy2(cfg_path, backup)
                try:
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        existing = loaded
                except json.JSONDecodeError:
                    existing = {}

        mcp_servers = existing.get("mcpServers")
        if not isinstance(mcp_servers, dict):
            mcp_servers = {}
            existing["mcpServers"] = mcp_servers

        mcp_servers["computer-use"] = entry
        cfg_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        updated_files.append(str(cfg_path))

    installed_skills: List[str] = []
    if custom_path is None or skill_roots is not None:
        installed_skills = install_agy_skill(skill_roots)

    return {
        "updated_files": updated_files,
        "installed_skills": installed_skills,
        "entry": entry,
    }


def run_doctor() -> bool:
    print("\n🔍 正在进行跨平台桌面环境、CDP 浏览器引擎与 DPI 校准自检...")
    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))

    from core.desktop_engine import ENGINE

    diag = ENGINE.get_system_diagnostics()
    browser_bin = ENGINE.window_manager._find_browser_binary()
    print(f"  • 操作系统   : {diag['os_name']} ({diag['platform']})")
    print(f"  • Python     : {sys.executable} (v{diag['python_version']})")
    print(f"  • CDP 浏览器 : {browser_bin or '未检测到 Chrome/Edge（将回退系统原生窗口模式）'}")

    if diag.get("gui_error"):
        print(f"  ⚠️ 图形桌面连接提示: {diag['gui_error']}")
        if sys.platform == "darwin":
            print(
                "  💡 macOS 首次使用请在「系统设置 -> 隐私与安全性」中开启：\n"
                "     1) 屏幕录制 (Screen Recording) -> 勾选 Antigravity 和 终端(Terminal)\n"
                "     2) 辅助功能 (Accessibility)    -> 勾选 Antigravity 和 终端(Terminal)"
            )
        return False

    for m in diag.get("monitors", []):
        print(
            f"  • 显示器 #{m['index']}: 物理捕获={m['width']}x{m['height']}, "
            f"逻辑点击={int(m['logical_width'])}x{int(m['logical_height'])}, "
            f"DPI缩放比={m['scale_x']:.2f}x"
        )

    try:
        shot = ENGINE.screenshot(monitor_index=1, draw_grid=True, draw_cursor=True)
        kb = len(shot.jpeg_base64) * 3 // 4 // 1024
        print(
            f"  ✅ 实时截屏自检成功！输出视觉尺寸={shot.image_width}x{shot.image_height} (~{kb} KB), "
            f"当前鼠标准星=({shot.cursor_norm_x}, {shot.cursor_norm_y})"
        )
        if sys.platform == "darwin":
            print(
                "  💡 温馨提示 (macOS): 若操控桌面原生软件时未响应，请确认「系统设置 -> 隐私与安全性 -> 辅助功能」已授权 Antigravity。"
            )
        return True
    except Exception as exc:
        print(f"  ⚠️ 截屏测试未通过: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure AGY Computer Use v2.1 Native MCP + Skill")
    parser.add_argument("--config-path", type=Path, default=None, help="Custom mcp_config.json path")
    parser.add_argument("--python-exe", type=str, default=None, help="Explicit Python executable path")
    parser.add_argument("--skip-doctor", action="store_true", help="Skip live screen capture check")
    args = parser.parse_args()

    res = install_agy_mcp_config(custom_path=args.config_path, python_exe=args.python_exe)
    print("=" * 68)
    print("  🎉 已完成本地 Antigravity 原生全层适配（MCP v2.1 + 全局 Skill）！")
    for f in res["updated_files"]:
        print(f"  📄 MCP 配置  : {f}")
    for s in res.get("installed_skills", []):
        print(f"  🧠 原生 Skill: {s}")
    print("=" * 68)

    if not args.skip_doctor:
        run_doctor()

    print("\n✨ 已启用 GPT 原生无感模式（v3.0）：")
    print("  • 平时 100% 隐形无打扰，无多余网页后台或桌面窗口；")
    print("  • 打开本地 Antigravity -> Customizations -> MCP Servers 点 Refresh（或重启一次 AGY）；")
    print("  • 直接在聊天框说：「帮我后台打开 B 站搜 AI 短视频，别抢我鼠标」；")
    print("  • AI 干活时底部自动浮出毛玻璃小胶囊：随时点「查看窗口」（带丝滑 AI 光标与点击涟漪）或「停止」！\n")


if __name__ == "__main__":
    main()

