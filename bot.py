"""QQ AI 聊天机器人（NapCat + GLM-5.3）。

作为 WebSocket 客户端连接 NapCat 的 OneBot v11 正向 WebSocket，
收到群聊 @ 消息 / 好友私聊消息后调用 GLM-5.3 生成回复并发回。

用法：
    python bot.py                # 正常运行
    python bot.py --check-glm    # 只自检智谱 API Key 是否可用
    python bot.py --config 其他配置.toml
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import websockets

from conversation import ConversationStore
from glm_client import GLMClient, GLMError

# PyInstaller 打包后以 exe 所在目录为项目根（保证 data/ 与 config.toml 跟着 exe 走）
ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
DATA_DIR = ROOT / "data"

# 群聊里 @ 机器人但没说别的内容时的固定回复
EMPTY_AT_REPLY = "在的在的，直接说你想聊的吧～"
# 群聊会话追加到 system prompt 后的场景说明
GROUP_CONTEXT_NOTE = (
    "\n\n[当前场景] 你正在一个QQ群聊中，不同用户的消息以「昵称: 内容」的格式出现。"
    "你回复时不要添加任何昵称前缀，直接说内容本身。"
)

HELP_TEXT = (
    "我是AI聊天机器人，直接跟我说话就行～\n"
    "可用命令：\n"
    "  /帮助 - 查看本帮助\n"
    "  /清空记忆 - 清空当前对话的记忆（私聊人人可用，群聊仅管理员）\n"
    "  /性格 - 查看当前人格设定（仅管理员）\n"
    "  /性格 <描述> - 修改人格，如：/性格 你是一只傲娇的猫娘（仅管理员）\n"
    "  /性格 重置 - 恢复配置文件里的默认人格（仅管理员）\n"
    "  /状态 - 查看机器人运行状态（仅管理员）"
)


class ApiError(Exception):
    """OneBot API 调用失败。"""


# ---------------------------------------------------------------- CQ 码处理

CQ_PATTERN = re.compile(r"\[CQ:([^,\]]+)(?:,[^\]]*)?\]")
# GLM-5.3 仅支持文本，非文本 CQ 码替换成占位符
CQ_PLACEHOLDER = {
    "image": "[图片]",
    "face": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "json": "[卡片消息]",
    "forward": "[合并转发]",
    "xml": "[卡片消息]",
    "music": "[音乐分享]",
    "redbag": "[红包]",
    "poke": "[戳一戳]",
    "share": "[链接分享]",
    "location": "[位置]",
}
CQ_AT_QQ = re.compile(r"\[CQ:at,qq=(\d+)")

def cq_to_text(raw: str) -> str:
    """把 CQ 码字符串转成送给模型的纯文本。"""

    def repl(m: re.Match) -> str:
        tag = m.group(1)
        if tag == "at":
            at = CQ_AT_QQ.match(m.group(0))
            return f"@{at.group(1)}" if at else "@某人"
        return CQ_PLACEHOLDER.get(tag, "")

    return CQ_PATTERN.sub(repl, raw)


# ---------------------------------------------------------------- 配置加载

def load_config(path: Path) -> dict:
    import tomllib

    if not path.exists():
        sys.exit(
            f"未找到配置文件 {path}\n"
            f"请先复制 config.example.toml 为 config.toml 并填写配置（详见 README.md）"
        )
    with open(path, "rb") as f:
        cfg = tomllib.load(f)

    for section in ("napcat", "glm", "persona", "behavior", "launcher"):
        cfg.setdefault(section, {})
    napcat, glm, persona, behavior = cfg["napcat"], cfg["glm"], cfg["persona"], cfg["behavior"]

    napcat.setdefault("ws_url", "ws://127.0.0.1:3001")
    napcat.setdefault("access_token", "")
    glm.setdefault("base_url", "https://open.bigmodel.cn/api/paas/v4")
    glm.setdefault("model", "glm-5.3")
    glm.setdefault("reasoning_effort", "low")
    glm.setdefault("max_tokens", 4096)
    glm.setdefault("timeout", 120)
    persona.setdefault("name", "小智")
    persona.setdefault(
        "system_prompt", "你是{name}，一个友善的AI聊天助手，用轻松口语化的风格简洁回答问题。"
    )
    launcher_default = str(ROOT / "NapCat" / "shell")
    cfg["launcher"].setdefault("napcat_dir", launcher_default)
    if not str(cfg["launcher"]["napcat_dir"]).strip():
        cfg["launcher"]["napcat_dir"] = launcher_default
    behavior.setdefault("group_enabled", True)
    behavior.setdefault("private_enabled", True)
    behavior.setdefault("private_whitelist", [])
    behavior.setdefault("admin_users", [])
    behavior.setdefault("max_history_turns", 20)
    behavior.setdefault("reply_prefix", "")
    behavior.setdefault("segment_length", 3000)

    if not str(glm.get("api_key", "")).strip():
        sys.exit("配置错误：[glm] api_key 不能为空，请填写智谱开放平台的 API Key")
    if not str(napcat["ws_url"]).startswith(("ws://", "wss://")):
        sys.exit("配置错误：[napcat] ws_url 必须以 ws:// 或 wss:// 开头")

    persona["system_prompt"] = persona["system_prompt"].replace("{name}", str(persona["name"]))
    return cfg


def apply_runtime_overrides(cfg: dict) -> dict:
    """控制台保存过的配置（data/runtime.json）优先于 config.toml。"""
    p = DATA_DIR / "runtime.json"
    if not p.exists():
        return cfg
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return cfg
    if isinstance(r.get("napcat"), dict):
        cfg["napcat"].update({k: v for k, v in r["napcat"].items() if k in ("ws_url", "access_token")})
    if isinstance(r.get("glm"), dict) and str(r["glm"].get("api_key", "")).strip():
        cfg["glm"]["api_key"] = str(r["glm"]["api_key"])
    if isinstance(r.get("admins"), list):
        cfg["behavior"]["admin_users"] = [int(a) for a in r["admins"] if str(a).strip().isdigit()]
    return cfg


def setup_logging() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    # Windows 控制台默认 GBK，强制 UTF-8 防止中文日志乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(DATA_DIR / "bot.log", encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------- 机器人主体

class QQGLMBot:
    def __init__(self, cfg: dict, glm: GLMClient, store: ConversationStore):
        self.cfg = cfg
        self.glm = glm
        self.store = store
        self.ws = None
        self.self_id: int | None = None
        self.self_name = ""
        self._pending: dict[str, asyncio.Future] = {}  # echo -> API 响应 Future
        self._start_time = time.time()
        self._persona_file = DATA_DIR / "persona.json"  # 聊天命令改的人格持久化在这里
        self.loop: asyncio.AbstractEventLoop | None = None
        self._reconnect_event: asyncio.Event | None = None
        self._apply_persona()

    # ---------- 人格管理 ----------

    def _apply_persona(self) -> None:
        """优先使用 /性格 命令保存的覆盖人格，否则用 config.toml 里的。"""
        default = self.cfg["persona"]
        name, prompt = default["name"], default["system_prompt"]
        if self._persona_file.exists():
            try:
                data = json.loads(self._persona_file.read_text(encoding="utf-8"))
                name = data.get("name", name)
                prompt = data.get("system_prompt", prompt)
            except (json.JSONDecodeError, OSError):
                logging.warning("人格覆盖文件读取失败，使用配置文件中的人格")
        self.persona_name = str(name)
        self.system_prompt = str(prompt).replace("{name}", self.persona_name)

    def _save_persona(self, prompt: str) -> None:
        try:
            DATA_DIR.mkdir(exist_ok=True)
            self._persona_file.write_text(
                json.dumps({"name": self.persona_name, "system_prompt": prompt}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            logging.warning("人格覆盖文件写入失败: %s", e)

    # ---------- WebSocket 连接 ----------

    @staticmethod
    def _ws_connect(url: str, token: str):
        headers = [("Authorization", f"Bearer {token}")] if token else None
        # websockets>=14 改名 additional_headers，旧版是 extra_headers
        try:
            return websockets.connect(url, additional_headers=headers, open_timeout=10, max_size=2**23)
        except TypeError:
            return websockets.connect(url, extra_headers=headers, open_timeout=10, max_size=2**23)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        logging.info("正在连接 NapCat: %s", self.cfg["napcat"]["ws_url"])
        delay = 1.0
        while True:
            url = self.cfg["napcat"]["ws_url"]
            token = self.cfg["napcat"]["access_token"]
            reconnect_requested = False
            try:
                async with self._ws_connect(url, token) as ws:
                    self.ws = ws
                    delay = 1.0
                    self._reconnect_event = asyncio.Event()
                    # 必须先启动接收循环，_call_api 的应答才能被取到
                    recv_task = asyncio.create_task(self._recv_loop(ws))
                    reconnect_task = asyncio.create_task(self._reconnect_event.wait())
                    try:
                        await self._on_connected()
                        done, pending = await asyncio.wait(
                            {recv_task, reconnect_task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        for t in pending:
                            t.cancel()
                        if reconnect_task in done and recv_task not in done:
                            reconnect_requested = True
                            logging.info("管理界面更新了 NapCat 连接配置，正在重连…")
                        elif recv_task in done:
                            await recv_task  # 断连时重抛异常
                    finally:
                        recv_task.cancel()
                        reconnect_task.cancel()
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.WebSocketException) as e:
                logging.error(
                    "无法连接 NapCat（%s）：%s。请确认 NapCat 已启动、正向WebSocket地址/端口/token 正确，"
                    "5 秒内若地址不通可尝试 ws://127.0.0.1:3001/onebot/v11/ws 形式",
                    url, e,
                )
            finally:
                self.ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ApiError("连接已断开"))
                self._pending.clear()
            if not reconnect_requested:
                logging.info("%d 秒后重试……", int(delay))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    def request_reconnect(self) -> None:
        """供管理 API 线程调用：让 run() 主循环用最新配置立即重连 NapCat。"""
        ev, loop = self._reconnect_event, self.loop
        if ev is not None and loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(ev.set)

    async def _on_connected(self) -> None:
        info = await self._call_api("get_login_info", {})
        self.self_id = int(info["user_id"])
        self.self_name = info.get("nickname", "")
        logging.info("已连接 NapCat，机器人账号：%s（%s）", self.self_id, self.self_name)

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    continue
            except json.JSONDecodeError:
                logging.warning("收到无法解析的消息: %r", raw[:200])
                continue
            if "post_type" in obj:
                if obj.get("post_type") == "message":
                    # 事件处理放后台任务，避免单条消息阻塞接收循环
                    asyncio.create_task(self._handle_message(obj))
            elif obj.get("echo"):
                fut = self._pending.pop(obj["echo"], None)
                if fut and not fut.done():
                    fut.set_result(obj)

    # ---------- OneBot API 调用 ----------

    async def _call_api(self, action: str, params: dict, timeout: float = 30) -> dict:
        if self.ws is None:
            raise ApiError("尚未连接到 NapCat")
        echo = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        try:
            await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
            resp = await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, ConnectionError, websockets.WebSocketException) as e:
            raise ApiError(f"调用 OneBot 接口 {action} 失败: {e}") from e
        finally:
            self._pending.pop(echo, None)
        if resp.get("retcode") != 0:
            raise ApiError(f"OneBot 接口 {action} 返回错误: retcode={resp.get('retcode')} {resp.get('wording')}")
        return resp.get("data") or {}

    # ---------- 消息入口 ----------

    async def _handle_message(self, ev: dict) -> None:
        try:
            sender = ev.get("sender") or {}
            uid = sender.get("user_id")
            if uid is None or self.self_id is not None and uid == self.self_id:
                return
            if sender.get("anonymous"):  # 匿名消息拿不到真实身份，跳过
                return
            raw = ev.get("raw_message") or ""
            if ev.get("message_type") == "group":
                await self._handle_group(ev, raw, sender)
            elif ev.get("message_type") == "private":
                await self._handle_private(ev, raw, sender)
        except Exception:
            logging.exception("处理消息时出现未预期的异常（event_id=%s）", ev.get("message_id"))

    async def _handle_group(self, ev: dict, raw: str, sender: dict) -> None:
        behavior = self.cfg["behavior"]
        if not behavior["group_enabled"]:
            return
        gid = ev.get("group_id")
        uid = sender.get("user_id")
        if gid is None:
            return
        at_me = re.compile(r"\[CQ:at,qq=%s(?:,[^\]]*)?\]" % self.self_id)
        if not at_me.search(raw):
            return
        text = cq_to_text(at_me.sub("", raw)).strip()
        nickname = sender.get("card") or sender.get("nickname") or str(uid)

        async def reply(msg: str) -> None:
            await self._send_text(True, gid, msg)

        if text.startswith("/"):
            await self._handle_command(text, f"group:{gid}", True, uid, reply)
            return
        if not text:
            await reply(EMPTY_AT_REPLY)
            return

        key = f"group:{gid}"
        prompt = f"{nickname}: {text}"
        async with await self.store.lock(key):
            messages = self._build_messages(key, group=True)
            messages.append({"role": "user", "content": prompt})
            answer = await self._ask_glm(messages)
            self.store.append_pair(key, prompt, answer)
        await reply(answer)

    async def _handle_private(self, ev: dict, raw: str, sender: dict) -> None:
        behavior = self.cfg["behavior"]
        if not behavior["private_enabled"]:
            return
        if ev.get("sub_type") == "system":
            return
        uid = sender.get("user_id")
        if uid is None:
            return
        whitelist = behavior["private_whitelist"]
        if whitelist and uid not in whitelist:
            logging.debug("私聊用户 %s 不在白名单，已忽略", uid)
            return
        text = cq_to_text(raw).strip()

        async def reply(msg: str) -> None:
            await self._send_text(False, uid, msg)

        if text.startswith("/"):
            await self._handle_command(text, f"private:{uid}", False, uid, reply)
            return
        if not text:
            await reply("我只能看懂文字消息哦，发点文字过来吧～")
            return

        key = f"private:{uid}"
        async with await self.store.lock(key):
            messages = self._build_messages(key, group=False)
            messages.append({"role": "user", "content": text})
            answer = await self._ask_glm(messages)
            self.store.append_pair(key, text, answer)
        await reply(answer)

    # ---------- 对话与回复 ----------

    def _build_messages(self, key: str, group: bool) -> list[dict]:
        system = self.system_prompt
        if group:
            system += GROUP_CONTEXT_NOTE
        return [{"role": "system", "content": system}] + self.store.history(key)

    async def _ask_glm(self, messages: list[dict]) -> str:
        try:
            return await self.glm.chat(messages)
        except GLMError as e:
            logging.warning("GLM 回复失败: %s", e)
            return "（AI 出了点小问题，请稍后再试）"

    async def _send_text(self, is_group: bool, target_id: int, text: str) -> None:
        full = self.cfg["behavior"]["reply_prefix"] + text
        seg_len = max(200, int(self.cfg["behavior"]["segment_length"]))
        chunks = [full[i : i + seg_len] for i in range(0, len(full), seg_len)]
        for i, chunk in enumerate(chunks):
            try:
                if is_group:
                    await self._call_api(
                        "send_group_msg",
                        {"group_id": target_id, "message": [{"type": "text", "data": {"text": chunk}}]},
                    )
                else:
                    await self._call_api(
                        "send_private_msg",
                        {"user_id": target_id, "message": [{"type": "text", "data": {"text": chunk}}]},
                    )
            except ApiError as e:
                logging.error("发送消息失败(target=%s): %s", target_id, e)
                return
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)  # 分段发送间隔，降低风控风险

    # ---------- 命令 ----------

    async def _handle_command(
        self,
        text: str,
        session_key: str,
        is_group: bool,
        user_id: int,
        reply,
    ) -> None:
        body = text[1:].strip()
        cmd, _, arg = body.partition(" ")
        cmd = cmd.lower()
        arg = arg.strip()
        admins = self.cfg["behavior"]["admin_users"]
        if cmd in ("帮助", "help", ""):
            await reply(HELP_TEXT)
        elif cmd in ("清空记忆", "重置记忆", "重置", "reset"):
            if is_group and user_id not in admins:
                await reply("群里清空的是整群记忆，只有管理员可以操作哦")
            else:
                self.store.clear(session_key)
                await reply("记忆已清空，我们重新开始吧～")
        elif cmd in ("性格", "人格", "persona"):
            if user_id not in admins:
                await reply("只有管理员可以查看或修改人格哦")
            elif not arg:
                await reply(f"当前人格名：{self.persona_name}\n当前设定：\n{self.system_prompt}")
            elif arg in ("重置", "恢复", "reset"):
                self._persona_file.unlink(missing_ok=True)
                self._apply_persona()
                await reply("人格已恢复为配置文件里的默认设定")
            else:
                self.system_prompt = arg.replace("{name}", self.persona_name)
                self._save_persona(arg)
                await reply("人格已更新，接下来就按新设定来聊啦～")
        elif cmd in ("状态", "status"):
            if user_id not in admins:
                await reply("只有管理员可以查看状态哦")
            else:
                sessions, msg_count = self.store.stats()
                mins = int(time.time() - self._start_time) // 60
                b = self.cfg["behavior"]
                await reply(
                    f"运行状态\n"
                    f"- 已运行：{mins // 60}小时{mins % 60}分钟\n"
                    f"- 机器人账号：{self.self_id}（{self.self_name}）\n"
                    f"- 人格：{self.persona_name}"
                    f"{'（聊天命令修改过）' if self._persona_file.exists() else ''}\n"
                    f"- 模型：{self.cfg['glm']['model']}（effort={self.cfg['glm']['reasoning_effort']}）\n"
                    f"- 活跃会话：{sessions} 个，共 {msg_count} 条记忆\n"
                    f"- 群聊回复：{'开' if b['group_enabled'] else '关'} / 私聊回复：{'开' if b['private_enabled'] else '关'}"
                )
        else:
            await reply(f"未知命令 /{cmd}，发送 /帮助 查看可用命令")

    # ---------- 管理控制台支持（被 AdminAPI 调用，注意线程安全） ----------

    def status_payload(self) -> dict:
        glm = self.cfg["glm"]
        sessions, msg_count = self.store.stats()
        return {
            "ok": True,
            "connected": self.ws is not None,
            "bot_qq": self.self_id,
            "bot_name": self.self_name,
            "uptime_min": int(time.time() - self._start_time) // 60,
            "sessions": sessions,
            "messages": msg_count,
            "group_enabled": bool(self.cfg["behavior"]["group_enabled"]),
            "private_enabled": bool(self.cfg["behavior"]["private_enabled"]),
            "admins": list(self.cfg["behavior"]["admin_users"]),
            "persona": {"name": self.persona_name, "customized": self._persona_file.exists()},
            "napcat": {
                "ws_url": self.cfg["napcat"]["ws_url"],
                "token_masked": mask_secret(self.cfg["napcat"]["access_token"], 4),
            },
            "glm": {
                "model": glm["model"],
                "effort": glm["reasoning_effort"],
                "base_url": glm["base_url"],
                "key_masked": mask_secret(glm["api_key"]),
            },
        }

    def apply_settings(self, data: dict) -> dict:
        """保存控制台提交的配置（立即生效），并持久化到 data/runtime.json。"""
        runtime = self._load_runtime()
        changed = []
        nap = data.get("napcat") or {}
        if str(nap.get("ws_url", "")).strip().startswith(("ws://", "wss://")):
            self.cfg["napcat"]["ws_url"] = str(nap["ws_url"]).strip()
            runtime.setdefault("napcat", {})["ws_url"] = self.cfg["napcat"]["ws_url"]
            changed.append("napcat.ws_url")
        if nap.get("access_token") is not None:
            self.cfg["napcat"]["access_token"] = str(nap["access_token"]).strip()
            runtime.setdefault("napcat", {})["access_token"] = self.cfg["napcat"]["access_token"]
            changed.append("napcat.access_token")
        glm_in = data.get("glm") or {}
        if str(glm_in.get("api_key", "")).strip():
            self.cfg["glm"]["api_key"] = str(glm_in["api_key"]).strip()
            runtime.setdefault("glm", {})["api_key"] = self.cfg["glm"]["api_key"]
            self._rebuild_glm()
            changed.append("glm.api_key")
        for field in ("base_url", "model"):
            v = str(glm_in.get(field, "")).strip()
            if not v:
                continue
            if field == "base_url" and not v.startswith(("http://", "https://")):
                logging.warning("忽略非法 base_url: %s", v)
                continue
            self.cfg["glm"][field] = v
            runtime.setdefault("glm", {})[field] = v
            self._rebuild_glm()
            changed.append(f"glm.{field}")
        if isinstance(data.get("admins"), list):
            admins = [int(a) for a in data["admins"] if str(a).strip().isdigit()]
            self.cfg["behavior"]["admin_users"] = admins
            runtime["admins"] = admins
            changed.append("admins")
        if changed:
            self._save_runtime(runtime)
        if any(c.startswith("napcat.") for c in changed):
            self.request_reconnect()
        logging.info("管理控制台更新配置: %s", ", ".join(changed) or "无变化")
        return {"ok": True, "changed": changed}

    def test_glm(self, data: dict) -> dict:
        """按请求里的 base_url/model/api_key 组合测试（缺省项沿用当前配置）。"""
        d = data or {}
        g = self.cfg["glm"]
        key = str(d.get("api_key", "")).strip() or g["api_key"]
        base = str(d.get("base_url", "")).strip() or g["base_url"]
        model = str(d.get("model", "")).strip() or g["model"]

        async def run_test() -> tuple[str, float]:
            client = GLMClient(key, base, model, g["reasoning_effort"], g["max_tokens"], 60)
            t0 = time.time()
            return await client.ping(), round(time.time() - t0, 1)

        try:
            reply, secs = asyncio.run_coroutine_threadsafe(run_test(), self.loop).result(timeout=120)
            return {"ok": True, "reply": reply, "seconds": secs, "model": model}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    def test_napcat(self, data: dict) -> dict:
        nap = data or {}
        url = str(nap.get("ws_url", "")).strip() or self.cfg["napcat"]["ws_url"]
        token = nap["access_token"] if nap.get("access_token") is not None else self.cfg["napcat"]["access_token"]

        async def run_test() -> dict:
            async with self._ws_connect(url, str(token)) as ws:
                await ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": "probe"}))
                while True:
                    resp = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if resp.get("echo") == "probe":
                        d = resp.get("data") or {}
                        return {
                            "ok": resp.get("retcode") == 0,
                            "qq": d.get("user_id"),
                            "nickname": d.get("nickname"),
                        }

        try:
            return asyncio.run_coroutine_threadsafe(run_test(), self.loop).result(timeout=25)
        except Exception as e:
            return {"ok": False, "message": f"连接失败: {e}"}

    def _load_runtime(self) -> dict:
        p = DATA_DIR / "runtime.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_runtime(self, runtime: dict) -> None:
        try:
            DATA_DIR.mkdir(exist_ok=True)
            (DATA_DIR / "runtime.json").write_text(
                json.dumps(runtime, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        except OSError as e:
            logging.warning("runtime.json 写入失败: %s", e)

    def _rebuild_glm(self) -> None:
        g = self.cfg["glm"]
        self.glm = GLMClient(
            g["api_key"], g["base_url"], g["model"], g["reasoning_effort"], g["max_tokens"], g["timeout"]
        )


# ---------------------------------------------------------------- 管理控制台 API

def mask_secret(s: str, keep: int = 8) -> str:
    s = str(s or "")
    if not s:
        return ""
    return (s[:keep] + "****") if len(s) > keep else "****"


class AdminAPI:
    """机器人内部管理 API。

    监听 127.0.0.1 的系统随机端口（不占用固定端口，退出即释放），
    实际端口写入「插件目录/api.port」与「data/api.port」，
    由 NapCat WebUI 插件（6099 同源路由）转发访问，浏览器不直接接触。
    """

    def __init__(self, bot: QQGLMBot):
        self.bot = bot
        self.server = None
        self.port = 0

    def start(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        bot = self.bot

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path.startswith("/api/status"):
                    self._json(200, bot.status_payload())
                else:
                    self._json(404, {"ok": False, "message": "not found"})

            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                except (json.JSONDecodeError, ValueError):
                    return self._json(400, {"ok": False, "message": "请求体不是合法 JSON"})
                try:
                    if self.path == "/api/settings":
                        self._json(200, bot.apply_settings(data))
                    elif self.path == "/api/test-glm":
                        self._json(200, bot.test_glm(data))
                    elif self.path == "/api/test-napcat":
                        self._json(200, bot.test_napcat(data))
                    else:
                        self._json(404, {"ok": False, "message": "not found"})
                except Exception as e:  # 控制台不能被单次异常打死
                    logging.exception("管理接口处理异常")
                    self._json(500, {"ok": False, "message": str(e)})

            def log_message(self, fmt, *args):  # 静默默认访问日志，避免刷屏
                pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as e:
            logging.error("管理 API 启动失败: %s", e)
            return
        self.port = self.server.server_address[1]
        self._write_port_files()
        threading.Thread(target=self.server.serve_forever, daemon=True, name="admin-api").start()
        logging.info("管理 API 已就绪（内部随机端口 %d，经 WebUI 插件同源转发）", self.port)

    def _write_port_files(self) -> None:
        """把随机端口写到两处：插件目录（WebUI 插件转发用）、data/（启动器检测用）。"""
        plugin_dir = (
            Path(self.bot.cfg["launcher"]["napcat_dir"])
            / "plugins" / "napcat-plugin-qq-ai-bot"
        )
        for target in (plugin_dir / "api.port", DATA_DIR / "api.port"):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(self.port), encoding="ascii")
            except OSError as e:
                # 失步会导致启动器误判，绝不能静默吞掉
                logging.warning("api.port 写入失败（%s）: %s", target, e)


# ---------------------------------------------------------------- 入口

async def check_glm(cfg: dict) -> None:
    glm = GLMClient(
        api_key=cfg["glm"]["api_key"],
        base_url=cfg["glm"]["base_url"],
        model=cfg["glm"]["model"],
        reasoning_effort=cfg["glm"]["reasoning_effort"],
        max_tokens=cfg["glm"]["max_tokens"],
        timeout=cfg["glm"]["timeout"],
    )
    print(f"正在调用 {cfg['glm']['model']} 自检（约需几秒）……")
    try:
        reply = await glm.ping()
    except GLMError as e:
        sys.exit(f"✗ 自检失败：{e}\n  请检查 api_key 是否正确、网络是否可达 open.bigmodel.cn")
    print(f"✓ 自检成功，模型回复：{reply}")


def _api_alive_on(port: int) -> bool:
    """探测指定端口是否已有机器人实例在服务。"""
    if port <= 0:
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1.5) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def _existing_instance_port(cfg: dict) -> int:
    """读取两个 api.port 文件并探测，返回已在服务的端口（无则 0）。"""
    candidates = []
    plugin_file = (
        Path(cfg["launcher"]["napcat_dir"]) / "plugins" / "napcat-plugin-qq-ai-bot" / "api.port"
    )
    for f in (DATA_DIR / "api.port", plugin_file):
        try:
            v = int(f.read_text(encoding="ascii").strip())
            if v > 0 and v not in candidates:
                candidates.append(v)
        except (OSError, ValueError):
            continue
    for port in candidates:
        if _api_alive_on(port):
            return port
    return 0


async def async_main(cfg: dict) -> None:
    # 单实例保护：已有实例在服务时直接退出，避免多实例重复回复消息
    existing = _existing_instance_port(cfg)
    if existing:
        logging.info("已有机器人实例在运行（端口 %d），本进程退出", existing)
        print(f"已有机器人实例在运行（端口 {existing}），本进程退出")
        return
    glm = GLMClient(
        api_key=cfg["glm"]["api_key"],
        base_url=cfg["glm"]["base_url"],
        model=cfg["glm"]["model"],
        reasoning_effort=cfg["glm"]["reasoning_effort"],
        max_tokens=cfg["glm"]["max_tokens"],
        timeout=cfg["glm"]["timeout"],
    )
    store = ConversationStore(
        max_turns=cfg["behavior"]["max_history_turns"], persist_path=DATA_DIR / "sessions.json"
    )
    bot = QQGLMBot(cfg, glm, store)
    AdminAPI(bot).start()
    await bot.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="QQ AI 聊天机器人（NapCat + GLM-5.3）")
    parser.add_argument("--config", default=str(ROOT / "config.toml"), help="配置文件路径")
    parser.add_argument("--check-glm", action="store_true", help="只自检智谱 API Key 是否可用")
    args = parser.parse_args()

    setup_logging()
    cfg = apply_runtime_overrides(load_config(Path(args.config)))
    if args.check_glm:
        asyncio.run(check_glm(cfg))
    else:
        try:
            asyncio.run(async_main(cfg))
        except KeyboardInterrupt:
            print("\n再见～")


if __name__ == "__main__":
    main()
