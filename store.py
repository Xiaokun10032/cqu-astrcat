# store.py
"""插件数据访问层：集中 KV key 约定与读写。

绑定数据以单个聚合文档存储，键为 QQ 号，值为该用户的绑定与偏好：:

    {"<qq>": {"room": "B4611", "session": "...", "remind": true, "threshold": 10.0}}

之所以不用「一用户一 key」，是因为插件 KV 没有枚举接口，定时任务需要一次性
拿到全部绑定用户；聚合文档还能让「保留两天快照」这类需求只动一个 key。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from astrbot import logger
from astrbot.core import sp

if TYPE_CHECKING:
    from astrbot.api.star import Star

BINDINGS_KEY = "bindings"
"""聚合绑定文档的 KV key。"""

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
