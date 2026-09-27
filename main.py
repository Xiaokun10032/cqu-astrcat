from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .service import CquService
from .store import Store
from .tools import FeeQueryError


@register(
    "astrbot_plugin_cqu_astrcat", "Xiaokun10032", "简单的cqu一卡通聚合查询bot", "0.2.2"
)
class CquAstrcat(Star):
    """cqu 一卡通聚合查询插件入口：只负责注册、生命周期与命令转发。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = Store(self)
        self.service = CquService(config)

    # ---------- 生命周期 ----------
    async def initialize(self):
        """插件实例化后调用：启动连接池并迁移旧版绑定数据。"""
        await self.service.start()
        await self.store.migrate_legacy_bindings()

    async def terminate(self):
        """插件卸载/停用时调用：释放连接池。"""
        await self.service.close()

    # ---------- 命令 ----------
    @filter.command_group("cqu")
    def cqu():
        pass

    @cqu.command("bind", alias={"b"})
    async def bind(self, event: AstrMessageEvent, room: str = ""):
        """绑定房间：/cqu bind B4611"""
        room = room.strip().upper()
        if not room:
            yield event.plain_result("用法：/cqu bind 房间号，例如 /cqu bind B4611")
            return

        qq = str(event.get_sender_id())
        await self.store.upsert_binding(qq, room, event.unified_msg_origin)
        yield event.plain_result("✅ 绑定成功：*****\n使用 /cqu fee 查询电费")

    @cqu.command("unbind", alias={"ub"})
    async def unbind(self, event: AstrMessageEvent):
        """解绑：/cqu unbind"""
        qq = str(event.get_sender_id())
        await self.store.delete_binding(qq)
        yield event.plain_result("✅ 已解除绑定")

    @cqu.command("myroom")
    async def myroom(self, event: AstrMessageEvent):
        """查看绑定：/cqu myroom"""
        qq = str(event.get_sender_id())
        info = await self.store.get_binding(qq)
        if not info:
            yield event.plain_result("你还没有绑定房间，使用 /cqu bind [房间代号]")
            return
        yield event.plain_result(f"当前绑定：{info['room']}")

    @cqu.command("fee")
    async def fee(self, event: AstrMessageEvent):
        """查询电费：/cqu fee"""
        qq = str(event.get_sender_id())
        info = await self.store.get_binding(qq)
        if not info:
            yield event.plain_result("请先使用 /cqu bind 房间号 绑定")
            return

        if not self.service.auth_token:
            yield event.plain_result(
                "⚠️ 插件尚未配置 auth_token，请联系管理员在 WebUI 配置"
            )
            return

        try:
            result = await self.service.query_room(info["room"])
        except FeeQueryError as e:
            yield event.plain_result(f"❌ 查询失败：{e}")
            return

        yield event.plain_result(result.to_text())
