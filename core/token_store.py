"""Token 持久化（轮转写回 DB）。

- 启动优先级：环境变量 refresh_token -> DB token 表（主理人决策 #8）。
- 轮转后的 refresh_token / access_token / drive_id / expires_at 持久化回 token 表，
  全库仅一行（id 固定为 1），避免重启后旧 token 失效。
- 为线程安全，每次操作打开独立会话（会话工厂由外部注入）。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from db import utc_now
from models import Token

logger = logging.getLogger(__name__)


class TokenStore:
    """Token 读写仓库。"""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        refresh_token: str = "",
    ) -> None:
        """Args:
        session_factory: 返回新 ``Session`` 的可调用对象（线程安全）。
        refresh_token: 来自环境变量的 refresh_token（优先级最高）。
        """
        self._session_factory = session_factory
        self._env_refresh_token = refresh_token or ""

    def load_refresh_token(self) -> str:
        """获取可用的 refresh_token（env 优先，否则读 DB）。"""
        if self._env_refresh_token:
            return self._env_refresh_token
        with self._session_factory() as db:
            token = db.get(Token, 1)
            if token and token.refresh_token:
                return token.refresh_token
        raise RuntimeError(
            "未找到 refresh_token：请通过环境变量 ALIYUNDRIVE_REFRESH_TOKEN 注入，"
            "或先完成一次成功刷新以落库。"
        )

    def get_token(self) -> Optional[Token]:
        """读取当前 token 行（可能为 None）。"""
        with self._session_factory() as db:
            return db.get(Token, 1)

    def save_token(
        self,
        access_token: str,
        refresh_token: str,
        drive_id: str,
        expires_at: Optional[datetime],
    ) -> Token:
        """保存（或更新）token 行，并落库。"""
        with self._session_factory() as db:
            token = db.get(Token, 1)
            if token is None:
                token = Token(id=1)
                db.add(token)
            token.access_token = access_token
            token.refresh_token = refresh_token
            token.drive_id = drive_id
            token.expires_at = expires_at or utc_now()
            db.commit()
            db.refresh(token)
            logger.info("token 已%s，drive_id=%s", "更新" if token.id else "写入", drive_id)
            return token

    def save_refresh_token(self, refresh_token: str) -> Token:
        """仅保存 refresh_token（用于前端手动输入），保留已有字段。

        与 ``save_token`` 的区别：只更新 refresh_token 列，不覆盖
        access_token / drive_id / expires_at，避免在用户仅更换凭证时
        丢失上一次刷新得到的元数据。
        """
        with self._session_factory() as db:
            token = db.get(Token, 1)
            if token is None:
                token = Token(id=1)
                db.add(token)
            token.refresh_token = refresh_token
            db.commit()
            db.refresh(token)
            logger.info("refresh_token 已通过前端输入保存")
            return token
