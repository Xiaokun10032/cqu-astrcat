# service.py
"""与消息事件无关的业务编排，供命令处理器和后台任务共用。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api import AstrBotConfig, logger

from .store import Store
from .tools import FeeInfo, FeeQueryClient, FeeQueryError

DEFAULT_THRESHOLD = 10.0
"""余额提醒的默认阈值（元）。"""


@dataclass
class Usage:
    """自某次快照以来的用电量。"""

    spent: float
    since: str


@dataclass
class BalanceAlert:
    """一条待推送的余额不足提醒。"""

    qq: str
    session: str
    amount: float
    threshold: float
    usage: Usage | None


class CquService:
    """电费查询编排：配置解析、连接池生命周期、查询与日结计算。

    Args:
        config: 插件配置对象。
        store: 插件数据访问层，用于读取历史快照。
    """

    def __init__(self, config: AstrBotConfig, store: Store) -> None:
        self._config = config
        self._store = store
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

    async def usage_since_snapshot(self, qq: str, current: float) -> Usage | None:
        """计算自上次快照以来的用电量。

        Args:
            qq: 用户 QQ 号。
            current: 本次查询到的余额。

        Returns:
            用电量；没有可用快照，或快照里没有该用户时为 None。
        """
        previous = await self._store.previous_snapshot()
        if previous is None:
            return None
        date, balances = previous
        if qq not in balances:
            return None
        return Usage(spent=balances[qq] - current, since=date)

    async def snapshot_all(
        self, bindings: dict[str, dict[str, Any]]
    ) -> dict[str, float]:
        """逐个查询所有已绑定用户的余额。

        Args:
            bindings: 全部绑定数据，键为 QQ 号。

        Returns:
            QQ 号到余额的映射。查询失败或余额无法解析的用户会被跳过，
            避免个别房间的问题拖垮整批快照。
        """
        balances: dict[str, float] = {}
        for qq, entry in bindings.items():
            room = (entry or {}).get("room")
            if not room:
                continue
            try:
                info = await self.query_room(room)
            except FeeQueryError as e:
                logger.warning(f"[cqu-astrcat] 快照查询失败 qq={qq}：{e}")
                continue
            amount = info.amount_value
            if amount is None:
                logger.warning(
                    f"[cqu-astrcat] 快照余额无法解析 qq={qq}：{info.amount!r}"
                )
                continue
            balances[qq] = amount
        return balances

    def build_alerts(
        self,
        balances: dict[str, float],
        bindings: dict[str, dict[str, Any]],
        previous: tuple[str, dict[str, float]] | None,
    ) -> list[BalanceAlert]:
        """挑出余额低于阈值且开启了提醒的用户。

        Args:
            balances: 本次快照的余额。
            bindings: 全部绑定数据。
            previous: 上次快照 ``(日期, 余额映射)``，用于附带用电量，可为 None。

        Returns:
            待推送的提醒列表；未开启提醒或缺少会话标识的用户会被跳过。
        """
        alerts: list[BalanceAlert] = []
        for qq, amount in balances.items():
            entry = bindings.get(qq) or {}
            if not entry.get("remind"):
                continue
            session = entry.get("session")
            if not session:
                continue
            threshold = float(entry.get("threshold", DEFAULT_THRESHOLD))
            if amount >= threshold:
                continue
            usage = None
            if previous is not None and qq in previous[1]:
                usage = Usage(spent=previous[1][qq] - amount, since=previous[0])
            alerts.append(
                BalanceAlert(
                    qq=qq,
                    session=session,
                    amount=amount,
                    threshold=threshold,
                    usage=usage,
                )
            )
        return alerts
