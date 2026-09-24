from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from .tools import FeeQueryClient, FeeQueryError
import re


@register(
    "astrbot_plugin_cqu_astrcat", "Xiaokun10032", "简单的cqu一卡通聚合查询bot", "0.2.2"
)
class CquAstrcat(Star):
    KEY_ROOM = "user_room:"

    def __init__(self, context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.fee_client: FeeQueryClient | None = None

    # ----------Life Cycly-----------------
    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        self.fee_client = FeeQueryClient(
            timeout=int(self.config.get("timeout", 10))
        )  # 实例化httpx异步处理client

    async def _client(self) -> FeeQueryClient:
        """懒加载兜底：防止 initialize 没被调用"""
        if self.fee_client is None:
            self.fee_client = FeeQueryClient(
                timeout=int(self.config.get("timeout", 10))
            )
        return self.fee_client

    # ---------- 配置读取 ----------

    def _key(self, qq: str) -> str:
        return f"{self.KEY_ROOM}{qq}"

    def _auth_token(self) -> str:
        return (self.config.get("auth_token") or "").strip()

    def _cookies(self, campus: str) -> dict:
        """把配置里对应校区的 cookie 字符串解析成 dict"""
        raw = (self.config.get(campus, {}).get("cookie") or "").strip()
        result = {}
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
        return result

    def _query_params(self, campus: str) -> dict:
        return {
            "feeitemid": str(self.config.get(campus, {}).get("feeitemid", "448")),
            "fee_type": str(self.config.get("fee_type", "IEC")),
            "level": int(self.config.get("level", 2)),
        }

    # ---------- 命令 ----------
    @filter.command_group("cqu")
    def cqu():
        pass

    # @cqu.command("school_bus", alias={"sb", "校车"})

    @cqu.command("bind", alias={"b"})
    async def bind(self, event: AstrMessageEvent, room: str = ""):
        """绑定房间：/cqu bind B4611"""
        room = room.strip().upper()
        if not room:
            yield event.plain_result("用法：/cqu bind 房间号，例如 /cqu bind B4611")
            return
        # if not self.ROOM_RE.match(room):
        #     yield event.plain_result(
        #         "房间号格式看起来不对，应形如 B4611（字母 + 3~6 位数字）"
        #     )
        #     return

        qq = str(event.get_sender_id())
        await self.put_kv_data(self._key(qq), {"room": room})
        yield event.plain_result(f"✅ 绑定成功：*****\n使用 /cqu fee 查询电费")

    @cqu.command("unbind", alias={"ub"})
    async def unbind(self, event: AstrMessageEvent):
        """解绑：/cqu unbind"""
        qq = str(event.get_sender_id())
        await self.delete_kv_data(self._key(qq))
        yield event.plain_result("✅ 已解除绑定")

    @cqu.command("myroom")
    async def myroom(self, event: AstrMessageEvent):
        """查看绑定：/cqu myroom"""
        qq = str(event.get_sender_id())
        info = await self.get_kv_data(self._key(qq), None)
        if not info:
            yield event.plain_result("你还没有绑定房间，使用 /cqu bind [房间代号]")
            return
        yield event.plain_result(f"当前绑定：{info['room']}")

    @cqu.command("fee")
    async def fee(self, event: AstrMessageEvent):
        """查询电费：/cqu fee"""
        qq = str(event.get_sender_id())
        info = await self.get_kv_data(self._key(qq), None)
        if not info:
            yield event.plain_result("请先使用 /cqu bind 房间号 绑定")
            return

        token = self._auth_token()
        if not token:
            yield event.plain_result(
                "⚠️ 插件尚未配置 auth_token，请联系管理员在 WebUI 配置"
            )
            return

        client = await self._client()
        campus = client.detect_campus(info["room"])
        cookies = self._cookies(campus)

        try:
            result = await client.query(
                campus=campus,
                room=info["room"],
                auth_token=token,
                cookies=cookies,
                **self._query_params(campus),
            )
        except FeeQueryError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return

        yield event.plain_result(result.to_text())

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        if self.fee_client:
            await self.fee_client.close()
            self.fee_client = None
