"""错误分类器（T-D2，PRD T07）。

将 ``ApiError`` / ``ShareExpiredError`` / ``requests.Timeout`` / ``ConnectionError``
等各类异常统一映射为 ``TransferErrorKind``。业务层禁止直接 ``isinstance`` 推断。

设计原则：
- 唯一入口：``ErrorClassifier.classify(exc) -> TransferErrorKind``
- 优先级：``ShareExpiredError`` > ``TransferError``（已是分类好的）> ``ApiError``（按 code/message）> 异常类名 > ``UNKNOWN``
- 不抛异常，分类失败兜底 ``UNKNOWN``
"""
from __future__ import annotations

import logging
from typing import Optional

from core.types import (
    ApiError,
    ShareExpiredError,
    TransferError,
    TransferErrorKind,
)

logger = logging.getLogger(__name__)


# 不区分大小写的关键字 → kind
# 仅当 ApiError 的 code/message 命中时映射；否则降级。
_API_CODE_KEYWORDS: list[tuple[TransferErrorKind, tuple[str, ...]]] = [
    (TransferErrorKind.SHARE_EXPIRED, ("sharelink.cancelled", "sharelink.expired",
                                        "notfound.sharelink", "invalidparameter.sharelink",
                                        "share_invalid", "share_expired")),
    (TransferErrorKind.PROOF_INVALID, ("proof", "proofcode", "proofinvalid", "invalid.proof",
                                        "proof_invalid", "proof_code")),
    (TransferErrorKind.QUOTA_FULL, ("quotaexceeded", "quota_full", "spacefull",
                                     "storagespacefull", "user.is.overcapacity",
                                     "insufficient.storage")),
    (TransferErrorKind.RATE_LIMITED, ("ratelimitexceeded", "rate_limit", "toomanyrequests",
                                        "forbidden.too.frequent")),
    (TransferErrorKind.TARGET_MISSING, ("targetnotfound", "foldernotfound",
                                          "filenotfound.folder", "target_missing",
                                          "invalidparameter.fileid")),
    (TransferErrorKind.FILE_NOT_FOUND, ("filenotfound", "file_not_found",
                                         "notfound.file", "invalidparameter.notfound")),
    (TransferErrorKind.INVALID_PARAMETER, ("invalidparameter", "badrequest",
                                            "invalid_request")),
    (TransferErrorKind.NETWORK, ("internalerror", "internalservererror", "badgateway",
                                  "serviceunavailable", "gatewaytimeout")),
]

_API_CODE_EXACT: dict[str, TransferErrorKind] = {
    # 精确 code 映射（优先级高于关键词）
    "ShareLink.Cancelled": TransferErrorKind.SHARE_EXPIRED,
    "ShareLink.Expired": TransferErrorKind.SHARE_EXPIRED,
    "NotFound.ShareLink": TransferErrorKind.SHARE_EXPIRED,
    "InvalidParameter.ShareLink": TransferErrorKind.SHARE_EXPIRED,
    "InvalidParameter.Proof": TransferErrorKind.PROOF_INVALID,
    "InvalidParameter.ProofCode": TransferErrorKind.PROOF_INVALID,
    "QuotaExceeded": TransferErrorKind.QUOTA_FULL,
    "Forbidden.TooFrequent": TransferErrorKind.RATE_LIMITED,
    "TooManyRequests": TransferErrorKind.RATE_LIMITED,
    "NotFound.File": TransferErrorKind.FILE_NOT_FOUND,
    "FileNotFound": TransferErrorKind.FILE_NOT_FOUND,
    "InvalidParameter": TransferErrorKind.INVALID_PARAMETER,
    "InternalError": TransferErrorKind.NETWORK,
    "ServiceUnavailable": TransferErrorKind.NETWORK,
    "BadGateway": TransferErrorKind.NETWORK,
    "GatewayTimeout": TransferErrorKind.NETWORK,
}


class ErrorClassifier:
    """错误分类静态方法集合。"""

    @staticmethod
    def classify(exc: BaseException) -> TransferErrorKind:
        """将任意异常分类为 ``TransferErrorKind``。"""
        if isinstance(exc, ShareExpiredError):
            return TransferErrorKind.SHARE_EXPIRED
        if isinstance(exc, TransferError):
            return exc.kind
        if isinstance(exc, ApiError):
            kind = ErrorClassifier._from_api_code(exc.code, exc.message or "")
            if kind is not None:
                return kind
            # 无关键字命中时按 status_code 兜底
            return ErrorClassifier._from_status_code(exc.status_code)
        return ErrorClassifier._from_exception(exc)

    @staticmethod
    def _from_api_code(code: str, message: str) -> Optional[TransferErrorKind]:
        # 精确匹配
        if code and code in _API_CODE_EXACT:
            return _API_CODE_EXACT[code]
        # 关键字匹配（大小写不敏感）
        c = (code or "").lower()
        m = (message or "").lower()
        for kind, keywords in _API_CODE_KEYWORDS:
            for kw in keywords:
                if kw in c or kw in m:
                    return kind
        return None

    @staticmethod
    def _from_status_code(status_code: Optional[int]) -> TransferErrorKind:
        if status_code is None:
            return TransferErrorKind.UNKNOWN
        if status_code == 429:
            return TransferErrorKind.RATE_LIMITED
        if 500 <= status_code < 600:
            return TransferErrorKind.NETWORK
        if status_code == 403:
            # 403 多数是权限；可能是 quota / proof；留 UNKNOWN 让上层覆盖
            return TransferErrorKind.UNKNOWN
        if status_code in (400, 404):
            return TransferErrorKind.INVALID_PARAMETER
        return TransferErrorKind.UNKNOWN

    @staticmethod
    def _from_exception(exc: BaseException) -> TransferErrorKind:
        """从非 ApiError 异常（网络/超时等）推断。"""
        cls = exc.__class__.__name__.lower()
        # requests 异常
        if "timeout" in cls or "readtimeout" in cls or "connecttimeout" in cls:
            return TransferErrorKind.NETWORK
        if "connection" in cls or "connectionerror" in cls:
            return TransferErrorKind.NETWORK
        if "httperror" in cls:
            return TransferErrorKind.NETWORK
        # 兜底
        return TransferErrorKind.UNKNOWN
