#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/.gemini/agy-computer-use"
HUD_BIN="$INSTALL_DIR/bin/agy-hud"

echo "================================================================"
echo "  ⚡ AGY Computer Use v3.1 — MacBook 灵动岛原生动画版安装/升级"
echo "================================================================"

# 1. 彻底杀掉旧版底部毛玻璃胶囊进程 & 清理旧版网页操控台残留
pkill -f "console_server.py" 2>/dev/null || true
pkill -f "agy-hud" 2>/dev/null || true
rm -rf "$HOME/Desktop/AGY Computer Use.app" \
       "$HOME/Desktop/AGY 静默操控台.command" \
       "$INSTALL_DIR/console-app-shell" \
       "$INSTALL_DIR/console_server.py" \
       "$INSTALL_DIR/ui" \
       "$INSTALL_DIR/start_mac.command" \
       "$INSTALL_DIR/start_win.bat" \
       "$INSTALL_DIR/launch_app_win.vbs" 2>/dev/null || true

# 若旧版 /Applications/AGY Computer Use.app 存在，先复用里面现成的独立 Python 运行时再彻底删除
ARCH="$(uname -m)"
OLD_APP="/Applications/AGY Computer Use.app"
mkdir -p "$INSTALL_DIR/bin"

if [ -d "$OLD_APP" ]; then
  if [ "$ARCH" = "arm64" ] && [ -x "$OLD_APP/Contents/Resources/py_arm64/bin/python3" ] && [ ! -x "$INSTALL_DIR/runtime/bin/python3" ]; then
    echo "♻️  复用已安装的独立 Python 运行时（免重新下载依赖）..."
    mkdir -p "$INSTALL_DIR/runtime"
    rsync -a "$OLD_APP/Contents/Resources/py_arm64/" "$INSTALL_DIR/runtime/"
  elif [ -x "$OLD_APP/Contents/Resources/py_x86_64/bin/python3" ] && [ ! -x "$INSTALL_DIR/runtime/bin/python3" ]; then
    mkdir -p "$INSTALL_DIR/runtime"
    rsync -a "$OLD_APP/Contents/Resources/py_x86_64/" "$INSTALL_DIR/runtime/"
  fi
  rm -rf "$OLD_APP" 2>/dev/null || true
fi

# 2. 同步最新 v3.1 核心引擎到 ~/.gemini/agy-computer-use
if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
  rsync -a \
    --exclude='.git' \
    --exclude='.github' \
    --exclude='.venv' \
    --exclude='runtime' \
    --exclude='bin' \
    --exclude='__pycache__' \
    "$SRC_DIR/" "$INSTALL_DIR/"
fi

# 3. 安装或本机编译 v3.1 MacBook 灵动岛动画引擎 (替代旧版毛玻璃胶囊！)
rm -f "$HUD_BIN"
if [ -x "$SRC_DIR/bin/agy-hud" ] && [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
  cp -f "$SRC_DIR/bin/agy-hud" "$HUD_BIN"
elif curl -fsSL --connect-timeout 8 "https://github.com/qixian772/agy-computer-use/releases/download/v3.1.0/agy-hud-macos-universal" -o "$HUD_BIN.tmp" 2>/dev/null || \
     curl -fsSL --connect-timeout 8 "https://github.com/qixian772/agy-computer-use/releases/latest/download/agy-hud-macos-universal" -o "$HUD_BIN.tmp" 2>/dev/null; then
  mv -f "$HUD_BIN.tmp" "$HUD_BIN"
elif command -v swiftc >/dev/null 2>&1 && [ -f "$INSTALL_DIR/native/macos/agy_hud.swift" ]; then
  echo "🔨 正在使用本机 Swift 编译原生 MacBook 灵动岛引擎..."
  swiftc -O "$INSTALL_DIR/native/macos/agy_hud.swift" -o "$HUD_BIN" \
    -framework AppKit -framework QuartzCore -framework CoreGraphics
fi

if [ -f "$HUD_BIN" ]; then
  chmod 755 "$HUD_BIN"
  xattr -cr "$HUD_BIN" 2>/dev/null || true
  codesign --force --sign - "$HUD_BIN" 2>/dev/null || true
fi

# 4. 绑定本地 Antigravity 原生 MCP (16 Tools) + /computer-use 技能
cd "$INSTALL_DIR"
if [ -x "$INSTALL_DIR/runtime/bin/python3" ]; then
  TARGET_PY="$INSTALL_DIR/runtime/bin/python3"
  xattr -cr "$INSTALL_DIR/runtime" 2>/dev/null || true
else
  if ! python3 -c "import sys, venv; assert sys.version_info >= (3, 9)" >/dev/null 2>&1; then
    echo "❌ 未检测到 Python 3.9+，请直接双击 Release v3.1.0 中的 AGY-Computer-Use-Mac-Installer.pkg 安装！"
    exit 1
  fi
  [ -x ".venv/bin/python" ] || python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt -q || \
    .venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
  TARGET_PY="$INSTALL_DIR/.venv/bin/python"
fi

"$TARGET_PY" "$INSTALL_DIR/setup_agy.py" --python-exe "$TARGET_PY" --skip-doctor

# 5. 从屏幕顶部刘海丝滑弹出 4.5 秒「灵动岛动态律动 + 授权接管」演示动画，随后自动缩回刘海隐形！
if [ -x "$HUD_BIN" ]; then
  nohup "$HUD_BIN" --demo >/dev/null 2>&1 </dev/null &
fi

echo ""
echo "================================================================"
echo "  ✅ 已成功切换为 MacBook 灵动岛模式（v3.1，已彻底替代底部毛玻璃胶囊）！"
echo "  • 看一眼屏幕正上方刘海：灵动岛正在为你播放弹性律动与登录接管演示；"
echo "  • 平时 100% 缩回刘海隐形，AI 干活时实时律动播报，遇到扫码/登录自动流光弹出；"
echo "  • 在本地 Antigravity -> Customizations -> MCP Servers 点一次 Refresh（或重启 AGY）即可！"
echo "================================================================"
