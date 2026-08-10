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
    config_raw = data.get("config") or "{}"
    try:
        config = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
    except (TypeError, ValueError):
        config = {}
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


@bp.route("/mount/<int:mount_id>", methods=["POST"])
def update_mount(mount_id: int):
    sync = _sync()
    data = dict(request.form.to_dict())
    config_raw = data.get("config") or "{}"
    try:
        config = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
    except (TypeError, ValueError):
        config = {}
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
