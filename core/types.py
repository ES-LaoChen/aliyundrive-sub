"""核心层轻量数据结构、异常与错误分类枚举（非 ORM）。

``ShareFile`` / ``DriveFile`` 仅承载文件基础字段，跨模块传递，不在 ORM 中。
异常统一在此定义，便于上层（transfer / scheduler）按类型区分处理。

T-D1 增补：
- ``TransferErrorKind`` 枚举：8 类错误分类，与 ``ApiError`` 解耦
- ``TransferError`` 异常：携带 ``kind`` 便于 with_retry 装饰器判断
- 现有 ``ShareExpiredError`` 仍继承自 ``ApiError``，保持既有 isinstance 检查不破
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TransferErrorKind(str, Enum):
    """转存错误分类（与 ``ApiError.code`` 解耦）。

    业务层唯一入口为 ``ErrorClassifier.classify()``，禁止直接 ``isinstance`` 推断。
    """

    SHARE_EXPIRED = "share_expired"          # 分享链接已失效
    PROOF_INVALID = "proof_invalid"          # proof_code 校验失败（不重试，避免浪费配额）
    QUOTA_FULL = "quota_full"                # 网盘空间已满
    RATE_LIMITED = "rate_limited"            # 429 / 限流
    TARGET_MISSING = "target_missing"        # 目标目录不存在 / 非当前账号
    FILE_NOT_FOUND = "file_not_found"        # 源文件已不在分享中
    INVALID_PARAMETER = "invalid_parameter"  # 400 / 参数错（不重试）
    NETWORK = "network"                      # 5xx / Timeout / ConnectionError
    UNKNOWN = "unknown"                      # 兜底


@dataclass
class ShareFile:
    """分享目录中的文件节点。"""

    file_id: str
    name: str
    parent_file_id: str
    # "file" | "folder"
    type: str = "file"
    size: int = 0


@dataclass
class DriveFile:
    """自己云盘中的文件节点。"""

    file_id: str
    name: str
    parent_file_id: str
    # "file" | "folder"
    type: str = "file"
    size: int = 0


class ApiError(Exception):
    """阿里云盘 API 返回的非 2xx 错误或业务错误码。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.status_code = status_code


class ShareExpiredError(ApiError):
    """分享链接已失效 / 已取消 / 已过期，需人工替换。"""


class TransferError(Exception):
    """业务层装饰后的转存错误，统一携带 ``TransferErrorKind`` 便于分类处理。

    区分于 ``ApiError``：
    - ``ApiError`` 是网络/服务端返回的原始错误（仍可能抛给上层）
    - ``TransferError`` 是 with_retry 装饰器或 ErrorClassifier 主动分类后的「业务错误」，
      携带 ``kind / code / message / attempts``，用于决定重试/放弃/通知文案。
    """

    def __init__(
        self,
        kind: TransferErrorKind,
        message: str = "",
        code: str = "",
        attempts: int = 0,
        original: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message or f"[{kind.value}] {code}")
        self.kind = kind
        self.code = code
        self.message = message
        self.attempts = attempts
        self.original = original
