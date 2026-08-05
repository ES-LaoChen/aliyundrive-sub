"""通知中心：Telegram Bot + 预留 MediaLibraryHook。

NotifierManager 聚合多个通知渠道；转存完成 / 临期提醒 / 分享失效时统一发送。
单个渠道失败不影响其他渠道及主流程（见 ARCHITECTURE.md 共享约定）。
Emby/Plex 扫库（MediaLibraryHook）按主理人决策 #2、#3 仅留空实现占位，P2 再接线。

T-D4 增补：
- ``send_transfer_summary(sub, summary, error_kind_map)`` 渲染 PRD §4.2 markdown 模板
- ``ERROR_KIND_MESSAGES`` 翻译表（TransferErrorKind → 人话）
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable, List, Optional, Protocol

import requests

logger = logging.getLogger(__name__)


# ====================== T-D4：错误类型 → 人话翻译表 ======================
# 通知里暴露给用户的人话文案；与 TransferErrorKind 一一对应。
ERROR_KIND_MESSAGES: dict[str, str] = {
    "share_expired": "分享链接已失效",
    "proof_invalid": "proof_code 校验失败",
    "quota_full": "网盘空间已满",
    "rate_limited": "接口限流",
    "target_missing": "目标目录不存在或无权访问",
    "file_not_found": "源文件已不在分享中",
    "invalid_parameter": "请求参数无效",
    "network": "网络异常（已自动重试）",
    "unknown": "未知错误",
}


def translate_error_kind(kind: str) -> str:
    """把 TransferErrorKind 字符串翻译为人话；找不到原样返回。"""
    return ERROR_KIND_MESSAGES.get(kind, kind)


class Notifier(Protocol):
    """通知渠道抽象。"""

    def send(self, title: str, content: str, level: str = "info") -> None:
        ...


class MediaLibraryHook:
    """预留：Emby/Plex 扫库刷新（P2，不在本版接线）。"""

    def refresh(self) -> None:
        # TODO(P2): 调用 Emby/Plex 刷新 API，触发媒体库更新。
        logger.info("MediaLibraryHook.refresh() 预留接口，未实现（P2）")


class TelegramNotifier:
    """Telegram Bot 通知渠道（T-TG，仅用于命中通知）。

    通过 Bot API ``sendMessage`` 发送纯文本消息；``token`` 或 ``chat_id`` 为空时
    ``send`` 直接返回（不报错、不发送），便于未配置时静默降级。
    """

    def __init__(self, token: str = "", chat_id: str = "") -> None:
        self._token = token
        self._chat_id = chat_id

    def send(self, title: str, content: str, level: str = "info") -> None:
        if not self._token or not self._chat_id:
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": f"[{level}] {title}\n{content}",
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception:
            logger.warning("Telegram 通知发送失败", exc_info=True)


class NotifierManager:
    """通知聚合器：注册多个渠道，统一发送。"""

    def __init__(self) -> None:
        self._notifiers: List[Notifier] = []
        # 预留媒体库钩子（不随通知发送，独立触发）。
        self.media_hook = MediaLibraryHook()

    def register(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)

    def configure_telegram(self, token: str, chat_id: str) -> None:
        """热更新 Telegram 通知渠道（T-TG 配置保存时调用）。

        找到已注册的 ``TelegramNotifier`` 则原地更新其 ``token`` / ``chat_id``；
        未找到则新建并加入。保持 ``send`` 行为不变（空 token/chat_id 时静默）。

        Args:
            token: Telegram Bot Token（可空）。
            chat_id: 接收通知的 chat_id（可空）。
        """
        for notifier in self._notifiers:
            if isinstance(notifier, TelegramNotifier):
                notifier._token = token
                notifier._chat_id = chat_id
                return
        # 未找到则新建并加入（空值也允许，send 会静默降级）。
        self.register(TelegramNotifier(token, chat_id))

    def send(self, title: str, content: str, level: str = "info") -> None:
        for notifier in self._notifiers:
            try:
                notifier.send(title, content, level)
            except Exception:
                logger.warning("通知渠道发送异常", exc_info=True)

    def send_transfer_summary(
        self,
        sub: Any,
        summary: dict,
        error_kind_map: Optional[dict] = None,
        failed_items: Optional[Iterable[dict]] = None,
        elapsed_seconds: Optional[float] = None,
    ) -> None:
        """发送转存摘要通知（PRD §4.2 模板）。

        Args:
            sub: 订阅 ORM 对象（取 name / id 字段）。
            summary: 形如 ``{"added": N, "skipped": M, "failed": F, "pending": P}``。
            error_kind_map: ``TransferErrorKind → 人话`` 翻译表（默认用 ``ERROR_KIND_MESSAGES``）。
            failed_items: 失败详情列表，每项 ``{"name": str, "kind": str, "attempts": int, "last_error": str}``。
            elapsed_seconds: 本次 run 耗时（秒），None 时不显示。
        """
        kind_map = error_kind_map or ERROR_KIND_MESSAGES
        added = summary.get("added", 0)
        skipped = summary.get("skipped", 0)
        failed = summary.get("failed", 0)
        pending = summary.get("pending", 0)

        # 状态选择器（✅ 全部成功 / ⚠️ 部分成功 / ❌ 全部失败）
        if failed == 0 and added > 0:
            status_text = "✅ 全部成功"
        elif added == 0 and skipped == 0 and failed > 0:
            status_text = "❌ 全部失败"
        elif added > 0 and failed > 0:
            status_text = "⚠️ 部分成功"
        else:
            status_text = "ℹ️ 无新增（仅跳过）"

        lines: list[str] = [
            f"## 订阅「{getattr(sub, 'name', '?')}」#{getattr(sub, 'id', '?')} 本次转存",
            f"- 状态: {status_text}",
            f"- 新增: {added}",
            f"- 跳过(已存在): {skipped}",
            f"- 失败: {failed}",
        ]
        # 失败列表（前 5 个 + 翻译）
        failed_list = list(failed_items or [])[:5]
        for item in failed_list:
            kind_str = item.get("kind", "unknown")
            kind_msg = kind_map.get(kind_str, kind_str)
            attempts = item.get("attempts", 0)
            max_a = item.get("max_attempts", 3)
            lines.append(
                f"  - {item.get('name', '?')} — {kind_msg}（重试 {attempts}/{max_a}）"
            )
        lines.append(f"- 待续传: {pending}")
        if elapsed_seconds is not None:
            lines.append(f"- 耗时: {elapsed_seconds:.0f}s")
        content = "\n".join(lines)
        # level
        if failed == 0:
            level = "info"
        elif added > 0 or skipped > 0:
            level = "warn"
        else:
            level = "error"
        self.send(
            f"订阅「{getattr(sub, 'name', '?')}」#{getattr(sub, 'id', '?')} 本次转存",
            content,
            level,
        )

    def refresh_media_library(self) -> None:
        self.media_hook.refresh()
