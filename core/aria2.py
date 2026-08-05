"""Aria2 远程下载客户端（JSON-RPC）。

转存完成后可将下载直链提交到 Aria2，实现「转存 -> 本地下载」联动。
配置开关 ``ARIA2_RPC_ENABLE``；失败仅记日志，不阻断主流程（见 ARCHITECTURE.md 支链路 B）。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class Aria2Client:
    """Aria2 JSON-RPC 封装。"""

    def __init__(
        self,
        rpc_url: str = "",
        secret: str = "",
        enabled: bool = False,
    ) -> None:
        self.rpc_url = rpc_url
        self.secret = secret
        self.enabled = enabled

    # ----- 运行时配置更新（供 Web 设置页调用） -----
    def update(self, rpc_url: str, secret: str, enabled: bool) -> None:
        self.rpc_url = rpc_url
        self.secret = secret
        self.enabled = enabled

    def _call(self, method: str, params: list[Any]) -> Any:
        """发起一次 JSON-RPC 调用。"""
        if self.secret:
            params = [f"token:{self.secret}", *params]
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": params,
        }
        resp = requests.post(self.rpc_url, json=payload, timeout=30)
        data = resp.json()
        if "error" in data and data["error"]:
            raise RuntimeError(f"Aria2 RPC 错误: {data['error']}")
        return data.get("result")

    def test_connection(self) -> bool:
        """探测 Aria2 是否可用。"""
        if not self.enabled or not self.rpc_url:
            return False
        try:
            self._call("aria2.getVersion", [])
            return True
        except Exception:
            logger.warning("Aria2 连接测试失败", exc_info=True)
            return False

    def add_uri(self, urls: list[str], options: Optional[dict] = None) -> str:
        """提交下载任务，返回 gid。未启用或地址为空时返回空串。"""
        if not self.enabled or not self.rpc_url or not urls:
            return ""
        try:
            return str(self._call("aria2.addUri", [list(urls), options or {}]))
        except Exception:
            logger.warning("提交 Aria2 下载任务失败", exc_info=True)
            return ""
