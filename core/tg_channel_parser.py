"""Telegram 公开频道网页抓取解析器（纯函数、零副作用）。

仅依赖 BeautifulSoup 解析 ``https://t.me/s/{channel}`` 返回的 HTML，不发起网络请求、
不读写数据库，便于单测与复用。

提供：
- ``ChannelMessage``：单条频道消息的轻量数据结构。
- ``TgChannelParser``：频道名归一化、阿里云盘分享链接提取、消息块解析。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ChannelMessage:
    """单条频道消息的结构（data class，便于测试断言）。"""

    message_id: int
    text: str
    links: List[str]
    title: str = ""


class TgChannelParser:
    """Telegram 频道网页抓取解析工具（全部为静态方法，无实例状态）。"""

    # 阿里云盘分享链接（alipan.com / aliyundrive.com 的 /s/ 路径，含可选查询串）。
    _SHARE_RE = re.compile(
        r"https?://(?:www\.)?(?:alipan\.com|aliyundrive\.com)/s/[A-Za-z0-9_]+(?:\?[^\s\"'<>]+)?",
        re.IGNORECASE,
    )

    @staticmethod
    def _normalize_channel(raw: str) -> Optional[str]:
        """把各种写法的频道标识归一化为末段用户名（或数字 ID）。

        支持：
          - ``@user`` / ``user``
          - ``https://t.me/user`` / ``https://t.me/s/User``（含 ``telegram.me`` 与可选 ``www.``）
          - 纯数字 ID（如 ``1234567890``）
        无法识别（空串 / 不含可提取用户名）返回 ``None``。
        """
        if raw is None:
            return None
        s = raw.strip()
        if not s:
            return None
        # 去前导 @。
        s = s.lstrip("@")
        # 提取 t.me / telegram.me 末段用户名（兼容 /s/ 预览子路径与可选 www.）。
        m = re.search(
            r"(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]+)",
            s,
            re.IGNORECASE,
        )
        if m:
            return m.group(1).lower()
        # 纯数字 ID 原样返回（可能是频道数字 ID）。
        if re.fullmatch(r"\d+", s):
            return s
        # 纯 user 形式（仅字母数字下划线）。
        if re.fullmatch(r"[A-Za-z0-9_]+", s):
            return s.lower()
        return None

    @staticmethod
    def extract_share_links(text: str) -> List[str]:
        """从一段文本中提取全部阿里云盘分享链接。

        Args:
            text: 待扫描文本（如消息正文 + 链接拼接后的 blob）。

        Returns:
            分享链接列表（去重由调用方负责），无则空列表。
        """
        if not text:
            return []
        return TgChannelParser._SHARE_RE.findall(text)

    @staticmethod
    def strip_share_links(text: str) -> str:
        """从一段文本中去除全部阿里云盘分享链接，保留描述性正文。

        用于在“频道日记”中把推送原文拆成「描述文本 + 链接」两部分展示，
        让用户直接看到频道推送了什么内容，而不是一段混着链接的原始串。

        Args:
            text: 待处理的推送原文（如 ``blob``）。

        Returns:
            去除分享链接并合并多余空白后的描述文本；无内容返回空串。
        """
        if not text:
            return ""
        cleaned = TgChannelParser._SHARE_RE.sub("", text)
        return " ".join(cleaned.split())

    @staticmethod
    def is_aliyun_share_link(url: str) -> bool:
        """判断单个 URL 是否为「阿里云盘分享链接」。

        用于更新订阅前的防御性校验：仅当识别到 ``alipan.com`` / ``aliyundrive.com``
        域名下 ``/s/`` 开头的分享链接时返回 ``True``，过滤掉 Telegram 链接、百度网盘、
        以及其他无关链接，避免误把非阿里云盘链接写进订阅 ``share_url``。

        域名特征（与 ``_SHARE_RE`` 一致，小写不敏感）：

        - 协议：``http`` / ``https``
        - 域名：``alipan.com`` 或 ``aliyundrive.com``（可选 ``www.`` 前缀）
        - 路径：以 ``/s/`` 开头，后接分享码（可带 ``?pwd=`` 提取码等查询参数）

        Args:
            url: 待判定的链接字符串。

        Returns:
            是阿里云盘分享链接返回 ``True``，否则 ``False``。
        """
        if not url or not url.strip():
            return False
        return bool(TgChannelParser._SHARE_RE.fullmatch(url.strip()))

    @staticmethod
    def parse_messages(html: str) -> List[ChannelMessage]:
        """解析 ``https://t.me/s/{channel}`` 返回的 HTML，提取消息列表。

        每个 ``div.tgme_widget_message`` 视为一条消息：
          - ``data-post``（如 ``durov/123``）末段数字作 ``message_id``
          - ``.tgme_widget_message_text`` 的可见文本作 ``text``（合并多余空白）
          - ``.tgme_widget_message_title`` 的可见文本作 ``title``（可选）
          - 块内全部 ``a[href]`` 的 href 作 ``links``（含链接预览）

        解析失败（无 BeautifulSoup / 单块解析异常）不抛异常：整体失败返回空列表，
        单块失败跳过该块。
        """
        if not html:
            return []
        try:
            from bs4 import BeautifulSoup
        except Exception:
            logger.warning("BeautifulSoup 不可用，无法解析 TG 消息 HTML")
            return []

        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            logger.warning("TG 消息 HTML 解析失败", exc_info=True)
            return []

        out: List[ChannelMessage] = []
        for block in soup.select("div.tgme_widget_message"):
            try:
                message_id = 0
                data_post = block.get("data-post")
                if data_post:
                    # 形如 "durov/123" 或 "durov/123?..."，取末段数字。
                    tail = str(data_post).rsplit("/", 1)[-1]
                    m = re.search(r"\d+", tail)
                    if m:
                        message_id = int(m.group(0))

                # 正文：合并空白，避免换行/多空格干扰分享链接提取。
                text_parts: List[str] = []
                title = ""
                text_el = block.select_one(".tgme_widget_message_text")
                if text_el is not None:
                    text_parts.append(" ".join(text_el.get_text(strip=True).split()))
                title_el = block.select_one(".tgme_widget_message_title")
                if title_el is not None:
                    title = " ".join(title_el.get_text(strip=True).split())

                # 链接：块内全部 <a href>，含链接预览。
                links: List[str] = []
                for a in block.find_all("a", href=True):
                    href = a.get("href")
                    if href:
                        links.append(str(href))

                out.append(
                    ChannelMessage(
                        message_id=message_id,
                        text=" ".join(text_parts),
                        links=links,
                        title=title,
                    )
                )
            except Exception:
                logger.debug("解析单条 TG 消息块失败（跳过）", exc_info=True)
                continue
        return out
