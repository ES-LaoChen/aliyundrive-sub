"""云盘资源蓝图（P2 最小实现）：浏览 / 新建文件夹 / 重命名 / 删除 / 下载。

下载：获取直链后（若启用 Aria2）提交到 Aria2 本地下载。
所有操作依赖 svc.client（AliyunClient），需有效 token。

支持磁盘切换（default / resource / backup）与面包屑导航（返回上级目录）。
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from urllib.parse import quote, unquote

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("files", __name__, url_prefix="/files")


def _services() -> Services:
    return current_app.config["SERVICES"]


def _crumb_encode(items: list) -> str:
    """将面包屑列表序列化为 URL 安全字符串。

    使用 quote 编码 JSON，配合蓝图 index() 中的 unquote 解码。
    经 url_for 二次编码 / request.args 二次解码后保持一致。
    """
    return quote(json.dumps(items, ensure_ascii=False))


@bp.route("", methods=["GET"])
def index():
    svc = _services()
    parent = request.args.get("parent", "root")
    path = request.args.get("path", "/") or "/"
    drive = request.args.get("drive", "default")

    # 获取盘列表
    drives: list[dict] = []
    current_drive_id: Optional[str] = None
    try:
        drives = svc.client.list_drives()
        for d in drives:
            if d["drive_type"] == drive:
                current_drive_id = d["drive_id"]
                break
        if current_drive_id is None and drives:
            current_drive_id = drives[0]["drive_id"]
    except Exception as exc:
        logger.warning("获取盘列表失败: %s", exc)

    # 解析面包屑
    crumb_raw = request.args.get("crumb", "")
    breadcrumb: list[dict] = []
    if crumb_raw:
        try:
            breadcrumb = json.loads(unquote(crumb_raw))
        except (json.JSONDecodeError, ValueError):
            breadcrumb = []

    # 列举文件
    try:
        items = svc.client.list_files(parent, drive_id=current_drive_id)
    except Exception as exc:
        logger.warning("列举云盘文件失败: %s", exc)
        flash(f"列举云盘文件失败：{exc}", "error")
        items = []
    items.sort(key=lambda f: (f.type != "folder", f.name.lower()))
    return render_template(
        "files.html",
        items=items,
        parent=parent,
        path=path,
        drives=drives,
        current_drive=drive,
        current_drive_id=current_drive_id,
        breadcrumb=breadcrumb,
        crumb_raw=crumb_raw,
        crumb_encode=_crumb_encode,
    )


@bp.route("/create_folder", methods=["POST"])
def create_folder():
    svc = _services()
    parent = request.form.get("parent", "root")
    name = request.form.get("name", "").strip()
    drive = request.form.get("drive", "default")
    drive_id = request.form.get("drive_id") or None
    crumb = request.form.get("crumb", "")
    path = request.form.get("path", "/")
    if name:
        try:
            svc.client.create_folder(parent, name, drive_id=drive_id)
            flash(f"已创建文件夹「{name}」", "success")
        except Exception as exc:
            logger.warning("创建文件夹失败: %s", exc)
            flash(f"创建文件夹失败：{exc}", "error")
    return redirect(
        url_for("files.index", parent=parent, path=path, drive=drive, crumb=crumb)
    )


@bp.route("/rename", methods=["POST"])
def rename():
    svc = _services()
    file_id = request.form.get("file_id", "")
    new_name = request.form.get("new_name", "").strip()
    parent = request.form.get("parent", "root")
    drive = request.form.get("drive", "default")
    drive_id = request.form.get("drive_id") or None
    crumb = request.form.get("crumb", "")
    path = request.form.get("path", "/")
    if file_id and new_name:
        try:
            svc.client.rename_file(file_id, new_name, drive_id=drive_id)
            flash(f"已重命名为「{new_name}」", "success")
        except Exception as exc:
            logger.warning("重命名失败: %s", exc)
            flash(f"重命名失败：{exc}", "error")
    return redirect(
        url_for("files.index", parent=parent, path=path, drive=drive, crumb=crumb)
    )


@bp.route("/delete", methods=["POST"])
def delete():
    svc = _services()
    file_id = request.form.get("file_id", "")
    parent = request.form.get("parent", "root")
    drive = request.form.get("drive", "default")
    drive_id = request.form.get("drive_id") or None
    crumb = request.form.get("crumb", "")
    path = request.form.get("path", "/")
    if file_id:
        try:
            svc.client.delete_file(file_id, drive_id=drive_id)
            flash("已移入回收站", "success")
        except Exception as exc:
            logger.warning("删除失败: %s", exc)
            flash(f"删除失败：{exc}", "error")
    return redirect(
        url_for("files.index", parent=parent, path=path, drive=drive, crumb=crumb)
    )


@bp.route("/api/drives", methods=["GET"])
def api_drives():
    """JSON API：返回用户可用盘列表，供前端目录选择器使用。"""
    svc = _services()
    try:
        drives = svc.client.list_drives()
    except Exception as exc:
        logger.warning("API: 获取盘列表失败: %s", exc)
        return jsonify({"drives": [], "error": str(exc)}), 200
    return jsonify({"drives": drives})


@bp.route("/api/list", methods=["GET"])
def api_list():
    """JSON API：列举某目录下的文件夹，供前端目录树选择器使用。

    Query params:
        parent: 父目录 file_id（默认 "root"）
        drive:  default | resource | backup（默认 "default"）
        path:   当前目录显示路径（默认 "/"）
    Returns:
        {"items": [{"file_id": str, "name": str, "type": "folder"}], "path": str}
    """
    svc = _services()
    parent = request.args.get("parent", "root")
    drive = request.args.get("drive", "default")
    display_path = request.args.get("path", "/") or "/"

    # 解析盘 ID
    current_drive_id: Optional[str] = None
    try:
        drives = svc.client.list_drives()
        for d in drives:
            if d["drive_type"] == drive:
                current_drive_id = d["drive_id"]
                break
        if current_drive_id is None and drives:
            current_drive_id = drives[0]["drive_id"]
    except Exception as exc:
        logger.warning("API: 获取盘列表失败: %s", exc)
        return jsonify({
            "error": f"获取盘列表失败: {exc}",
            "hint": "请在设置页配置 refresh_token",
            "items": [],
            "path": display_path,
        }), 503

    try:
        items = svc.client.list_files(parent, drive_id=current_drive_id)
    except Exception as exc:
        logger.warning("API: 列举文件失败: %s", exc)
        err_str = str(exc)
        # token 完全缺失时给更明确提示
        hint = ""
        if "未找到 refresh_token" in err_str or "refresh_token" in err_str.lower():
            hint = "请在设置页配置 refresh_token 后再试"
        return jsonify({
            "error": err_str,
            "hint": hint,
            "items": [],
            "path": display_path,
        }), 503

    # 只返回文件夹
    folders = [
        {"file_id": f.file_id, "name": f.name, "type": f.type}
        for f in items
        if f.type == "folder"
    ]
    folders.sort(key=lambda f: f["name"].lower())
    return jsonify({"items": folders, "path": display_path, "parent": parent})


@bp.route("/download", methods=["POST"])
def download():
    svc = _services()
    file_id = request.form.get("file_id", "")
    parent = request.form.get("parent", "root")
    drive = request.form.get("drive", "default")
    drive_id = request.form.get("drive_id") or None
    crumb = request.form.get("crumb", "")
    path = request.form.get("path", "/")
    if not file_id:
        return redirect(
            url_for(
                "files.index", parent=parent, path=path, drive=drive, crumb=crumb
            )
        )
    try:
        url = svc.client.get_download_url(file_id, drive_id=drive_id)
        if svc.aria2.enabled and url:
            svc.aria2.add_uri([url], {"dir": request.form.get("path", "")})
            flash("已提交 Aria2 下载", "success")
        else:
            flash(f"下载直链：{url}", "info")
    except Exception as exc:
        logger.warning("获取下载链接失败: %s", exc)
        flash(f"获取下载链接失败：{exc}", "error")
    return redirect(
        url_for("files.index", parent=parent, path=path, drive=drive, crumb=crumb)
    )
