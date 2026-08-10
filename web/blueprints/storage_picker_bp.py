"""存储目录与同步引擎蓝图：local 挂载管理 + 外部 AList 引擎管理 + 目录浏览。

对应 TaoSync 的存储目录管理页面。后端范围：local（真实驱动）+ 外部 OpenList/AList。
"""
from __future__ import annotations

import json
import logging

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("storage_picker_bp", __name__, url_prefix="/sync-storage")


def _services() -> Services:
    return current_app.config["SERVICES"]


def _sync():
    svc = _services()
    if svc.sync_service is None:
        raise RuntimeError("同步管理模块未初始化")
    return svc.sync_service


@bp.route("/", methods=["GET"], strict_slashes=False)
def index():
    sync = _sync()
    try:
        engines = sync.get_engine_list()
    except Exception as e:
        logger.exception("读取同步引擎失败")
        engines = []
        flash("读取同步引擎失败：{}".format(e), "error")
    return render_template("settings_storage.html", engines=engines)


@bp.route("/mounts/<int:engine_id>", methods=["GET"])
def mounts(engine_id: int):
    sync = _sync()
    try:
        data = sync.get_mount_list(engine_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(data)


@bp.route("/mount", methods=["POST"])
def add_mount():
    sync = _sync()
    data = dict(request.form.to_dict())
    config = _build_local_config(data)
    payload = {
        "engineId": int(data.get("engineId")),
        "name": data.get("name", "").strip(),
        "driverType": data.get("driverType", "local").strip().lower(),
        "config": config,
        "enabled": 1 if data.get("enabled") in ("on", "1") else 0,
    }
    try:
        sync.add_mount(payload)
        flash("存储目录已添加", "success")
    except Exception as e:
        logger.exception("添加存储目录失败")
        flash("添加失败：{}".format(e), "error")
    return redirect(url_for("storage_picker_bp.index"))


def _build_local_config(data):
    """从表单构造 local 驱动的 config。

    兼容两种填写方式：
    - 直接字段 ``root_path``（推荐，交互式选择器/手动输入都用它）；
    - 旧的 ``config`` JSON 字符串（高级用法，作为补充覆盖）。

    只有当 ``root_path`` 既不在表单里、又在 JSON 中缺失时才回退为空对象，
    由后续驱动校验抛出明确的「请填写根目录绝对路径」提示。
    """
    config_raw = data.get("config") or "{}"
    try:
        extra = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
    except (TypeError, ValueError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}

    root_path = (data.get("root_path") or "").strip()
    config = dict(extra)
    if root_path:
        config["root_path"] = root_path
    return config


@bp.route("/mount/<int:mount_id>", methods=["POST"])
def update_mount(mount_id: int):
    sync = _sync()
    data = dict(request.form.to_dict())
    config = _build_local_config(data)
    payload = {
        "id": mount_id,
        "engineId": int(data.get("engineId")),
        "name": data.get("name", "").strip(),
        "driverType": data.get("driverType", "local").strip().lower(),
        "config": config,
        "enabled": 1 if data.get("enabled") in ("on", "1") else 0,
    }
    try:
        sync.update_mount(payload)
        flash("存储目录已更新", "success")
    except Exception as e:
        logger.exception("更新存储目录失败")
        flash("更新失败：{}".format(e), "error")
    return redirect(url_for("storage_picker_bp.index"))


@bp.route("/mount/<int:mount_id>/delete", methods=["POST"])
def delete_mount(mount_id: int):
    sync = _sync()
    try:
        sync.remove_mount(mount_id)
        flash("存储目录已删除", "success")
    except Exception as e:
        flash("删除失败：{}".format(e), "error")
    return redirect(url_for("storage_picker_bp.index"))


@bp.route("/engine", methods=["POST"])
def add_engine():
    sync = _sync()
    data = dict(request.form.to_dict())
    engine = {
        "remark": data.get("remark", "").strip(),
        "url": data.get("url", "").strip(),
        "token": data.get("token", "").strip(),
        "engineType": data.get("engineType", "alist").strip().lower(),
    }
    try:
        sync.add_engine(engine)
        flash("同步引擎已添加", "success")
    except Exception as e:
        logger.exception("添加同步引擎失败")
        flash("添加失败：{}".format(e), "error")
    return redirect(url_for("storage_picker_bp.index"))


@bp.route("/engine/<int:engine_id>", methods=["POST"])
def update_engine(engine_id: int):
    sync = _sync()
    data = dict(request.form.to_dict())
    engine = {
        "id": engine_id,
        "remark": data.get("remark", "").strip(),
        "url": data.get("url", "").strip(),
        "token": data.get("token", "").strip(),
    }
    try:
        sync.update_engine(engine)
        flash("同步引擎已更新", "success")
    except Exception as e:
        flash("更新失败：{}".format(e), "error")
    return redirect(url_for("storage_picker_bp.index"))


@bp.route("/engine/<int:engine_id>/delete", methods=["POST"])
def delete_engine(engine_id: int):
    sync = _sync()
    try:
        sync.remove_engine(engine_id)
        flash("同步引擎已删除", "success")
    except Exception as e:
        flash("删除失败：{}".format(e), "error")
    return redirect(url_for("storage_picker_bp.index"))


@bp.route("/browse", methods=["GET"])
def browse():
    """返回某挂载下的目录层级（供前端选择器使用）。"""
    sync = _sync()
    engine_id = request.args.get("engineId")
    mount_name = request.args.get("mount")
    path = request.args.get("path", "/")
    try:
        from core.sync_storage.engine import get_client_by_id
        client = get_client_by_id(int(engine_id), _services().session_factory)
        entries = client.filePathList(path if path else "/")
        # 仅保留目录形态，便于前端拼接虚拟路径
        result = []
        for entry in entries:
            name = entry.get("path")
            if not name:
                continue
            vpath = "{}/{}".format(mount_name, name) if path in ("", "/") \
                else "{}{}/{}".format(path, mount_name, name)
            result.append({"name": name, "vpath": vpath})
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/local-browse", methods=["GET"])
def local_browse():
    """基于服务器本地文件系统浏览目录，供「本地存储目录」交互式选择器使用。

    参数：
      path：要列出的目录绝对路径。省略时返回本机所有盘符（Windows）或根（*nix）。
    返回：{ path, parent, dirs:[{name, path}], roots:[...] }
    """
    import os

    req_path = (request.args.get("path") or "").strip()
    try:
        if not req_path:
            # 返回根列表：Windows 盘符或 unix 根
            if os.name == "nt":
                import string
                roots = []
                for d in string.ascii_uppercase:
                    root = "{}:\\".format(d)
                    if os.path.isdir(root):
                        roots.append({"name": root, "path": root})
            else:
                roots = [{"name": "/", "path": "/"}]
            return jsonify({"path": "", "parent": "", "dirs": [], "roots": roots})

        base = os.path.abspath(req_path)
        if not os.path.isdir(base):
            return jsonify({"error": "目录不存在：{}".format(req_path)}), 400
        dirs = []
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if os.path.isdir(full):
                dirs.append({"name": name, "path": full})
        parent = os.path.dirname(base) if base not in ("/", "") else ""
        return jsonify({
            "path": base,
            "parent": parent,
            "dirs": dirs,
            "roots": [],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
