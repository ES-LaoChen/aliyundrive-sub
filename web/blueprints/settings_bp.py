"""设置蓝图：云盘凭证状态 / 凭证输入 / Telegram 通知 / 订阅状态巡检。

凭证区既展示状态（db_saved / drive_id），也支持前端手动
输入 refresh_token（POST /settings/token），保存到 DB 并尝试验证有效性。
Telegram 通知/订阅状态巡检可在此保存：写入 Setting KV 表（持久化）
并实时更新运行时对象。
"""
from __future__ import annotations

import logging

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

from models import Setting
from schemas import SettingsForm
from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("settings", __name__, url_prefix="/settings")


def _services() -> Services:
    return current_app.config["SERVICES"]


def _get_kv(db, key: str, default: str = "") -> str:
    row = db.query(Setting).filter_by(key=key).first()
    return row.value if row else default


def _set_kv(db, key: str, value: str) -> None:
    row = db.query(Setting).filter_by(key=key).first()
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.commit()


@bp.route("", methods=["GET"], strict_slashes=False)
def index():
    svc = _services()
    with svc.session_factory() as db:
        cfg = {
            # SubStatus 巡检配置（未配置回退默认值）。
            "link_fail_threshold": _get_kv(db, "link_fail_threshold", "3"),
            "sub_check_interval": _get_kv(db, "sub_check_interval", "3600"),
            "substatus_concurrency_enabled": _get_kv(db, "substatus_concurrency_enabled", "false"),
            "substatus_concurrency_workers": _get_kv(db, "substatus_concurrency_workers", "3"),
            "substatus_poll_wait_seconds": _get_kv(db, "substatus_poll_wait_seconds", "2"),
            # Telegram 频道监控通知（可选）：从 KV 读取，回退 env 默认值。
            "tg_notify_enabled": _get_kv(db, "tg_notify_enabled", "false"),
            "tg_notify_chat_id": _get_kv(db, "tg_notify_chat_id", svc.settings.TG_NOTIFY_CHAT_ID),
            "tg_bot_token": _get_kv(db, "tg_bot_token", svc.settings.TG_BOT_TOKEN),
        }
    # 凭证状态（不展示明文）。
    token = svc.token_store.get_token()
    token_status = {
        "db_saved": bool(token and token.refresh_token),
        "drive_id": token.drive_id if token else "",
    }
    return render_template("settings.html", cfg=cfg, token_status=token_status)


@bp.route("", methods=["POST"])
def save():
    svc = _services()
    form = SettingsForm(
        # SubStatus 巡检配置（checkbox 未勾选时表单不含该字段，回退默认 "false"）。
        link_fail_threshold=request.form.get("link_fail_threshold", "3"),
        sub_check_interval=request.form.get("sub_check_interval", "3600"),
        substatus_concurrency_enabled=request.form.get("substatus_concurrency_enabled", "false"),
        substatus_concurrency_workers=request.form.get("substatus_concurrency_workers", "3"),
        substatus_poll_wait_seconds=request.form.get("substatus_poll_wait_seconds", "2"),
        # Telegram 频道监控通知（可选）：checkbox 未勾选时表单不含 enabled 字段，回退 "false"。
        tg_notify_enabled=request.form.get("tg_notify_enabled", "false"),
        tg_notify_chat_id=request.form.get("tg_notify_chat_id", "").strip(),
        tg_bot_token=request.form.get("tg_bot_token", "").strip(),
    )
    try:
        with svc.session_factory() as db:
            # SubStatus：写入 5 个巡检 KV（运行参数每轮读 DB 热更新）。
            _set_kv(db, "link_fail_threshold", form.link_fail_threshold)
            _set_kv(db, "sub_check_interval", form.sub_check_interval)
            _set_kv(db, "substatus_concurrency_enabled", form.substatus_concurrency_enabled)
            _set_kv(db, "substatus_concurrency_workers", form.substatus_concurrency_workers)
            _set_kv(db, "substatus_poll_wait_seconds", form.substatus_poll_wait_seconds)
            # Telegram 频道监控通知（可选）KV 落库。
            _set_kv(db, "tg_notify_enabled", form.tg_notify_enabled)
            _set_kv(db, "tg_notify_chat_id", form.tg_notify_chat_id)
            # 浏览器不回显密码框，仅当用户实际填写了 Token 才覆盖，
            # 否则保留 KV 中既有值（避免保存其它设置时把已存的 Token 清空）。
            if form.tg_bot_token:
                _set_kv(db, "tg_bot_token", form.tg_bot_token)
            # 计算 TG 有效 token：优先用本次表单填写的，否则回退 KV 中已存的
            # （密码框不回显，保存其它设置时不应把已存的 Token 清空）。
            # 必须在会话仍开启时计算，避免块外使用已关闭会话（健壮性隐患，不依赖
            # SQLAlchemy「关闭后仍可复用连接」的实现细节）。
            effective_tg_token = form.tg_bot_token or _get_kv(db, "tg_bot_token", "")

        # 实时更新运行时对象（通知器）。
        # Webhook 通知功能已移除，此处不再调用 configure。
        # TG 通知热更新：开关开启且 token+chat_id 齐全才注册，否则降级（静默不发送）。
        if form.tg_notify_enabled == "true" and effective_tg_token and form.tg_notify_chat_id:
            svc.notifier.configure_telegram(effective_tg_token, form.tg_notify_chat_id)
        else:
            svc.notifier.configure_telegram("", "")
        # 热更新巡检周期 job（仅当 scheduler 提供 reschedule 时；
        # 运行参数阈值/并发/等待每轮读 DB 天然热更新，无需此处处理）。
        reschedule = getattr(svc.scheduler, "reschedule_substatus_poll", None)
        if callable(reschedule):
            try:
                reschedule(svc.session_factory)
            except Exception:
                logger.exception("热更新 substatus_poll 巡检周期失败（忽略）")
    except Exception as exc:
        logger.exception("保存设置失败")
        flash(f"保存设置失败：{exc}", "error")
        return redirect(url_for("settings.index"))

    flash("设置已保存", "success")
    return redirect(url_for("settings.index"))


@bp.route("/token", methods=["POST"])
def save_token():
    """接收前端输入的 refresh_token，保存到 DB 并验证。"""
    svc = _services()
    refresh_token = request.form.get("refresh_token", "").strip()
    if not refresh_token:
        flash("请输入 refresh_token", "error")
        return redirect(url_for("settings.index"))

    # 保存到 DB（仅写 refresh_token 列，不覆盖已有 access_token / drive_id）。
    svc.token_store.save_refresh_token(refresh_token)

    # 尝试验证 token（刷新 access_token）。
    try:
        svc.client.refresh_access_token()
        flash("refresh_token 已保存并验证成功", "success")
    except Exception as exc:
        flash(f"refresh_token 已保存，但验证失败：{exc}", "warning")

    return redirect(url_for("settings.index"))


# ============================================================
# 存储目录（挂载）管理：同步引擎的多后端存储配置入口。
# ============================================================
def _sync_service():
    svc = _services()
    sync = getattr(svc, "sync_service", None)
    if sync is None:
        raise RuntimeError("同步服务未初始化")
    return sync


@bp.route("/storage", methods=["GET"])
def storage():
    """存储目录管理：列出已有挂载 + 新增表单。"""
    sync = _sync_service()
    try:
        mounts = sync.get_mount_list()
        drivers = sync.get_supported_drivers()
        engine_id = sync.get_system_engine_id()
    except Exception:
        logger.exception("读取存储目录失败")
        mounts, drivers, engine_id = [], [], 0
    return render_template(
        "settings_storage.html",
        mounts=mounts, drivers=drivers, engine_id=engine_id,
    )


@bp.route("/storage", methods=["POST"])
def storage_add():
    """新增存储目录（挂载）。config 以 JSON 文本提交。"""
    sync = _sync_service()
    name = (request.form.get("name") or "").strip()
    driver_type = (request.form.get("driverType") or "").strip().lower()
    config_raw = (request.form.get("config") or "").strip()
    enabled = 1 if request.form.get("enabled") == "1" else 0
    if not name or not driver_type:
        flash("请填写名称与驱动类型", "error")
        return redirect(url_for("settings.storage"))
    try:
        import json as _json
        config = _json.loads(config_raw) if config_raw else {}
        if not isinstance(config, dict):
            raise ValueError("config 必须是 JSON 对象")
    except Exception as exc:
        flash(f"config JSON 解析失败：{exc}", "error")
        return redirect(url_for("settings.storage"))
    try:
        data = {
            "engineId": sync.get_system_engine_id(),
            "name": name,
            "driverType": driver_type,
            "config": config,
            "enabled": enabled,
        }
        sync.add_mount(data)
        flash(f"存储目录「{name}」已添加", "success")
    except Exception as exc:
        logger.exception("新增存储目录失败")
        flash(f"添加失败：{exc}", "error")
    return redirect(url_for("settings.storage"))


@bp.route("/storage/<int:mount_id>/delete", methods=["POST"])
def storage_delete(mount_id: int):
    sync = _sync_service()
    try:
        sync.remove_mount(mount_id)
        flash("存储目录已删除", "success")
    except Exception as exc:
        logger.exception("删除存储目录失败")
        flash(f"删除失败：{exc}", "error")
    return redirect(url_for("settings.storage"))



