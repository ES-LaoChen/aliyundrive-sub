"""Telegram 频道网页抓取：HTTP 获取（可注入、带退避重试）。

生产代码正常依赖 ``httpx``；``httpx`` 不可用时（如未安装的环境 / 纯单测）模块仍可导入，
但真实 ``TgFetcher`` 会因客户端未初始化而在 ``fetch`` 时抛出 ``RuntimeError``——
测试用注入 stub 替代，不依赖真实网络。

默认抓取目标：``https://t.me/s/{channel}``（公开频道预览页，零权限）。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class TgFetcher:
    """封装公开频道预览页的网页获取，带指数退避重试（含 HTTP 429）。"""

    def __init__(
        self,
        proxy: str = "",
        timeout: float = 20.0,
        user_agent: str = "Mozilla/5.0 (compatible; TGMonitor/1.0)",
        max_retries: int = 3,
    ) -> None:
        """构造抓取器。

        Args:
            proxy: 代理地址（http/https/socks5），为空则直连。
            timeout: 单次请求超时（秒）。
            user_agent: 请求 UA，模拟浏览器以降低被拦概率。
            max_retries: 最大重试次数（含首次）。
        """
        self._proxy = proxy
        self._timeout = timeout
        self._user_agent = user_agent
        self._max_retries = max(int(max_retries), 1)
        # 懒导入 httpx：环境未安装时不影响模块导入，仅真实抓取时抛错。
        try:
            import httpx

            self._httpx = httpx
            self._client = httpx.Client(
                proxy=proxy or None,
                headers={"User-Agent": user_agent},
                timeout=timeout,
            )
            logger.debug("TgFetcher 已初始化 httpx 客户端")
        except Exception:
            self._httpx = None
            self._client = None
            logger.warning(
                "httpx 不可用，TgFetcher 无法发起真实请求（测试应注入 stub 替代）"
            )

    def fetch(self, url: str) -> str:
        """获取 URL 的响应文本，带退避重试。

        重试策略：
          - HTTP 429：读 ``Retry-After`` 头（秒），否则按 ``2**尝试次数`` 指数退避（封顶 30s）。
          - 其他网络/HTTP 异常：同样按指数退避重试。
          - 超过 ``max_retries`` 仍失败，抛 ``RuntimeError`` 并记日志。

        Args:
            url: 目标 URL（通常为 ``https://t.me/s/{channel}``）。

        Returns:
            响应文本（HTML）。

        Raises:
            RuntimeError: 重试耗尽仍失败，或客户端未初始化。
        """
        if self._client is None:
            raise RuntimeError("httpx 客户端未初始化，无法发起请求（请安装 httpx 或注入 stub）")

        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._client.get(url)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and str(retry_after).isdigit():
                        sleep_s = float(retry_after)
                    else:
                        sleep_s = min(2.0 ** attempt, 30.0)
                    logger.warning(
                        "TG 抓取遇 429，%.1fs 后重试（%d/%d）",
                        sleep_s, attempt, self._max_retries,
                    )
                    time.sleep(sleep_s)
                    last_exc = RuntimeError(f"HTTP 429 @ attempt {attempt}")
                    continue
                resp.raise_for_status()
                return resp.text
            except Exception as exc:  # noqa: BLE001  # 网络/HTTP 异常统一重试
                last_exc = exc
                if attempt < self._max_retries:
                    sleep_s = min(2.0 ** attempt, 30.0)
                    logger.warning(
                        "TG 抓取异常，%.1fs 后重试（%d/%d）: %s",
                        sleep_s, attempt, self._max_retries, exc,
                    )
                    time.sleep(sleep_s)
        raise RuntimeError(
            f"TG 抓取失败（已重试 {self._max_retries} 次）: {last_exc}"
        )
