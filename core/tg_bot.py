"""TG 机器人实时监听频道推送（需求：用 Bot 监控公共频道，无需管理员权限）。

通过 pytelegrambotapi 的 ``channel_post_handler`` 接收频道新消息（Bot 需已加入
频道，作为普通成员即可，无需管理员权限），收到后回调上层
``on_message(channel, blob, links, message_id)``。

依赖：pytelegrambotapi（pip install pytelegrambotapi）。仅在 run/start 被调用时
才 import telebot，未安装不影响其它功能。

``BotListener`` 支持 ``stop()``：调用 telebot 的 ``stop_polling`` 终止长轮询，
使监听线程自然退出，释放网络连接与线程资源（幂等，可重复调用）。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class BotListener:
    """可停止的 TG Bot 频道监听器（阻塞式，需在独立线程中运行 ``run()``）。"""

    def __init__(self, token: str, channels, on_message) -> None:
        """构造监听器。

        Args:
            token: TG_BOT_TOKEN。
            channels: 监控频道 @username 列表（可带或不带 @）。
            on_message: 回调 ``on_message(channel, blob, links, message_id)``（同步）。
        """
        self._token = token
        self._channel_set = {
            str(c).strip().lstrip("@").lower() for c in channels if c and str(c).strip()
        }
        self._on_message = on_message
        self._bot = None
        # 停止标志：stop() 可能先于 run() 内 bot 构造完成被调用。
        self._stopped = threading.Event()

    def run(self) -> None:
        """启动 telebot 实时监听（阻塞，需在线程中运行）。"""
        from telebot import TeleBot
        from core.tg_channel_parser import TgChannelParser

        if self._stopped.is_set():
            logger.info("TG_MONITOR: 监听器已被要求停止，跳过启动")
            return

        bot = TeleBot(self._token)
        self._bot = bot
        channel_set = self._channel_set
        on_message = self._on_message

        @bot.channel_post_handler(func=lambda m: True)
        def _handle_channel_post(message):
            try:
                ch = (getattr(message.chat, "username", None) or "").lstrip("@").lower()
                if ch not in channel_set:
                    return
                blob = (getattr(message, "text", None)
                        or getattr(message, "caption", None) or "")
                links = TgChannelParser.extract_share_links(blob or "")
                message_id = getattr(message, "message_id", None)
                logger.info(
                    "TG_MONITOR: 收到频道 %s 新消息 mid=%s 链接数=%d",
                    ch, message_id, len(links),
                )
                on_message(ch, blob, links, message_id)
            except Exception:
                logger.exception("TG_MONITOR: 处理频道消息异常")

        logger.info("TG_MONITOR: Bot 监听启动，监控频道=%s", sorted(channel_set))
        # skip_pending=True：跳过启动前积压消息，仅收加入后新推送（符合实时监控语义）。
        bot.infinity_polling(skip_pending=True)
        logger.info("TG_MONITOR: Bot 监听已退出")

    def stop(self) -> None:
        """停止监听（幂等）：终止长轮询，使监听线程自然退出、释放资源。"""
        self._stopped.set()
        bot = self._bot
        if bot is not None:
            try:
                bot.stop_polling()
            except Exception:
                logger.exception("TG_MONITOR: stop_polling 失败(已忽略)")


def start_bot_listener(token: str, channels, on_message) -> None:
    """兼容旧入口：构造并阻塞运行一个 ``BotListener``（不可停止句柄）。"""
    BotListener(token, channels, on_message).run()
