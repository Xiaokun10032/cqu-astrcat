from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .service import DEFAULT_THRESHOLD, CquService
from .store import Store
from .tools import FeeQueryError

DAILY_HOUR = 9
"""每日快照与余额提醒的执行时刻（本地时间，24 小时制）。"""


@register(
    "astrbot_plugin_cqu_astrcat", "Xiaokun10032", "简单的cqu一卡通聚合查询bot", "0.3"
)
class CquAstrcat(Star):
    """cqu 一卡通聚合查询插件入口：注册、生命周期与命令转发。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = Store(self)
        self.service = CquService(config, self.store)
        self._daily_task: asyncio.Task | None = None

    # ---------- 生命周期 ----------
    async def initialize(self):
        """插件实例化后调用：启动连接池、迁移旧数据并开启每日任务。"""
        await self.service.start()
        await self.store.migrate_legacy_bindings()
        self._daily_task = asyncio.create_task(self._daily_loop())

    async def terminate(self):
        """插件卸载/停用时调用：停止每日任务并释放连接池。"""
        if self._daily_task is not None:
            self._daily_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._daily_task
            self._daily_task = None
        await self.service.close()

    # ---------- 每日任务 ----------
    async def _daily_loop(self):
        """每天 DAILY_HOUR 点执行一次日结任务。

        单次执行失败由 `_daily_job` 内部消化，循环本身不会中断。
        """
        while True:
            now = datetime.now()
            next_run = now.replace(hour=DAILY_HOUR, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            await asyncio.sleep((next_run - now).total_seconds())
            await self._daily_job()

    async def _daily_job(self) -> tuple[int, int] | None:
        """记录全部绑定用户的余额快照，并推送余额不足提醒。

        单次执行失败只记录日志；单个用户的提醒推送失败也不会中断其余用户。

        Returns:
            ``(快照用户数, 成功推送的提醒数)``；整体执行失败时为 None。
        """
        try:
            bindings = await self.store.get_bindings()
            balances = await self.service.snapshot_all(bindings)
            previous = await self.store.previous_snapshot()
            if balances:
                await self.store.append_snapshot(balances)
                logger.info(f"[cqu-astrcat] 每日快照完成，共 {len(balances)} 位用户")
            else:
                logger.info("[cqu-astrcat] 每日快照没有可用数据")

            pushed = 0
            for alert in self.service.build_alerts(balances, bindings, previous):
                try:
                    text = (
                        f"电费余额仅剩 {alert.amount:.2f} 元，"
                        f"低于提醒阈值 {alert.threshold:.2f} 元"
                    )
                    if alert.usage is not None and alert.usage.spent > 0:
                        text += (
                            f"，自 {alert.usage.since} 以来"
                            f"已用电 {alert.usage.spent:.2f} 元"
                        )
                    chain = []
                    if (
                        MessageSession.from_str(alert.session).message_type
                        == MessageType.GROUP_MESSAGE
                    ):
                        chain.append(At(qq=alert.qq))
                        text = f" {text}"
                    chain.append(Plain(text))
                    await self.context.send_message(alert.session, MessageChain(chain))
                    pushed += 1
                except Exception as e:
                    logger.warning(f"[cqu-astrcat] 提醒推送失败 qq={alert.qq}：{e}")
            return len(balances), pushed
        except Exception as e:
            logger.error(f"[cqu-astrcat] 每日任务执行失败：{e}", exc_info=True)
            return None

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

    @cqu.command("remind")
    async def remind(
        self, event: AstrMessageEvent, action: str = "", threshold: str = ""
    ):
        """余额提醒设置：/cqu remind [on|off] [阈值]"""
        qq = str(event.get_sender_id())
        info = await self.store.get_binding(qq)
        if not info:
            yield event.plain_result("请先使用 /cqu bind 房间号 绑定")
            return

        action = action.strip().lower()
        if not action:
            state = "已开启" if info.get("remind") else "已关闭"
            current = float(info.get("threshold", DEFAULT_THRESHOLD))
            yield event.plain_result(
                f"余额提醒：{state}，阈值 {current:.2f} 元\n"
                "用法：/cqu remind on 10 开启并设阈值为 10 元，/cqu remind off 关闭"
            )
            return

        if action not in ("on", "off"):
            yield event.plain_result("用法：/cqu remind on 10 或 /cqu remind off")
            return

        if threshold.strip():
            try:
                value = float(threshold)
            except ValueError:
                yield event.plain_result("阈值需要是数字，例如 /cqu remind on 10")
                return
            if value <= 0:
                yield event.plain_result("阈值需要大于 0")
                return
        else:
            value = float(info.get("threshold", DEFAULT_THRESHOLD))

        enabled = action == "on"
        await self.store.set_remind(qq, enabled, value, event.unified_msg_origin)
        text = f"✅ 余额提醒已{'开启' if enabled else '关闭'}，阈值 {value:.2f} 元"
        if enabled:
            text += "\n提醒将发送到当前会话"
        yield event.plain_result(text)

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

        text = result.to_text()
        amount = result.amount_value
        usage = (
            await self.service.usage_since_snapshot(qq, amount)
            if amount is not None
            else None
        )
        if usage is not None and usage.spent > 0:
            text += f"\n📈 自 {usage.since} 以来已用电 {usage.spent:.2f} 元"
        yield event.plain_result(text)

    # ---------- 调试入口（验证完毕后请连同本段一起删除） ----------
    @cqu.command("debug_daily")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def debug_daily(self, event: AstrMessageEvent):
        """手动触发一次日结：/cqu debug_daily（仅管理员）

        会向所有开启提醒且余额不足的用户推送消息，注意避免重复触发。
        """
        result = await self._daily_job()
        if result is None:
            yield event.plain_result("❌ 日结执行失败，详见日志")
            return
        yield event.plain_result(
            f"✅ 日结完成：快照 {result[0]} 位，推送 {result[1]} 条"
        )
