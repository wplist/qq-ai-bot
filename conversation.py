"""会话记忆管理：按「群 / 私聊对象」独立保存对话历史。

内存中保存全部会话，每次变更后同步写入 data/sessions.json，
程序重启后自动恢复。history 超过上限轮数时丢弃最早的消息。
"""

import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ConversationStore:
    def __init__(self, max_turns: int, persist_path: Path | None):
        self._max_messages = max(1, max_turns) * 2  # 1轮 = user + assistant 两条
        self._persist_path = persist_path
        self._data: dict[str, list[dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()  # 保护 _locks 字典本身
        self._load()

    # ---------- 初始化 ----------

    def _load(self) -> None:
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {
                    k: [m for m in v if isinstance(m, dict) and "role" in m and "content" in m]
                    for k, v in raw.items()
                    if isinstance(v, list)
                }
                logger.info("已恢复 %d 个会话的历史记录", len(self._data))
        except (json.JSONDecodeError, OSError) as e:
            # 历史文件损坏时保留原文件另存，避免下次覆盖后无法排查
            try:
                self._persist_path.rename(self._persist_path.with_suffix(".broken.json"))
            except OSError:
                pass
            logger.warning("会话历史文件读取失败，已重置: %s", e)

    def _persist(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self._persist_path)
        except OSError as e:
            logger.warning("会话历史写入失败: %s", e)

    # ---------- 对外接口 ----------

    async def lock(self, key: str) -> asyncio.Lock:
        """获取某个会话的独立锁，保证同一会话串行处理。"""
        async with self._guard:
            return self._locks.setdefault(key, asyncio.Lock())

    def history(self, key: str) -> list[dict]:
        """返回会话历史的浅拷贝。调用方需先持有该会话的锁。"""
        return [dict(m) for m in self._data.get(key, [])]

    def append_pair(self, key: str, user_text: str, assistant_text: str) -> None:
        """追加一问一答并裁剪、持久化。调用方需先持有该会话的锁。"""
        msgs = self._data.setdefault(key, [])
        msgs.append({"role": "user", "content": user_text})
        msgs.append({"role": "assistant", "content": assistant_text})
        if len(msgs) > self._max_messages:
            del msgs[: len(msgs) - self._max_messages]
        self._persist()

    def clear(self, key: str) -> None:
        """清空某个会话的记忆。"""
        if key in self._data:
            del self._data[key]
            self._persist()

    def stats(self) -> tuple[int, int]:
        """返回 (会话数, 消息总数)。"""
        return len(self._data), sum(len(v) for v in self._data.values())
