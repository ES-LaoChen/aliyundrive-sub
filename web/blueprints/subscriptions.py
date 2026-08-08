"""订阅蓝图：列表 / 新增 / 编辑 / 删除 / 立即检查 / 启停。"""
from __future__ import annotations

import logging
from typing import Optional

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

from core.naming import RENAME_MODES
from models import Setting, Subscription
from schemas import SubscriptionCreate
from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("subscriptions", __name__, url_prefix="/subscriptions")


def _services() -> Services:
    return current_app.config["SERVICES"]


def _get_kv(db, key: str, default: str = "") -> str:
    """从 Setting KV 表读取配置（与 settings_bp 同语义的本地辅助）。"""
    row = db.query(Setting).filter_by(key=key).first()
    return row.value if row else default


@bp.route("/", methods=["GET"], strict_slashes=False)
def index():
    svc = _services()
    status_filter = request.args.get("status", "all")
    with svc.session_factory() as db:
        q = db.query(Subscription)
        # 仅接受三态枚举值；无效值或不传（"all"）视为不过滤。
        if status_filter in ("active", "pending_update", "completed"):
            q = q.filter(Subscription.status == status_filter)
        else:
            status_filter = "all"
        subs = q.order_by(Subscription.id.desc()).all()
    return render_template(
        "subscriptions.html", subscriptions=subs, status_filter=status_filter
    )


@bp.route("/new", methods=["GET"])
def new():
    return render_template(
        "subscription_edit.html", sub=None, form=SubscriptionCreate().model_dump()
    )


@bp.route("", methods=["POST"])
def create():
    svc = _services()
    form = SubscriptionCreate(**request.form.to_dict())

    # 校验：必须通过目录选择器选定目标目录（target_folder_id 与 path 都必填）。
    if not form.target_folder_id or not form.target_folder_path:
        flash("请通过「浏览选择」按钮选定目标目录路径", "error")
        return redirect(url_for("subscriptions.new"))

    with svc.session_factory() as db:
        sub = Subscription(
            name=form.name,
            share_url=form.share_url,
            target_folder_id=form.target_folder_id,
            target_folder_path=form.target_folder_path,
            target_drive_type=form.target_drive_type or "",
            interval=form.interval or "3600",
            naming_template=form.naming_template,
            naming_regex=form.naming_regex,
            # 重命名规则：仅接受合法模式，否则归一为 none（保持原名旧行为）
            rename_mode=form.rename_mode if form.rename_mode in RENAME_MODES else "none",
            rename_prefix=form.rename_prefix,
            rename_suffix=form.rename_suffix,
            status=form.status or "active",
            remark=form.remark,
        )
        db.add(sub)
        db.commit()
        db.refresh(sub)
        sub_id = sub.id
        status = sub.status
    if status == "active":
        svc.scheduler.register(sub)
        # 新增即触发首次检查，便于即时验证配置并获取最新结果（无需等待调度周期）。
        try:
            svc.scheduler.trigger_once(sub_id)
        except Exception:
            logger.exception("新增订阅后自动触发检查失败（不影响创建）")
        flash(f"订阅「{form.name or sub_id}」已创建，并立即执行首次检查", "success")
    else:
        flash(f"订阅「{form.name or sub_id}」已创建", "success")
    return redirect(url_for("subscriptions.index"))


@bp.route("/<int:sub_id>/edit", methods=["GET"])
def edit(sub_id: int):
    svc = _services()
    with svc.session_factory() as db:
        sub = db.get(Subscription, sub_id)
    if sub is None:
        flash("订阅不存在", "error")
        return redirect(url_for("subscriptions.index"))
    form = {k: v for k, v in sub.__dict__.items() if not k.startswith("_")}
    return render_template("subscription_edit.html", sub=sub, form=form)


@bp.route("/<int:sub_id>/edit", methods=["POST"])
def update(sub_id: int):
    svc = _services()
    form = SubscriptionCreate(**request.form.to_dict())

    # 校验：必须通过目录选择器选定目标目录。
    if not form.target_folder_id or not form.target_folder_path:
        flash("请通过「浏览选择」按钮选定目标目录路径", "error")
        return redirect(url_for("subscriptions.edit", sub_id=sub_id))

    with svc.session_factory() as db:
        sub = db.get(Subscription, sub_id)
        if sub is None:
            flash("订阅不存在", "error")
            return redirect(url_for("subscriptions.index"))
        sub.name = form.name
        sub.share_url = form.share_url
        sub.target_folder_id = form.target_folder_id
        sub.target_folder_path = form.target_folder_path
        sub.target_drive_type = form.target_drive_type or sub.target_drive_type or ""
        sub.interval = form.interval or "3600"
        sub.naming_template = form.naming_template
        sub.naming_regex = form.naming_regex
        # 重命名规则：仅接受合法模式，否则归一为 none
        sub.rename_mode = form.rename_mode if form.rename_mode in RENAME_MODES else "none"
        sub.rename_prefix = form.rename_prefix
        sub.rename_suffix = form.rename_suffix
        sub.status = form.status or sub.status
        sub.remark = form.remark
        db.commit()
        new_status = sub.status
    # 同步调度任务。
    if new_status == "active":
        svc.scheduler.register(sub)
    else:
        svc.scheduler.unregister(sub_id)
    flash(f"订阅「{form.name or sub_id}」已保存", "success")
    return redirect(url_for("subscriptions.index"))


@bp.route("/<int:sub_id>/delete", methods=["POST"])
def delete(sub_id: int):
    svc = _services()
    svc.scheduler.unregister(sub_id)
    with svc.session_factory() as db:
        sub = db.get(Subscription, sub_id)
        if sub is None:
            flash("订阅不存在", "error")
            return redirect(url_for("subscriptions.index"))
        name = sub.name
        db.delete(sub)
        db.commit()
    flash(f"订阅「{name or sub_id}」已删除", "success")
    return redirect(url_for("subscriptions.index"))


@bp.route("/<int:sub_id>/check", methods=["POST"])
def check(sub_id: int):
    svc = _services()
    svc.scheduler.trigger_once(sub_id)
    flash(f"已触发订阅 #{sub_id} 的立即检查", "info")
    return redirect(url_for("subscriptions.index"))


@bp.route("/<int:sub_id>/toggle", methods=["POST"])
def toggle(sub_id: int):
    svc = _services()
    with svc.session_factory() as db:
        sub = db.get(Subscription, sub_id)
        if sub is None:
            flash("订阅不存在", "error")
            return redirect(url_for("subscriptions.index"))
        sub.status = "completed" if sub.status == "active" else "active"
        db.commit()
        new_status = sub.status
    if new_status == "active":
        svc.scheduler.register(sub)
    else:
        svc.scheduler.unregister(sub_id)
    flash(f"订阅 #{sub_id} 已{'启用' if new_status == 'active' else '标记完结'}", "info")
    return redirect(url_for("subscriptions.index"))
