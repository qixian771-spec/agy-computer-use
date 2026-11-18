"""Dual-protocol Vision ReAct Autonomous Agent v2.0 (Silent Window + Turbo Batch).

Supports any 3rd-party API Relay (NewAPI / OneAPI / OpenRouter / Official):
- OpenAI Compatible (`/v1/chat/completions` + `/v1/models`)
- Anthropic Native (`/v1/messages` + `/v1/models`)
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from core.desktop_engine import DesktopEngine, ENGINE, EmergencyStopError, ScreenshotResult


CONFIG_PATH = Path.home() / ".gemini" / "agy-computer-use" / "config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "protocol": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o",
    "max_steps": 15,
    "monitor": 1,
    "step_delay": 0.2,
    "execution_mode": "silent_window",
}


def load_saved_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if v is not None})
        except Exception:
            pass
    return cfg


def save_config(new_cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_saved_config()
    for key in (
        "protocol",
        "base_url",
        "api_key",
        "model",
        "max_steps",
        "monitor",
        "step_delay",
        "execution_mode",
    ):
        if key in new_cfg and new_cfg[key] is not None:
            cfg[key] = new_cfg[key]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return cfg


def normalize_api_base(raw_url: str) -> str:
    url = (raw_url or "").strip().rstrip("/")
    if not url:
        return "https://api.openai.com/v1"
    for suffix in ("/chat/completions", "/messages", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    if not re.search(r"/v\d+(?:beta)?(?:/openai)?$", url):
        url = url + "/v1"
    return url


def fetch_models(base_url: str, api_key: str, protocol: str = "openai") -> List[str]:
    base = normalize_api_base(base_url)
    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        if protocol == "anthropic":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"

    resp = requests.get(f"{base}/models", headers=headers, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    raw_list = payload.get("data") if isinstance(payload, dict) else payload
    models: List[str] = []
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
            elif isinstance(item, str):
                models.append(item)
    return sorted(set(models))


def test_api_connection(
    base_url: str, api_key: str, model: str, protocol: str = "openai"
) -> Dict[str, Any]:
    t0 = time.time()
    base = normalize_api_base(base_url)
    if not api_key:
        raise ValueError("请先填写 API Key")
    if not model:
        raise ValueError("请先选择或输入模型名称")

    if protocol == "anthropic":
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Reply with OK only."}],
        }
        r = requests.post(f"{base}/messages", headers=headers, json=body, timeout=20)
        r.raise_for_status()
        data = r.json()
        reply = ""
        for blk in data.get("content", []):
            if isinstance(blk, dict) and blk.get("type") == "text":
                reply += blk.get("text", "")
    else:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        body = {
            "model": model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Reply with OK only."}],
        }
        r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=20)
        r.raise_for_status()
        data = r.json()
        reply = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )

    ms = int(round((time.time() - t0) * 1000))
    return {"ok": True, "latency_ms": ms, "reply": str(reply).strip()[:80], "normalized_base": base}


AGENT_SYSTEM_PROMPT = """你是 QIXIAN 本地极速桌面智能体 v2.0（支持「独立窗口静默执行」与「连招极速批处理」）。
你可以看到目标窗口（或全屏桌面）的实时截图（四周绘有 0..1000 归一化青色刻度尺，青色/红色准星代表当前 AI 虚拟鼠标位置，绿色圆圈代表上一步点击位置），并通过输出单个 JSON 对象操控完成任务。

【极速与静默优先原则（重要！）】
1. 静默开窗优先：若当前尚未绑定目标窗口，需要打开网页或软件时，优先使用 `open` 动作（例如 `{"thought": "...", "action": "open", "target": "https://www.bilibili.com"}`），系统会自动在后台新开独立窗口静默执行，绝不抢用户物理鼠标！若要操作已打开的某个窗口，可用 `bind_window` 绑定它。
2. 连招批处理提速（`batch`）：只要能连续确定 2~5 个动作（例如：点击输入框 -> 输入关键词 -> 按回车，或连续滚动），请优先输出 `batch` 一次性连招执行，速度提升 3 倍！
3. 坐标系规则：所有坐标 x, y 均为 [0, 1000] 的整数，左上角=(0,0)，正中心=(500,500)，右下角=(1000,1000)。严格对照截图边缘的 100..900 刻度尺读取目标正中心。

【安全与合规铁律】
- 严禁尝试破解、点击或绕过任何验证码（CAPTCHA / 人机验证 / 滑块）、密码框或支付确认！遇到此类界面，立即输出 `{"thought": "...", "action": "ask_user", "question": "检测到登录/验证码/支付确认，请切出窗口手动完成后再继续"}`。

【输出格式要求（仅输出一个合法 JSON 对象）】
1. ⚡ 极速连招批处理（强烈推荐用于“点击+输入+回车”等组合动作）：
   {"thought": "点击搜索框输入关键词并回车搜索", "action": "batch", "actions": [
     {"action": "click", "x": 480, "y": 150},
     {"action": "type", "text": "AI 短视频", "clear_first": true, "press_enter": true},
     {"action": "wait", "seconds": 0.6}
   ]}
2. 静默打开网址或本地软件（自动绑定后台窗口）：
   {"thought": "...", "action": "open", "target": "https://github.com/qixian772"}
3. 绑定已打开的窗口静默操控：
   {"thought": "...", "action": "bind_window", "target_window": "Chrome"}
4. 单步点击/双击/右键：
   {"thought": "...", "action": "click", "x": 500, "y": 320, "button": "left", "clicks": 1}
5. 单步输入文字（支持中英文，后台静默直注不占剪贴板）：
   {"thought": "...", "action": "type", "x": 450, "y": 280, "text": "要输入的内容", "clear_first": false, "press_enter": true}
6. 快捷键或单键（enter, esc, tab, ctrl+l, cmd+t）：
   {"thought": "...", "action": "key", "keys": "ctrl+l"}
7. 滚轮滚动：
   {"thought": "...", "action": "scroll", "direction": "down", "ticks": 6, "x": 500, "y": 500}
8. 拖拽：
   {"thought": "...", "action": "drag", "start_x": 200, "start_y": 300, "end_x": 600, "end_y": 300}
9. 等待加载：
   {"thought": "...", "action": "wait", "seconds": 1.2}
10. 需要人工介入：
   {"thought": "...", "action": "ask_user", "question": "请手动完成验证后再继续"}
11. 任务已完成：
   {"thought": "...", "action": "done", "summary": "已成功完成任务总结"}
"""


def _extract_json_object(raw_text: str) -> Dict[str, Any]:
    text = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("Model output is not a JSON object")
    return obj


class AutonomousAgentRunner:
    """Manages background ReAct execution loop (Silent Window + Foreground Live)."""

    def __init__(self, engine: DesktopEngine = ENGINE) -> None:
        self.engine = engine
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self.status: str = "idle"  # idle | running | completed | paused | stopped | error
        self.goal: str = ""
        self.steps: List[Dict[str, Any]] = []
        self.summary: str = ""
        self.error: str = ""
        self.latest_screenshot_b64: str = ""
        self.latest_target: Optional[Dict[str, Any]] = None
        self.current_step_num: int = 0
        self.max_steps: int = 15

    def get_state_snapshot(self, include_screenshot: bool = True) -> Dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "goal": self.goal,
                "current_step": self.current_step_num,
                "max_steps": self.max_steps,
                "summary": self.summary,
                "error": self.error,
                "latest_target": self.latest_target,
                "steps": list(self.steps),
                "session": self.engine.get_session_state(),
                "latest_screenshot": self.latest_screenshot_b64 if include_screenshot else "",
            }

    def stop(self) -> None:
        self.engine.trigger_emergency_stop("User clicked Stop")
        with self._lock:
            if self.status == "running":
                self.status = "stopped"
                self.summary = "已手动触发紧急急停。"

    def start_task(self, goal: str, cfg: Dict[str, Any]) -> None:
        with self._lock:
            if self.status == "running":
                raise RuntimeError("当前已有任务正在执行，请先停止当前任务。")
            self.engine.reset_emergency_stop()
            if cfg.get("execution_mode") in ("silent_window", "foreground_live"):
                self.engine.set_execution_mode(str(cfg["execution_mode"]))
            if cfg.get("window_id") is not None:
                wid_val = str(cfg["window_id"]).strip()
                if wid_val:
                    self.engine.bind_window(wid_val)

            self.status = "running"
            self.goal = goal.strip()
            self.steps = []
            self.summary = ""
            self.error = ""
            self.latest_target = None
            self.current_step_num = 0
            self.max_steps = max(1, min(50, int(cfg.get("max_steps", 15))))

        t = threading.Thread(
            target=self._run_loop, args=(goal.strip(), dict(cfg)), daemon=True
        )
        self._thread = t
        t.start()

    def _call_vlm(
        self,
        goal: str,
        history_notes: List[str],
        prev_shot_b64: Optional[str],
        curr_shot: ScreenshotResult,
        cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        base = normalize_api_base(str(cfg.get("base_url", "")))
        api_key = str(cfg.get("api_key", "")).strip()
        model = str(cfg.get("model", "")).strip()
        protocol = str(cfg.get("protocol", "openai")).lower()

        if not api_key or not model:
            raise ValueError("请先在右上角「⚙️ 中转站 API 配置」中填写 API Key 和视觉模型！")

        session = self.engine.get_session_state()
        if session["bound_window"]:
            bw = session["bound_window"]
            scope_desc = f"已锁定静默窗口 #{bw['window_id']}「{bw['display_name']}」（后台执行不抢鼠标）"
            elems = self.engine.get_window_elements(bw["window_id"])[:12]
            elem_text = (
                "；窗口内检测到的控件："
                + ", ".join(f"[{e['index']}]{e['role']}:'{e['name']}'@({e['x']},{e['y']})" for e in elems)
                if elems
                else ""
            )
            scope_desc += elem_text
        else:
            wins = self.engine.list_windows()[:8]
            win_list_str = (
                "；当前桌面已打开的窗口列表："
                + ", ".join(f"#{w['window_id']}:{w['display_name']}" for w in wins)
                if wins
                else ""
            )
            scope_desc = f"当前未锁定特定窗口（模式={session['execution_mode']}）{win_list_str}"

        history_text = (
            "\n".join(history_notes[-10:]) if history_notes else "（尚无历史动作，这是第 1 步）"
        )
        user_prompt = (
            f"【用户总任务】：{goal}\n"
            f"【当前操控范围】：{scope_desc}\n"
            f"【已执行步骤记录】：\n{history_text}\n"
            f"【当前准星坐标】：({curr_shot.cursor_norm_x}, {curr_shot.cursor_norm_y})。"
            f"请观察最新截图，优先使用 open（后台开窗）或 batch（多步极速连招），输出下一步 JSON："
        )

        curr_shot_b64 = curr_shot.jpeg_base64

        if protocol == "anthropic":
            content_blocks: List[Dict[str, Any]] = []
            if prev_shot_b64:
                content_blocks.append({"type": "text", "text": "上一步截图（供对比变化）："})
                content_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": prev_shot_b64,
                        },
                    }
                )
            content_blocks.append({"type": "text", "text": user_prompt})
            content_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": curr_shot_b64,
                    },
                }
            )
            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "Authorization": f"Bearer {api_key}",
                "anthropic-version": "2023-06-01",
            }
            body = {
                "model": model,
                "max_tokens": 700,
                "temperature": 0.1,
                "system": AGENT_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": content_blocks}],
            }
            resp = requests.post(f"{base}/messages", headers=headers, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            raw_reply = ""
            for blk in data.get("content", []):
                if isinstance(blk, dict) and blk.get("type") == "text":
                    raw_reply += blk.get("text", "")
        else:
            oai_content: List[Dict[str, Any]] = []
            if prev_shot_b64:
                oai_content.append({"type": "text", "text": "上一步截图（供对比变化）："})
                oai_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{prev_shot_b64}"},
                    }
                )
            oai_content.append({"type": "text", "text": user_prompt})
            oai_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{curr_shot_b64}"},
                }
            )
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }
            body = {
                "model": model,
                "max_tokens": 700,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                    {"role": "user", "content": oai_content},
                ],
            }
            resp = requests.post(
                f"{base}/chat/completions", headers=headers, json=body, timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
            raw_reply = (
                data.get("choices", [{}])[0].get("message", {}).get("content", "")
            )
            if isinstance(raw_reply, list):
                raw_reply = "".join(
                    part.get("text", "") for part in raw_reply if isinstance(part, dict)
                )

        return _extract_json_object(str(raw_reply))

    def _run_loop(self, goal: str, cfg: Dict[str, Any]) -> None:
        monitor = int(cfg.get("monitor", 1) or 1)
        step_delay = max(0.08, min(3.0, float(cfg.get("step_delay", 0.2))))
        history_notes: List[str] = []
        prev_shot_b64: Optional[str] = None

        try:
            curr_shot = self.engine.screenshot(
                monitor_index=monitor, draw_grid=True, draw_cursor=True
            )
            with self._lock:
                self.latest_screenshot_b64 = curr_shot.jpeg_base64

            for step_idx in range(1, self.max_steps + 1):
                self.engine.check_not_stopped()
                with self._lock:
                    self.current_step_num = step_idx

                decision = self._call_vlm(
                    goal=goal,
                    history_notes=history_notes,
                    prev_shot_b64=prev_shot_b64,
                    curr_shot=curr_shot,
                    cfg=cfg,
                )
                prev_shot_b64 = curr_shot.jpeg_base64
                self.engine.check_not_stopped()

                action = str(decision.get("action", "")).lower().strip()
                thought = str(decision.get("thought", "")).strip()

                step_record: Dict[str, Any] = {
                    "step": step_idx,
                    "thought": thought,
                    "action": action,
                    "params": decision,
                    "time": time.strftime("%H:%M:%S"),
                }

                if action == "done":
                    summary = str(decision.get("summary") or thought or "任务已完成！")
                    step_record["result"] = summary
                    with self._lock:
                        self.steps.append(step_record)
                        self.status = "completed"
                        self.summary = summary
                        self.latest_target = None
                    return

                if action == "ask_user":
                    question = str(
                        decision.get("question") or thought or "需要您手动操作确认后继续。"
                    )
                    # Automatically surface silent window if user needs to solve login/CAPTCHA!
                    if self.engine.bound_window_id:
                        self.engine.set_watch_window(watch=True)
                    step_record["result"] = question
                    with self._lock:
                        self.steps.append(step_record)
                        self.status = "paused"
                        self.summary = question
                        self.latest_target = None
                    return

                # Highlight target coordinates on live console
                if "x" in decision and "y" in decision and decision["x"] is not None:
                    with self._lock:
                        self.latest_target = {
                            "x": float(decision["x"]),
                            "y": float(decision["y"]),
                            "action": action,
                        }

                exec_msg = self._dispatch_action(action, decision, monitor=monitor)
                step_record["result"] = exec_msg
                history_notes.append(
                    f"Step {step_idx}: thought='{thought}' -> action={action} ({exec_msg})"
                )

                with self._lock:
                    self.steps.append(step_record)

                time.sleep(step_delay)
                # Capture once after action settles — reused directly for next step!
                curr_shot = self.engine.screenshot(
                    monitor_index=monitor, draw_grid=True, draw_cursor=True
                )
                with self._lock:
                    self.latest_screenshot_b64 = curr_shot.jpeg_base64

            with self._lock:
                self.status = "completed"
                self.summary = f"已达到最大步数限制（{self.max_steps} 步），自动暂停。"

        except EmergencyStopError as stop_err:
            with self._lock:
                self.status = "stopped"
                self.summary = str(stop_err)
        except Exception as exc:
            with self._lock:
                self.status = "error"
                self.error = str(exc)

    def _dispatch_action(
        self, action: str, d: Dict[str, Any], monitor: int = 1
    ) -> str:
        if action == "batch":
            sub_actions = d.get("actions") or []
            res = self.engine.execute_batch(sub_actions, monitor_index=monitor)
            return f"极速连招执行 {res.get('executed_count', 0)} 步 ({res.get('elapsed_ms', 0)}ms)"

        if action == "bind_window":
            target_win = str(d.get("target_window") or "").strip()
            res = self.engine.bind_window(target_win)
            if not res.get("ok"):
                return f"窗口绑定失败: {res.get('error')}"
            bw = res.get("bound_window") or {}
            return f"已静默锁定窗口 #{res.get('bound_window_id')} ({bw.get('display_name', '')})"

        if action == "click":
            res = self.engine.click(
                x=float(d["x"]) if d.get("x") is not None else None,
                y=float(d["y"]) if d.get("y") is not None else None,
                button=str(d.get("button", "left")),
                clicks=int(d.get("clicks", 1)),
                monitor_index=monitor,
                element_index=int(d["element_index"]) if d.get("element_index") is not None else None,
            )
            tag = "静默点击" if res.get("silent") else "点击"
            return f"{tag} ({res['x']}, {res['y']}) [{res['button']} x{res['clicks']}]"

        if action == "type":
            res = self.engine.type_text(
                text=str(d.get("text", "")),
                x=float(d["x"]) if d.get("x") is not None else None,
                y=float(d["y"]) if d.get("y") is not None else None,
                clear_first=bool(d.get("clear_first", False)),
                press_enter=bool(d.get("press_enter", False)),
                monitor_index=monitor,
                element_index=int(d["element_index"]) if d.get("element_index") is not None else None,
            )
            tag = "静默输入" if res.get("silent") else "输入"
            return f"{tag} {res['typed_length']} 字符 (回车={res['press_enter']})"

        if action == "key":
            res = self.engine.press_key(str(d.get("keys", "")))
            return f"按键 {res.get('keys')}"

        if action == "scroll":
            res = self.engine.scroll(
                direction=str(d.get("direction", "down")),
                ticks=int(d.get("ticks", 5)),
                x=float(d["x"]) if d.get("x") is not None else None,
                y=float(d["y"]) if d.get("y") is not None else None,
                monitor_index=monitor,
            )
            return f"滚动 {res['direction']} ({res['ticks']} 格)"

        if action == "drag":
            res = self.engine.drag(
                start_x=float(d["start_x"]),
                start_y=float(d["start_y"]),
                end_x=float(d["end_x"]),
                end_y=float(d["end_y"]),
                monitor_index=monitor,
            )
            return f"拖拽至 ({res['end_x']}, {res['end_y']})"

        if action == "move":
            res = self.engine.move(
                x=float(d["x"]), y=float(d["y"]), monitor_index=monitor
            )
            return f"移动准星至 ({res['x']}, {res['y']})"

        if action == "open":
            res = self.engine.open_target(str(d.get("target", "")))
            time.sleep(0.45)
            if res.get("window_id"):
                return f"已在后台静默新开窗口 #{res['window_id']} -> {res.get('target')}"
            return f"打开 {res.get('target')}"

        if action == "wait":
            secs = max(0.1, min(10.0, float(d.get("seconds", 0.8))))
            time.sleep(secs)
            return f"等待 {secs:.1f} 秒"

        raise ValueError(f"未知的动作指令: {action}")


AGENT_RUNNER = AutonomousAgentRunner(ENGINE)
