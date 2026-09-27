# service.py
"""与消息事件无关的业务编排，供命令处理器和后台任务共用。"""

from __future__ import annotations

from typing import Any

from astrbot.api import AstrBotConfig

from .tools import FeeInfo, FeeQueryClient


class CquService:
    """电费查询编排：配置解析、连接池生命周期与查询调用。

    Args:
        config: 插件配置对象。
    """

    def __init__(self, config: AstrBotConfig) -> None:
        self._config = config
        self._client: FeeQueryClient | None = None

    @property
    def auth_token(self) -> str:
        """去除首尾空白的 synjones-auth 令牌，未配置时为空串。"""
        return (self._config.get("auth_token") or "").strip()

    async def start(self) -> FeeQueryClient:
        """创建可复用的 HTTP 连接池，重复调用不会重建。

        Returns:
            已就绪的查询客户端。
        """
        if self._client is None:
            self._client = FeeQueryClient(timeout=int(self._config.get("timeout", 10)))
        return self._client

    async def close(self) -> None:
        """关闭 HTTP 连接池。"""
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _cookies(self, campus: str) -> dict[str, str]:
        """把配置里对应校区的 cookie 字符串解析成 dict。

        Args:
            campus: 校区代号，"a" 或 "d"。

        Returns:
            cookie 名值对，解析失败时为空 dict。
        """
        raw = (self._config.get(campus, {}).get("cookie") or "").strip()
        result: dict[str, str] = {}
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    def _query_params(self, campus: str) -> dict[str, Any]:
        """组装对应校区的接口查询参数。

        Args:
            campus: 校区代号，"a" 或 "d"。

        Returns:
            传给 FeeQueryClient.query 的 feeitemid / fee_type / level。
        """
        return {
            "feeitemid": str(self._config.get(campus, {}).get("feeitemid", "448")),
            "fee_type": str(self._config.get("fee_type", "IEC")),
            "level": int(self._config.get("level", 2)),
        }

    async def query_room(self, room: str) -> FeeInfo:
        """查询房间电费，按房间号自动判定校区。

        Args:
            room: 房间号，如 "B4611"。

        Returns:
            解析后的电费信息。

        Raises:
            FeeQueryError: 网络错误、接口业务码异常或响应结构不合法。
        """
        client = self._client or await self.start()
        campus = client.detect_campus(room)
        return await client.query(
            campus=campus,
            room=room,
            auth_token=self.auth_token,
            cookies=self._cookies(campus),
            **self._query_params(campus),
        )
