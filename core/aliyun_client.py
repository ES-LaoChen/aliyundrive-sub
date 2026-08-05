"""阿里云盘客户端：认证、分享解析、转存、盘内操作。

所有请求经统一 ``requests.Session``（带 urllib3 Retry 重试），自动注入
``Authorization: Bearer <access_token>``；401 时自动刷新 token 并重试一次。
接口路径集中在 ``ENDPOINTS``，便于后续用真实 token 实测校准（见交付说明遗留问题）。

遵循 ARCHITECTURE.md 共享约定：
- Token 注入 / 凭证来源优先级 / 错误与重试 / 时区 / check_name_mode=auto_rename。
- 业务代码仅依赖 ``ProofProvider`` 抽象接口（由 ``save_file`` 内部调用）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import Settings
from core.endpoints import ENDPOINTS
from core.share_parser import ShareParser
from core.token_store import TokenStore
from core.types import ApiError, DriveFile, ShareExpiredError, ShareFile
from db import utc_now
from models import Token

logger = logging.getLogger(__name__)

# token 临近过期提前刷新的余量（秒）。
_TOKEN_EXPIRE_LEEWAY = 300


def build_http_session() -> requests.Session:
    """构造带指数退避重试的 ``requests.Session``。

    对 5xx / 429 做最多 3 次重试；``Content-Type`` 默认 JSON。
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504, 429],
        allowed_methods=frozenset(["GET", "POST", "PUT", "DELETE"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": "aliyundrive-sub/0.1",
            "Content-Type": "application/json",
        }
    )
    return session


class AliyunClient:
    """阿里云盘 API 客户端。

    转存走新版 ``POST /v2/file/copy``（带 Bearer token + x-share-token），
    不需要 proof_code，因此不再依赖任何 proof 提供方。
    """

    def __init__(
        self,
        cfg: Settings,
        http: requests.Session,
        token_store: TokenStore,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.token_store = token_store
        self.share_parser = ShareParser(self)
        self._token_cache: Optional[Token] = None
        # 缓存最近一次 token 刷新响应中的全部盘 ID（避免调 404 端点）。
        self._drive_info_cache: dict[str, str] = {}

    # ===================== 认证 =====================
    def ensure_token(self) -> Token:
        """确保返回有效的 access_token（临近过期或缺失时刷新）。"""
        token = self._token_cache
        if token and token.access_token and not self._is_expired(token):
            return token
        return self.refresh_access_token()

    def _is_expired(self, token: Token, leeway: int = _TOKEN_EXPIRE_LEEWAY) -> bool:
        if not token.expires_at:
            return False
        return (utc_now() + timedelta(seconds=leeway)) >= token.expires_at

    def refresh_access_token(self) -> Token:
        """用 refresh_token 换取新的 access_token，并轮转写回 DB。"""
        refresh_token = self.token_store.load_refresh_token()
        resp = self.http.post(
            ENDPOINTS["token"],
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        data = resp.json()
        if data.get("code"):
            raise ApiError(data.get("code"), data.get("message", "token 刷新失败"))
        access_token = data.get("access_token", "")
        new_refresh = data.get("refresh_token", refresh_token)
        drive_id = data.get("default_drive_id", "") or data.get("drive_id", "")
        expires_in = int(data.get("expires_in", 7200))
        expires_at = utc_now() + timedelta(seconds=expires_in)
        # 缓存全部盘 ID（token 响应直接包含，无需额外调 get_drive_info 端点）。
        self._drive_info_cache = {
            "default_drive_id": str(data.get("default_drive_id", "") or ""),
            "resource_drive_id": str(data.get("resource_drive_id", "") or ""),
            "backup_drive_id": str(data.get("backup_drive_id", "") or ""),
        }
        token = self.token_store.save_token(
            access_token, new_refresh, drive_id, expires_at
        )
        self._token_cache = token
        logger.info("access_token 已刷新，drive_id=%s", drive_id)
        return token

    def get_drive_info(self) -> str:
        """返回默认 drive_id（优先用 token 响应缓存，否则用 user_info 端点）。"""
        token = self.ensure_token()
        if token.drive_id:
            return token.drive_id
        # 如果 DB 中没有 drive_id，尝试从缓存取
        if self._drive_info_cache.get("default_drive_id"):
            return self._drive_info_cache["default_drive_id"]
        # 最后兜底：调 user_info 端点（兼容老版 drive_info 端点）
        try:
            data = self._request("POST", ENDPOINTS["user_info"], json={})
            drive_id = data.get("default_drive_id", "") or data.get("drive_id", "")
            if drive_id:
                self.token_store.save_token(
                    token.access_token, token.refresh_token, drive_id, token.expires_at
                )
                self._token_cache = self.token_store.get_token()
                for key in ("default_drive_id", "resource_drive_id", "backup_drive_id"):
                    if data.get(key):
                        self._drive_info_cache[key] = str(data[key])
            return drive_id
        except Exception:
            return ""

    def list_drives(self) -> list[dict]:
        """返回用户所有可用盘的列表。

        token 刷新响应只对 VIP 用户返回 resource_drive_id，普通用户需要
        调 /v2/user/get 端点才能拿到全部盘 ID。优先用缓存，没有缓存或缓存
        不全时调端点补充。
        返回格式：[{"drive_id": str, "drive_name": str, "drive_type": str}, ...]
        """
        # 先尝试从缓存读（如果三个盘 ID 都齐了就直接返回）
        if self._drive_info_cache.get("default_drive_id"):
            cached_drives = self._build_drive_list(self._drive_info_cache)
            # 如果缓存里只有 default，没有 resource/backup，则需要调端点
            only_default = (
                self._drive_info_cache.get("default_drive_id")
                and not self._drive_info_cache.get("resource_drive_id")
                and not self._drive_info_cache.get("backup_drive_id")
            )
            if not only_default:
                return cached_drives

        # 调 /v2/user/get 端点获取完整盘信息（覆盖缓存中可能缺失的字段）
        try:
            data = self._request("POST", ENDPOINTS["user_info"], json={})
            for key in ("default_drive_id", "resource_drive_id", "backup_drive_id"):
                if data.get(key):
                    self._drive_info_cache[key] = str(data[key])
        except Exception as exc:
            logger.warning("调用 user_info 端点失败，回退到缓存: %s", exc)
            if not self._drive_info_cache:
                self.ensure_token()
            if not self._drive_info_cache:
                token = self.ensure_token()
                if token.drive_id:
                    self._drive_info_cache = {
                        "default_drive_id": token.drive_id,
                        "resource_drive_id": "",
                        "backup_drive_id": "",
                    }

        return self._build_drive_list(self._drive_info_cache)

    @staticmethod
    def _build_drive_list(drive_info: dict[str, str]) -> list[dict]:
        """从 drive_id 字典构造盘列表（去重 + 中文名 + 类型）。"""
        seen: set[str] = set()
        drives: list[dict] = []
        type_map = [
            ("default_drive_id", "默认盘", "default"),
            ("resource_drive_id", "资源盘", "resource"),
            ("backup_drive_id", "备份盘", "backup"),
        ]
        for key, name, dtype in type_map:
            did = drive_info.get(key, "") or ""
            if did and did not in seen:
                drives.append({"drive_id": str(did), "drive_name": name, "drive_type": dtype})
                seen.add(did)
        return drives

    # ===================== 分享解析 =====================
    def resolve_share(
        self, share_url: str, share_pwd: Optional[str] = None
    ) -> tuple[str, str]:
        """从分享 URL 解析出 (share_id, share_token)。"""
        share_id = self.share_parser.extract_share_id(share_url)
        share_token = self.share_parser.get_share_token(share_id, share_pwd)
        return share_id, share_token

    def get_share_info(self, share_id: str) -> dict:
        """获取分享元信息（含过期时间）。"""
        return self._request("POST", ENDPOINTS["share_info"], json={"share_id": share_id})

    def list_share_files(
        self, share_id: str, share_token: str, parent_file_id: str = "root"
    ) -> list[ShareFile]:
        """列举分享目录下的文件（顶层，分页）。"""
        return self.share_parser.list_files(share_id, share_token, parent_file_id)

    def list_share_files_recursive(
        self,
        share_id: str,
        share_token: str,
        parent_file_id: str = "root",
        max_depth: int = 20,
    ) -> list[ShareFile]:
        """递归列举分享目录下所有文件（含多层子文件夹），扁平返回。

        委托 ``ShareParser.list_all_files`` 完成递归；仅返回叶文件（type=="file"），
        原始子目录层级被忽略（扁平化）。``list_share_files``（单层）接口保持不变，
        以兼容其它调用方。
        """
        return self.share_parser.list_all_files(
            share_id, share_token, parent_file_id, max_depth
        )

    # ===================== 盘内操作 =====================
    def list_files(self, parent_file_id: str = "root", drive_id: Optional[str] = None) -> list[DriveFile]:
        """列举自己云盘某目录下的文件（分页）。

        Args:
            parent_file_id: 父目录 ID，默认 "root"。
            drive_id: 指定盘 ID。None 时自动用 get_drive_info() 取默认盘。
        """
        token = self.ensure_token()
        if drive_id is None:
            drive_id = self.get_drive_info()
        files: list[DriveFile] = []
        marker: str = ""
        while True:
            body = {
                "drive_id": drive_id,
                "parent_file_id": parent_file_id,
                "limit": 100,
                "order_by": "name",
                "order_direction": "ASC",
                "marker": marker,
            }
            data = self._request("POST", ENDPOINTS["file_list"], json=body)
            for item in data.get("items", []):
                files.append(
                    DriveFile(
                        file_id=item.get("file_id", ""),
                        name=item.get("name", ""),
                        parent_file_id=item.get("parent_file_id", ""),
                        type=item.get("type", "file"),
                        size=int(item.get("size", 0) or 0),
                    )
                )
            marker = data.get("next_marker", "") or ""
            if not marker:
                break
        return files

    def create_folder(self, parent_file_id: str, name: str, drive_id: Optional[str] = None) -> str:
        """在云盘创建文件夹，返回新 folder 的 file_id。"""
        token = self.ensure_token()
        if drive_id is None:
            drive_id = self.get_drive_info()
        data = self._request(
            "POST",
            ENDPOINTS["create_folder"],
            json={
                "drive_id": drive_id,
                "parent_file_id": parent_file_id,
                "name": name,
                "type": "folder",
                "check_name_mode": "refuse",
            },
        )
        return data.get("file_id", "")

    def save_file(
        self,
        parent_file_id: str,
        share_file: ShareFile,
        share_id: str,
        share_token: str,
        drive_id: Optional[str] = None,
        new_name: Optional[str] = None,
    ) -> str:
        """将分享文件转存到自己云盘，返回新 file_id。

        新版阿里云盘（alipan）转存走 ``POST /v2/file/copy``：
          - 不需要 proof_code
          - 需要 ``Authorization: Bearer <access_token>`` + ``x-share-token`` 两个头
        老版的 ``create_with_proof`` 在新版 alipan 上返 403（即便 proof 正确）。

        重命名在 copy 时随 ``new_name`` 字段一次性完成（对齐 app_daSSe 实测实现）：
          - aliyun 的 ``/v2/file/copy`` **只认 ``new_name`` 字段**来指定复制后的文件名；
            旧实现误用 ``name`` 字段会被接口忽略，导致改名静默失效（转存后仍是原名）。
          - 仅当目标名与原名不同才传 ``new_name``（与 app_daSSe 一致），避免无意义的
            重名处理。
          - ``auto_rename=False``：目标名必须精确生效；若目标目录下已存在同名文件，
            由上层 skip-by-name 去重保障不重复转存，否则 copy 直接失败（异常记入结果，
            而非静默产出错误文件名）。

        Args:
            drive_id: 目标盘 ID（default/resource/backup）。None 时用默认盘。
                重要：目标 parent_file_id 必须在该盘上存在，否则报 NotFound。
            new_name: 转存后的目标文件名；为空则沿用 ``share_file.name``。
        """
        token = self.ensure_token()
        target_drive_id = drive_id or self.get_drive_info()
        # 改名在 copy 时随 new_name 字段一次性完成（对齐 app_daSSe）。
        # 仅当目标名与原名不同才传，减少无意义的重名处理。
        copy_body = {
            "share_id": share_id,
            "file_id": share_file.file_id,
            "to_drive_id": target_drive_id,
            "to_parent_file_id": parent_file_id,
            "auto_rename": False,
        }
        target_name = new_name or share_file.name
        if target_name and target_name != share_file.name:
            copy_body["new_name"] = target_name
        data = self._request(
            "POST",
            ENDPOINTS["file_copy"],
            json=copy_body,
            extra_headers={"x-share-token": share_token},
        )
        # 兼容不同返回字段命名（fileId / driveId 为部分版本命名）。
        file_id = data.get("file_id") or data.get("fileId") or ""
        if not file_id:
            # 接口未返回新 file_id：转存实际未生效（可能因目标同名冲突、
            # 配额不足或接口异常）。显式报错，避免上层记为"假成功"。
            code = str(data.get("code") or "")
            message = data.get("message") or "转存未返回 file_id，可能目标已存在或配额不足"
            raise ApiError(code, message, 0)
        return file_id

    def rename_file(self, file_id: str, new_name: str, drive_id: Optional[str] = None) -> None:
        """重命名云盘文件。"""
        token = self.ensure_token()
        if drive_id is None:
            drive_id = self.get_drive_info()
        self._request(
            "POST",
            ENDPOINTS["rename"],
            json={
                "drive_id": drive_id,
                "file_id": file_id,
                "name": new_name,
                "check_name_mode": "refuse",
            },
        )

    def delete_file(self, file_id: str, drive_id: Optional[str] = None) -> None:
        """将云盘文件移入回收站。"""
        token = self.ensure_token()
        if drive_id is None:
            drive_id = self.get_drive_info()
        if not drive_id:
            raise ApiError("MissingDriveId", "无法删除：未获取到目标盘 drive_id", 0)
        self._request(
            "POST",
            ENDPOINTS["delete"],
            json={"drive_id": drive_id, "file_id": file_id},
        )

    def get_download_url(self, file_id: str, drive_id: Optional[str] = None) -> str:
        """获取文件下载直链。"""
        token = self.ensure_token()
        if drive_id is None:
            drive_id = self.get_drive_info()
        data = self._request(
            "POST",
            ENDPOINTS["download_url"],
            json={"drive_id": drive_id, "file_id": file_id},
        )
        return data.get("url", "") or data.get("download_url", "")

    # ===================== 底层请求 =====================
    def _request(
        self,
        method: str,
        url: str,
        json: Optional[dict] = None,
        headers: Optional[dict] = None,
        retry_on_401: bool = True,
        extra_headers: Optional[dict] = None,
    ) -> dict:
        """带鉴权头的统一请求；401 自动刷新并重试一次；>=400 抛 ApiError。

        Args:
            extra_headers: 与 ``headers`` 合并（不覆盖 Authorization），用于
                一些需要同时带 access_token + 业务 token 的端点（如 ``/v2/file/copy``）。
        """
        token = self.ensure_token()
        req_headers = {"Authorization": f"Bearer {token.access_token}"}
        if headers:
            req_headers.update(headers)
        if extra_headers:
            req_headers.update(extra_headers)
        resp = self.http.request(
            method.upper(), url, json=json, headers=req_headers, timeout=30
        )
        if resp.status_code == 401 and retry_on_401:
            logger.warning("收到 401，尝试刷新 token 后重试: %s", url)
            self.refresh_access_token()
            return self._request(method, url, json=json, headers=headers, retry_on_401=False, extra_headers=extra_headers)
        if resp.status_code >= 400:
            self._raise_api_error(resp)
        try:
            return resp.json()
        except ValueError:
            return {}

    def _raise_api_error(self, resp: requests.Response) -> None:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        code = str(data.get("code") or resp.status_code)
        message = data.get("message") or resp.text
        # 分享失效 / 取消 / 过期：转换为可识别的 ShareExpiredError。
        if self._is_share_invalid(code, message):
            raise ShareExpiredError(code, message, resp.status_code)
        raise ApiError(code, message, resp.status_code)

    @staticmethod
    def _is_share_invalid(code: str, message: str) -> bool:
        code = str(code)
        invalid_codes = {
            "ShareLink.Cancelled",
            "ShareLink.Expired",
            "NotFound.ShareLink",
            "InvalidParameter.ShareLink",
        }
        if code in invalid_codes:
            return True
        msg = (message or "").lower()
        if "share" in msg and any(k in msg for k in ("失效", "过期", "取消", "不存在", "invalid", "expired")):
            return True
        return False

    @staticmethod
    def classify_error(exc: BaseException):
        """静态方法：把任意异常分类为 ``TransferErrorKind``。

        委托给 ``ErrorClassifier.classify``；保留在 ``AliyunClient`` 上便于外部
        「直接对 client 异常做分类」（与 ``ApiError`` 解耦，不引入循环依赖）。
        """
        # 延迟 import 避免启动时循环
        from core.error_classifier import ErrorClassifier
        from core.types import TransferErrorKind

        return ErrorClassifier.classify(exc)
