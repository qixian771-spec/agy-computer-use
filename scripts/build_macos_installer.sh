#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

WORK="${RUNNER_TEMP:-/tmp}/macbuild"
rm -rf "$WORK"

STAGE_DIR="$WORK/root/Library/Application Support/AGYComputerUse"
mkdir -p "$STAGE_DIR/app"
mkdir -p "$STAGE_DIR/bin"
mkdir -p "$WORK/scripts"
mkdir -p "$WORK/out"

echo "==> [1/5] Compiling Universal 2 (arm64 + x86_64) native macOS Swift HUD & AI Cursor binary..."
swiftc -O -target arm64-apple-macos12.0 \
  native/macos/agy_hud.swift \
  -o "$WORK/agy-hud-arm64" \
  -framework AppKit -framework QuartzCore -framework CoreGraphics

swiftc -O -target x86_64-apple-macos12.0 \
  native/macos/agy_hud.swift \
  -o "$WORK/agy-hud-x86_64" \
  -framework AppKit -framework QuartzCore -framework CoreGraphics

lipo -create "$WORK/agy-hud-arm64" "$WORK/agy-hud-x86_64" -output "$WORK/out/agy-hud-macos-universal"
chmod 755 "$WORK/out/agy-hud-macos-universal"
codesign --force --sign - "$WORK/out/agy-hud-macos-universal"
file "$WORK/out/agy-hud-macos-universal"

cp -f "$WORK/out/agy-hud-macos-universal" "$STAGE_DIR/bin/agy-hud"

echo "==> [2/5] Copying clean MCP engine payload..."
rsync -a \
  --exclude='.git' \
  --exclude='.github' \
  --exclude='.venv' \
  --exclude='scripts' \
  --exclude='tests' \
  --exclude='ui' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  ./ "$STAGE_DIR/app/"

echo "==> [3/5] Bundling self-contained Python 3.11 runtimes (arm64 + x86_64) + all wheels..."
uv python install --install-dir "$WORK/uv_arm64" --no-bin cpython-3.11-macos-aarch64-none
PY_ARM64_SRC="$(find "$WORK/uv_arm64" -mindepth 1 -maxdepth 1 -type d -name "cpython-*" | head -n 1)"
rsync -a "$PY_ARM64_SRC/" "$STAGE_DIR/py_arm64/"

PY_ARM64_BIN="$STAGE_DIR/py_arm64/bin/python3"
PY_ARM64_SITE="$("$PY_ARM64_BIN" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"

uv pip install \
  --python "$PY_ARM64_BIN" \
  --target "$PY_ARM64_SITE" \
  --python-platform aarch64-apple-darwin \
  -r requirements.txt

"$PY_ARM64_BIN" -c "import mss, PIL, pyautogui, pyperclip, requests; print('  Verified arm64 self-contained runtime OK!')"

uv python install --install-dir "$WORK/uv_x86_64" --no-bin cpython-3.11-macos-x86_64-none
PY_X64_SRC="$(find "$WORK/uv_x86_64" -mindepth 1 -maxdepth 1 -type d -name "cpython-*" | head -n 1)"
rsync -a "$PY_X64_SRC/" "$STAGE_DIR/py_x86_64/"

PY_X64_SITE="$(find "$STAGE_DIR/py_x86_64/lib" -maxdepth 3 -name "site-packages" -type d | head -n 1)"
uv pip install \
  --python "$PY_ARM64_BIN" \
  --target "$PY_X64_SITE" \
  --python-platform x86_64-apple-darwin \
  -r requirements.txt

for PYROOT in "$STAGE_DIR/py_arm64" "$STAGE_DIR/py_x86_64"; do
  rm -rf \
    "$PYROOT/lib/python3.11/test" \
    "$PYROOT/lib/python3.11/idlelib" \
    "$PYROOT/lib/python3.11/tkinter" \
    "$PYROOT/lib/python3.11/turtledemo" \
    "$PYROOT/lib/python3.11/ensurepip" \
    "$PYROOT/lib/python3.11/config-"*
  find "$PYROOT" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$PYROOT" -name "*.a" -type f -delete 2>/dev/null || true
done

echo "==> [4/5] Ad-hoc signing Mach-O binaries..."
find "$STAGE_DIR" -type f \( -name "*.dylib" -o -name "*.so" -o -perm -u+x \) -exec codesign --force --sign - {} \; 2>/dev/null || true

echo "==> [5/5] Building zero-clutter invisible Apple Installer (.pkg v3.0)..."
cat > "$WORK/scripts/postinstall" <<'POST'
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
CONSOLE_USER="$(stat -f "%Su" /dev/console)"
if [ -z "$CONSOLE_USER" ] || [ "$CONSOLE_USER" = "root" ]; then
  exit 0
fi
USER_HOME="$(dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory | awk '{print $2}')"
STAGE="/Library/Application Support/AGYComputerUse"

# Clean up any leftover v2.1 Web Console processes or desktop/app icons
pkill -f "console_server.py" 2>/dev/null || true
pkill -f "agy-hud" 2>/dev/null || true
rm -rf "/Applications/AGY Computer Use.app" \
       "$USER_HOME/Desktop/AGY Computer Use.app" \
       "$USER_HOME/Desktop/AGY 静默操控台.command" 2>/dev/null || true

xattr -cr "$STAGE" 2>/dev/null || true

ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ] && [ -x "$STAGE/py_arm64/bin/python3" ]; then
  PY_SRC="$STAGE/py_arm64"
else
  PY_SRC="$STAGE/py_x86_64"
fi

sudo -u "$CONSOLE_USER" -H bash -c "
  INSTALL_DIR=\"\$HOME/.gemini/agy-computer-use\"
  mkdir -p \"\$INSTALL_DIR/bin\" \"\$INSTALL_DIR/runtime\"
  rsync -a --delete --exclude='bin' --exclude='runtime' --exclude='.venv' '$STAGE/app/' \"\$INSTALL_DIR/\"
  cp -f '$STAGE/bin/agy-hud' \"\$INSTALL_DIR/bin/agy-hud\"
  chmod 755 \"\$INSTALL_DIR/bin/agy-hud\"
  rsync -a --delete '$PY_SRC/' \"\$INSTALL_DIR/runtime/\"
  \"\$INSTALL_DIR/runtime/bin/python3\" \"\$INSTALL_DIR/setup_agy.py\" --python-exe \"\$INSTALL_DIR/runtime/bin/python3\" --skip-doctor
  nohup \"\$INSTALL_DIR/bin/agy-hud\" --demo >/dev/null 2>&1 </dev/null &
" || true

rm -rf "$STAGE" 2>/dev/null || true
exit 0
POST
chmod 755 "$WORK/scripts/postinstall"

pkgbuild \
  --root "$WORK/root" \
  --scripts "$WORK/scripts" \
  --identifier "com.qixian.agy-computer-use.pkg" \
  --version "3.1.0" \
  --install-location "/" \
  "$WORK/component.pkg"

cat > "$WORK/welcome.html" <<'WELCOME'
<!DOCTYPE html><html><body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;padding:12px;">
<h2>⚡ AGY Computer Use v3.1（MacBook 灵动岛原生动画版）</h2>
<p>彻底替代底部毛玻璃胶囊！点击<b>「继续」</b>即可一键挂载到你的 Mac 顶部刘海灵动岛：</p>
<ul>
  <li><b>平时 100% 缩回刘海隐形</b>：直接集成进你的本地 Antigravity 对话框；</li>
  <li><b>60fps 弹性物理动画 + 律动声波</b>：AI 后台干活时从顶部刘海丝滑伸展，实时显示正在干嘛，鼠标悬停自动展开快捷抽屉；</li>
  <li><b>扫码 / 登录 / 验证 / 系统权限全接管</b>：遇到登录或验证时泛起琥珀流光弹出窗口，扫码完成自动缩回后台继续执行！</li>
</ul>
</body></html>
WELCOME

cat > "$WORK/distribution.xml" <<'DIST'
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>AGY Computer Use v3.1 (MacBook 灵动岛版)</title>
  <welcome file="welcome.html" mime-type="text/html"/>
  <options customize="never" require-scripts="true" hostArchitectures="x86_64,arm64"/>
  <choices-outline>
    <line choice="default">
      <line choice="com.qixian.agy-computer-use.pkg"/>
    </line>
  </choices-outline>
  <choice id="default"/>
  <choice id="com.qixian.agy-computer-use.pkg" visible="false">
    <pkg-ref id="com.qixian.agy-computer-use.pkg"/>
  </choice>
  <pkg-ref id="com.qixian.agy-computer-use.pkg" version="3.1.0" onConclusion="none">component.pkg</pkg-ref>
</installer-gui-script>
DIST

productbuild \
  --distribution "$WORK/distribution.xml" \
  --resources "$WORK" \
  --package-path "$WORK" \
  "$WORK/out/AGY-Computer-Use-Mac-Installer.pkg"

ls -lh "$WORK/out/"
echo "✅ v3.1 Universal MacBook Dynamic Island + Invisible .pkg built successfully!"

