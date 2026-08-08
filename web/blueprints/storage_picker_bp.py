"""统一目录选择器后端 API（存储管理 / 同步管理共用）。

提供两类浏览能力，供前端 ``storage_picker`` 组件调用：

1. 存储后端虚拟路径浏览（已存在的挂载）：
   - ``GET /storage-picker/mounts``：列出全部启用挂载（作为顶层「盘」）。
   - ``GET /storage-picker/list?path=/挂载名/相对路径``：经 ``TaoSyncClient``
     列出该虚拟路径下的目录（仅目录，供选择器使用）。用于同步管理的源/目标路径选择。

2. 本地文件系统浏览（用于「新增存储目录」时选取 local 驱动的 ``root_path``，
   此时挂载尚不存在，无法经 TaoSyncClient）：
   - ``GET /storage-picker/local?path=/abs/dir``：在沙箱根（``LOCAL_PICKER_ROOT``，
     默认 ``/``）下列出子目录，防止越界逃逸。

两类接口返回统一 JSON 形状：
   ``{"items": [{"name": str, "is_dir": true}], "path": str, "parent": str, "error": str|null}``
前端据此渲染目录树、面包屑与搜索过滤。
"""
from __future__ import annotations

import logging
import os

from flask import (
    Blueprint,
    current_app,
    jsonify,
    request,
)
from werkzeug.security import safe_join

from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("storage_picker", __name__, url_prefix="/storage-picker")

# 本地选择器沙箱根：仅允许浏览此根之下，避免误选系统敏感目录。
# 容器/服务器可按需通过环境变量收紧（如设为 /app/data）。
LOCAL_PICKER_ROOT = os.environ.get("LOCAL_PICKER_ROOT", "/") or "/"


def _services() -> Services:
    return current_app.config["SERVICES"]


def _safe_local_dir(path: str) -> str:
    """把请求路径约束在 LOCAL_PICKER_ROOT 之内，返回绝对真实目录。"""
    root = os.path.realpath(LOCAL_PICKER_ROOT)
    rel = (path or "/").strip().lstrip("/")
    if not rel:
        return root
    # 用 safe_join 防止 ../ 逃逸；失败则回退根。
    joined = safe_join(root, rel)
    if joined is None:
        return root
    real = os.path.realpath(joined)
    if os.path.commonpath((root, real)) != root:
        return root
    return real


@bp.route("/mounts", methods=["GET"])
def mounts():
    """列出全部启用挂载（作为选择器顶层节点）。"""
    svc = _services()
    try:
        rows = svc.sync_service.get_mount_list()
        items = [{"name": m["name"], "is_dir": True, "driver": m["driverType"]} for m in rows]
    except Exception as exc:
        logger.warning("目录选择器：读取挂载失败: %s", exc)
        return jsonify({"items": [], "error": str(exc)}), 200
    return jsonify({"items": items, "path": "/", "parent": ""})


@bp.route("/list", methods=["GET"])
def list_virtual():
    """列出存储后端虚拟路径下的目录（供同步管理源/目标选择）。

    Query:
        path: 虚拟路径，默认 ``/``（列出挂载）；``/挂载名/子目录`` 进入挂载内部。
    """
    svc = _services()
    path = request.args.get("path", "/") or "/"
    try:
        engine_id = svc.sync_service.get_system_engine_id()
    except Exception as exc:
        logger.warning("目录选择器：读取引擎失败: %s", exc)
        return jsonify({"items": [], "path": path, "parent": "", "error": str(exc)}), 200

    try:
        with svc.session_factory() as db:
            from core.sync.engine import get_storage_client

            client = get_storage_client(db, engine_id)
            details = client.file_list_detail_api(path)
        # 仅保留目录，并给出名称（去掉结尾 '/'）。
        items = []
        for name, meta in details.items():
            if meta.get("isDir"):
                items.append({"name": name.rstrip("/"), "is_dir": True})
        items.sort(key=lambda x: x["name"].casefold())
    except Exception as exc:
        logger.warning("目录选择器：列举虚拟路径 %s 失败: %s", path, exc)
        return jsonify({
            "items": [], "path": path, "parent": "",
            "error": str(exc),
        }), 200

    return jsonify({"items": items, "path": path, "parent": ""})


@bp.route("/local", methods=["GET"])
def list_local():
    """列出本地文件系统子目录（用于新增 local 挂载选取 root_path）。

    仅在 ``LOCAL_PICKER_ROOT`` 沙箱内浏览；返回绝对目录路径。
    """
    req_path = request.args.get("path", "/") or "/"
    parent_abs = _safe_local_dir(req_path)
    try:
        entries = []
        with os.scandir(parent_abs) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                entries.append({"name": entry.name, "is_dir": True})
        entries.sort(key=lambda x: x["name"].casefold())
    except Exception as exc:
        logger.warning("目录选择器：列举本地目录 %s 失败: %s", parent_abs, exc)
        return jsonify({
            "items": [], "path": parent_abs, "parent": "",
            "error": str(exc),
        }), 200

    return jsonify({"items": entries, "path": parent_abs, "parent": ""})
