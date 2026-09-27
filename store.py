# store.py
"""插件数据访问层：集中 KV key 约定与读写。

绑定数据以单个聚合文档存储，键为 QQ 号，值为该用户的绑定与偏好：:

    {"<qq>": {"room": "B4611", "session": "...", "remind": true, "threshold": 10.0}}

余额快照单独存放在另一个 key，按天追加、只保留最近若干天：:

    [{"date": "2026-09-27", "balances": {"<qq>": 12.34}}]

之所以不用「一用户一 key」，是因为插件 KV 没有枚举接口，定时任务需要一次性
拿到全部绑定用户。快照与绑定分开，是因为定时任务在查询完所有用户后才回写，
若与用户命令写入同一个 key，回写会覆盖期间发生的绑定操作。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from astrbot import logger
from astrbot.core import sp

if TYPE_CHECKING:
    from astrbot.api.star import Star

BINDINGS_KEY = "bindings"
"""聚合绑定文档的 KV key。"""

HISTORY_KEY = "history"
"""余额快照列表的 KV key。"""

HISTORY_KEEP = 2
"""快照保留天数，够算「昨日用电」即可。"""

DATE_FORMAT = "%Y-%m-%d"
"""快照日期格式，ISO 形式可直接按字符串比较大小。"""

LEGACY_ROOM_PREFIX = "user_room:"
"""旧版按用户存储绑定房间的 key 前缀，仅用于一次性迁移。"""


class Store:
    """插件 KV 读写封装。

    Args:
        plugin: 插件实例，需具备 PluginKVStoreMixin 提供的 KV 方法与 plugin_id。
    """

    def __init__(self, plugin: Star) -> None:
        self._plugin = plugin
        self._lock = asyncio.Lock()
        """保护绑定文档的读-改-写，避免并发绑定互相覆盖。"""

    async def get_bindings(self) -> dict[str, dict[str, Any]]:
        """读取全部用户的绑定与偏好。

        Returns:
            QQ 号到用户数据的映射，无数据时为空 dict。
        """
        return await self._plugin.get_kv_data(BINDINGS_KEY, {}) or {}

    async def get_binding(self, qq: str) -> dict[str, Any] | None:
        """读取单个用户的绑定与偏好。

        Args:
            qq: 用户 QQ 号。

        Returns:
            该用户的数据，未绑定时为 None。
        """
        return (await self.get_bindings()).get(qq)

    async def upsert_binding(self, qq: str, room: str, session: str = "") -> None:
        """写入或更新用户的绑定房间，保留已有的偏好字段。

        Args:
            qq: 用户 QQ 号。
            room: 房间号。
            session: 消息会话标识，用于后续主动推送，为空时不覆盖旧值。
        """
        async with self._lock:
            bindings = await self.get_bindings()
            entry = bindings.get(qq) or {}
            entry["room"] = room
            if session:
                entry["session"] = session
            bindings[qq] = entry
            await self._plugin.put_kv_data(BINDINGS_KEY, bindings)

    async def delete_binding(self, qq: str) -> bool:
        """删除用户的绑定。

        Args:
            qq: 用户 QQ 号。

        Returns:
            是否确实删除了已有绑定。
        """
        async with self._lock:
            bindings = await self.get_bindings()
            if qq not in bindings:
                return False
            del bindings[qq]
            await self._plugin.put_kv_data(BINDINGS_KEY, bindings)
            return True

    async def set_remind(
        self, qq: str, remind: bool, threshold: float, session: str = ""
    ) -> bool:
        """更新用户的余额提醒设置。

        Args:
            qq: 用户 QQ 号。
            remind: 是否开启余额提醒。
            threshold: 提醒阈值（元），需大于 0。
            session: 消息会话标识，为空时不覆盖旧值。开启提醒时传入当前会话，
                即可把提醒目标从绑定时所在会话切换到当前会话。

        Returns:
            是否更新成功；用户尚未绑定时返回 False。
        """
        async with self._lock:
            bindings = await self.get_bindings()
            entry = bindings.get(qq)
            if entry is None:
                return False
            entry["remind"] = remind
            entry["threshold"] = threshold
            if session:
                entry["session"] = session
            await self._plugin.put_kv_data(BINDINGS_KEY, bindings)
            return True

    # ---------- 余额快照 ----------
    async def get_history(self) -> list[dict[str, Any]]:
        """读取余额快照列表，按日期升序。

        Returns:
            形如 ``[{"date": "2026-09-27", "balances": {"<qq>": 12.34}}]``，
            无数据时为空列表。
        """
        return await self._plugin.get_kv_data(HISTORY_KEY, []) or []

    async def append_snapshot(self, balances: dict[str, float]) -> None:
        """把今天的余额快照追加进历史，并裁掉超出保留天数的旧快照。

        同一天重复调用会覆盖当天已有的快照。

        Args:
            balances: QQ 号到余额的映射。
        """
        today = datetime.now().strftime(DATE_FORMAT)
        async with self._lock:
            history = [e for e in await self.get_history() if e.get("date") != today]
            history.append({"date": today, "balances": balances})
            history.sort(key=lambda e: e.get("date", ""))
            await self._plugin.put_kv_data(HISTORY_KEY, history[-HISTORY_KEEP:])

    async def previous_snapshot(self) -> tuple[str, dict[str, float]] | None:
        """取今天之前最近一次快照，用于计算用电量。

        机器人停机导致中间缺天时，返回的是缺口之前那次快照，调用方应把日期
        一并展示，让用户知道用电量覆盖的是哪一段。

        Returns:
            ``(日期, 余额映射)``；今天之前没有任何快照时为 None。
        """
        today = datetime.now().strftime(DATE_FORMAT)
        for entry in reversed(await self.get_history()):
            if entry.get("date", "") < today:
                return entry["date"], entry.get("balances") or {}
        return None

    async def migrate_legacy_bindings(self) -> None:
        """把旧版 ``user_room:{qq}`` 形式的绑定并入聚合文档。

        迁移是幂等的：旧 key 不存在时直接返回，迁移后旧 key 会被删除。
        房间号会被补全到聚合文档，已存在的用户数据不会被覆盖。
        """
        try:
            prefs = await sp.range_get_async("plugin", self._plugin.plugin_id)
        except Exception as e:
            logger.warning(f"[cqu-astrcat] 读取旧版绑定失败，跳过迁移：{e}")
            return

        legacy: dict[str, dict[str, Any]] = {}
        for pref in prefs:
            if not pref.key.startswith(LEGACY_ROOM_PREFIX):
                continue
            value = (pref.value or {}).get("val")
            if isinstance(value, dict) and value.get("room"):
                legacy[pref.key] = value

        if not legacy:
            return

        async with self._lock:
            bindings = await self.get_bindings()
            for key, value in legacy.items():
                qq = key[len(LEGACY_ROOM_PREFIX) :]
                bindings.setdefault(qq, {}).setdefault("room", str(value["room"]))
            await self._plugin.put_kv_data(BINDINGS_KEY, bindings)

        for key in legacy:
            await self._plugin.delete_kv_data(key)

        logger.info(f"[cqu-astrcat] 已迁移 {len(legacy)} 条旧版房间绑定")
