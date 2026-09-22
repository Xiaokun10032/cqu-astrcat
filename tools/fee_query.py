# tools/fee_query.py
import re
from dataclasses import dataclass, field
from typing import Optional, Union, Literal

import httpx

Campus = Literal["a", "d"]


class FeeQueryError(Exception):
    """电费查询失败，主插件只需要捕获这一种异常"""

    pass


@dataclass
class FeeInfo:
    """结构化的电费信息，方便主插件使用"""

    room: str = ""
    amount: str = ""  # 剩余金额（A 区为现金余额）
    e_subsidy: str = ""  # 电剩余补助（A 区为补贴余额）
    w_subsidy: str = ""  # 水剩余补助（A 区不返回）
    e_meter_addr: str = ""  # 电表地址
    e_meter_value: str = ""  # 电表读数
    w_meter_addr: str = ""  # 水表地址
    w_meter_value: str = ""  # 水表读数
    e_price: float = -1  # 电价（仅 A 区返回，-1 表示未知）
    account_id: str = ""
    raw: dict = field(default_factory=dict)

    def to_text(self) -> str:
        # lines = [f"🏠 房间：{self.room}"]
        lines = [f"🏠 房间：*****"]
        if self.amount:
            lines.append(f"💰 剩余金额：{self.amount} 元")
        if self.e_price >= 0:
            lines.append(f"⚡ 电价：{self.e_price} 元/度")
        if self.e_meter_addr:
            lines.append(f"⚡ 电表：{self.e_meter_addr} 读数 {self.e_meter_value}")
        if self.e_subsidy:
            lines.append(f"🔋 电补助：{self.e_subsidy}")
        if self.w_meter_addr:
            lines.append(f"💧 水表：{self.w_meter_addr} 读数 {self.w_meter_value}")
        if self.w_subsidy:
            lines.append(f"💧 水补助：{self.w_subsidy}")
        return "\n".join(lines)


class FeeQueryClient:
    """异步电费查询客户端，内部复用连接池"""

    URL = "http://payment.cqu.edu.cn/charge/feeitem/getThirdData"

    DEFAULT_HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "http://payment.cqu.edu.cn",
        "Referer": "http://payment.cqu.edu.cn/",
        "X-Requested-With": "com.tencent.mm",
        "Authorization": "Basic Y2hhcmdlOmNoYXJnZV9zZWNyZXQ=",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    D_RE = re.compile(r"^(?:[A-Za-z][0-9]|[A-Za-z]{2})\d{3}$")

    def __init__(self, timeout: float = 10.0):
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.close()

    def detect_campus(self, room: str) -> Campus:
        """根据房间号判定校区：匹配 D 区命名规则返回 "d"，否则返回 "a"。

        D 区：楼栋为「1 字母+1 数字」或「2 字母」，再接三位数字房间号，如 B4611、XX611。
        A 区：形如 B10S306，首字母后中间必掺杂字母，天然不命中 D 正则。
        """
        return "d" if self.D_RE.match(room.strip().upper()) else "a"

    async def query(
        self,
        room: str,
        auth_token: str,
        cookies: Union[dict, str],
        *,
        feeitemid: str = "448",
        fee_type: str = "IEC",
        level: int = 2,
        campus: Campus = "d",
    ) -> FeeInfo:
        """
        :param room:       房间号，如 "B4611"
        :param auth_token: synjones-auth 头，形如 "bearer xxx"
        :param cookies:    cookie，dict 或 "k=v; k2=v2" 字符串
        :param campus:     校区代号，用于按响应结构分发解析
        :raises FeeQueryError: 网络错误、状态码异常、接口业务码非 200
        """
        if self._client is None:
            await self.start()

        headers = {**self.DEFAULT_HEADERS, "synjones-auth": auth_token}
        data = {
            "feeitemid": str(feeitemid),
            "type": fee_type,
            "level": str(level),
            "room": room,
        }

        try:
            resp = await self._client.post(
                self.URL,
                headers=headers,
                cookies=cookies,
                data=data,
            )
        except httpx.RequestError as e:
            raise FeeQueryError(f"网络请求失败：{e}") from e

        if resp.status_code != 200:
            raise FeeQueryError(f"HTTP {resp.status_code}：{resp.text[:200]}")

        try:
            result = resp.json()
        except Exception as e:
            raise FeeQueryError(f"响应不是合法 JSON：{e}") from e

        if result.get("code") != 200:
            raise FeeQueryError(f"接口返回错误：{result.get('msg')}")

        m = result.get("map") or {}
        show = m.get("showData") or {}
        d = m.get("data") or {}

        parser = self._parse_a if campus == "a" else self._parse_d
        return parser(room, show, d, result)

    @staticmethod
    def _parse_d(room: str, show: dict, d: dict, result: dict) -> FeeInfo:
        return FeeInfo(
            room=room,
            amount=show.get("剩余金额", ""),
            e_subsidy=show.get("电剩余补助", ""),
            w_subsidy=show.get("水剩余补助", ""),
            e_meter_addr=show.get("电表地址", ""),
            e_meter_value=show.get("电表读数", ""),
            w_meter_addr=show.get("水表地址", ""),
            w_meter_value=show.get("水表读数", ""),
            account_id=d.get("accountid", ""),
            raw=result,
        )

    @staticmethod
    def _parse_a(room: str, show: dict, d: dict, result: dict) -> FeeInfo:
        raw_price = show.get("电价", "")
        try:
            e_price = float(raw_price) if raw_price else -1.0
        except ValueError:
            e_price = -1.0
        return FeeInfo(
            room=show.get("房间编号", "") or room,
            amount=show.get("现金余额", ""),  # 现金余额 → 剩余金额
            e_subsidy=show.get("补贴余额", ""),  # 补贴余额 → 电剩余补助
            e_price=e_price,  # 电价
            account_id=d.get("roomID", ""),
            raw=result,
        )
