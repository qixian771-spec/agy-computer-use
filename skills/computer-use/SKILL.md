---
name: computer-use
description: Native desktop & silent background browser/window automation for Antigravity (macOS, Windows & Linux) with MacBook Dynamic Island live telemetry & login/auth handoff. Use whenever the user asks to control their computer, open websites (Bilibili, Douyin, Xiaohongshu, GitHub, etc.), click UI buttons, type text into apps, inspect open windows, or watch live automation.
---

# AGY Computer Use v3.1 (MacBook Dynamic Island Edition)

You have native access to the **`computer-use`** MCP server (16 high-speed tools + **MacBook Notch Dynamic Island HUD**). Follow this protocol strictly for fluid, non-intrusive execution:

## 1. Default Silent Background Execution (Never Steal the User's Physical Mouse)
- **Websites / URLs**: Always start with `computer_open_silent(target="https://...", watch=False)`.
  - Launches an isolated CDP-attached Chrome/Edge background window (`--disable-backgrounding-occluded-windows`), binds to it, and streams live animated activity (`🌐 打开网页`, `🖱️ 点击元素`, `⌨️ 输入文字`) directly inside the **MacBook Dynamic Island** at the top of the screen!
- **Existing Desktop Apps**: Call `computer_list_windows` -> `computer_bind_window(target_window="...")`.
- **No Extra Dashboards**: Never open any web console UI; the MacBook Dynamic Island automatically animates while active and tucks completely back into the hardware notch when done.

## 2. Login / QR Scan / OAuth / CAPTCHA Handoff via Dynamic Island (`computer_wait_for_user_auth`)
- Whenever `computer_open_silent` or `computer_screenshot` reports `[AUTH/VERIFY GATE DETECTED]`, OR you visually see a login page, QR scan modal, SMS/2FA prompt, OAuth approval screen, or CAPTCHA / slider verification:
  1. **NEVER** attempt to solve CAPTCHAs, guess credentials, or bypass verification yourself!
  2. Immediately call **`computer_wait_for_user_auth(title="需要扫码登录...", reason="...", auto_pop_window=True)`**.
  3. The MacBook Dynamic Island will glow amber-gold with spring animations, pop the real window to the front for the user, and **automatically tuck the window back into silent background & return a fresh screenshot** as soon as the user scans/logs in or clicks **「✓ 我已完成，继续后台执行」** on the Dynamic Island!

## 3. High-Speed Batching & Element Targeting
- Prefer `[Detected Controls: [1]input:'搜索'@(510,145), ...]` (`element_index`) or read `[0, 1000]` coordinates from the cyan grid overlay.
- Chain predictable steps (`click` -> `type` -> `wait`) in a single **`computer_batch`** call for `<80ms` execution.
- If the user asks to watch live (`"切出来给我看看"`), call `computer_watch_window(watch=True)` (renders a 60fps dedicated AI pointer + click ripples on the real window), and `computer_watch_window(watch=False)` to tuck back behind Antigravity.
