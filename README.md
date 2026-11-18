# 🏝️ AGY Computer Use v3.1（MacBook 灵动岛原生动画版 · macOS & Windows）

专为 **Google Antigravity (AGY)** 打造的 **MacBook 刘海灵动岛 + 后台静默 Computer Use 引擎**——彻底替代底部毛玻璃胶囊与网页控制台，平时 **100% 缩回 MacBook 刘海隐形**，干活时液态弹性伸展，遇到扫码登录或权限验证自动流光接管！

---

## ✨ v3.1 核心体感

1. **🏝️ 硬件级融合 MacBook 灵动岛 (`native/macos/agy_hud.swift`)**
   - 自动识别 MacBook 刘海几何尺寸（`safeAreaInsets` + `auxiliaryTopLeftArea/RightArea` 凹弧顶耳无缝贴合黑色边框；外接显示器自动切换顶部悬浮灵动岛）。
   - **60fps 二阶弹簧物理形变 (`SpringValue`) + 五段渐变声波律动**：实时丝滑播报 AI 正在干嘛（打开网页、点击按钮、输入内容、滚动浏览），干完活闪过翠绿 `✓` 后像水滴一样缩回刘海彻底消失！
2. **🔐 登录 / 扫码 / 验证码 / 系统权限全接管 (`computer_wait_for_user_auth`)**
   - 后台自动检测登录页、扫码弹窗、安全滑块验证与 macOS 屏幕录制权限。
   - 一旦需要你配合，**灵动岛泛起琥珀金流光并弹性下拉接管卡片**，自动把真实登录窗口弹到前台；你扫完码登录成功（或点一下灵动岛 **「✓ 我已完成，继续后台执行」**），窗口瞬间缩回后台隐身，AI 无缝继续任务！
3. **👁️ 悬停交互抽屉 + 60fps 丝滑 AI 专属光标**
   - 鼠标移到灵动岛即可弹性展开快捷按钮（**查看真实窗口 / 切回后台隐身 / 我来接管 / 停止**）；
   - 查看真实窗口时呈现 60fps 蓝色 AI 专属光标与点击涟漪，**全程零抢夺你的物理鼠标**！

---

## 🚀 一句话升级 / 安装到 v3.1

### 方式 A：在本地 Mac AGY 会话里一句话自动升级（推荐）
```text
帮我拉取更新 https://github.com/qixian772/agy-computer-use 到 ~/.gemini/agy-computer-use 并执行 bash install_mac.command 升级到 v3.1 MacBook 灵动岛动画版（替代旧版底部毛玻璃胶囊）。
```

### 方式 B：Mac 零命令行图形安装包 (`.pkg`)
前往 [Releases v3.1.0](https://github.com/qixian772/agy-computer-use/releases/tag/v3.1.0) 下载 **`AGY-Computer-Use-Mac-Installer.pkg`** 双击安装即可。
