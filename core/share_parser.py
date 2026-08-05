"""分享链接解析：提取 share_id、换取 share_token、列举分享文件。

由 ``AliyunClient`` 组合持有并委托调用（见类图 ``AliyunClient --> ShareParser``）。
所有 HTTP 走 ``AliyunClient._request``，统一鉴权与错误处理
（分享失效会自动转为 ``ShareExpiredError``）。
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Optional

from core.endpoints import ENDPOINTS
from core.types import ShareFile

if TYPE_CHECKING:
    from core.aliyun_client import AliyunClient

logger = logging.getLogger(__name__)

# 分享链接形态：
#   https://www.aliyundrive.com/s/<share_id>             （整盘分享）
#   https://www.aliyundrive.com/s/<share_id>/folder/<parent_id>  （子目录分享）
#   https://www.alipan.com/s/<share_id>                  （新版域名）
#   https://www.alipan.com/s/<share_id>/folder/<parent_id>
_SHARE_ID_REGEX = re.compile(r"/s/([A-Za-z0-9_-]+)")
_SHARE_PARENT_REGEX = re.compile(r"/folder/([A-Za-z0-9_-]+)")


class ShareParser:
    """分享链接解析器。"""

    def __init__(self, client: "AliyunClient") -> None:
        self._client = client

    def extract_share_id(self, share_url: str) -> str:
        """从分享 URL 提取 share_id。"""
        match = _SHARE_ID_REGEX.search(share_url or "")
        if not match:
            raise ValueError(f"无法从分享链接解析 share_id: {share_url}")
        return match.group(1)

    def extract_parent_id(self, share_url: str) -> str:
        """从分享 URL 提取 parent_file_id（/folder/<id> 段）；整盘分享返回 "root"。

        关键：用户的分享链接常带子目录（如 /folder/<hex_id>），
        必须用此 ID 作为列举起点，否则只看到顶层 folder 而漏掉真实文件。
        """
        match = _SHARE_PARENT_REGEX.search(share_url or "")
        return match.group(1) if match else "root"

    def get_share_token(
        self, share_id: str, share_pwd: Optional[str] = None
    ) -> str:
        """换取分享访问令牌 share_token。"""
        body = {"share_id": share_id, "share_pwd": share_pwd or ""}
        data = self._client._request(
            "POST", ENDPOINTS["share_token"], json=body
        )
        token = data.get("share_token", "")
        if not token:
            raise ValueError(f"获取 share_token 失败，分享可能已失效: {share_id}")
        return token

    def list_files(
        self, share_id: str, share_token: str, parent_file_id: str = "root"
    ) -> list[ShareFile]:
        """列举分享目录下文件（分页；仅顶层，子目录不递归）。"""
        files: list[ShareFile] = []
        marker: str = ""
        while True:
            body = {
                "share_id": share_id,
                "share_token": share_token,
                "parent_file_id": parent_file_id,
                "limit": 100,
                "order_by": "name",
                "order_direction": "ASC",
                "marker": marker,
            }
            data = self._client._request(
                "POST",
                ENDPOINTS["file_list"],
                json=body,
                headers={"x-share-token": share_token},
            )
            items = data.get("items", []) or []
            for item in items:
                # 防御：跳过非 dict / 缺关键字段的脏条目，避免空 file_id/name 进入转存。
                if not isinstance(item, dict):
                    continue
                fid = item.get("file_id") or ""
                fname = item.get("name") or ""
                ftype = item.get("type", "file") or "file"
                if not fid or not fname:
                    logger.warning(
                        "分享列举到脏条目（缺 file_id/name），已跳过: %r", item
                    )
                    continue
                files.append(
                    ShareFile(
                        file_id=fid,
                        name=fname,
                        parent_file_id=item.get("parent_file_id", ""),
                        type=ftype,
                        size=int(item.get("size", 0) or 0),
                    )
                )
            marker = data.get("next_marker", "") or ""
            if not marker:
                break
        return files

    # 递归遍历分享目录时允许的最大深度（防御异常深层目录或环路导致无限递归）。
    SHARE_WALK_MAX_DEPTH = 20

    def list_all_files(
        self,
        share_id: str,
        share_token: str,
        parent_file_id: str = "root",
        max_depth: int = SHARE_WALK_MAX_DEPTH,
    ) -> list[ShareFile]:
        """递归遍历分享目录树，返回所有叶文件（type=="file"）。

        对每一层用 ``self.list_files`` 分页列举；遇到 folder 递归进入其 file_id；
        file 直接追加到结果。``max_depth`` 到达上限即停止（防御异常深层/环路）并记 warning。

        说明：``list_files`` 内部已分页（while 循环处理 next_marker），本方法对每一层
        只需调用一次 ``self.list_files`` 即可拿到该层全部条目。递归收集到的文件是扁平的
        （忽略原始子目录层级），天然契合「扁平化转存」需求。
        """
        result: list[ShareFile] = []
        self._walk(share_id, share_token, parent_file_id, result, 0, max_depth)
        return result

    def _walk(
        self,
        share_id: str,
        share_token: str,
        parent_file_id: str,
        result: list[ShareFile],
        depth: int,
        max_depth: int,
    ) -> None:
        """递归辅助：收集 ``parent_file_id`` 下的所有叶文件到 ``result``。

        Args:
            depth: 当前已下钻的层数（从 0 起）；达到 ``max_depth`` 即停止。
        """
        if depth >= max_depth:
            logger.warning(
                "分享递归达到最大深度 %s，停止遍历：%s", max_depth, parent_file_id
            )
            return
        items = self.list_files(share_id, share_token, parent_file_id)
        for item in items:
            if item.type == "folder":
                self._walk(
                    share_id, share_token, item.file_id, result, depth + 1, max_depth
                )
            else:
                result.append(item)
