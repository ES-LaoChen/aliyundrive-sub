"""Flask 应用工厂：注册蓝图、挂载模板与静态资源。

Web 不做应用内鉴权（主理人决策 #1 / 架构决策 #2），
由反代层负责 basic auth；本模块不校验任何身份。
"""
from __future__ import annotations

import logging
from typing import Optional

from flask import Flask, jsonify, redirect, render_template, current_app
from sqlalchemy import text

from web.services import Services

logger = logging.getLogger(__name__)


def create_app(services: Services) -> Flask:
    """构造 Flask 应用并注册全部蓝图。

    Args:
        services: 运行时服务容器（来自 ``app.build_services``）。
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SERVICES"] = services
    app.secret_key = "aliyundrive-sub-dev"  # 仅用于 flash 消息

    # ── Jinja 过滤器：UTC → 本地时区 ──
    # DB 用 ``db.utc_now()`` 写入的 naive datetime（值是 UTC）。
    # 模板直接 ``{{ x }}`` 会按字面显示（11:57），用户看是 8 小时前。
    # 此过滤器把 UTC 视为 timezone-aware，再 astimezone 到本地时区后格式化。
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Asia/Shanghai")  # 与系统时区保持一致（容器/CI 默认 UTC 时也安全）

    @app.template_filter("localtime")
    def _localtime_filter(value, fmt="%Y-%m-%d %H:%M:%S"):
        if value is None:
            return ""
        # naive datetime：按 UTC 解释
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(LOCAL_TZ).strftime(fmt)
        return str(value)

    # 蓝图延迟导入，避免与 app 工厂循环依赖。
    from web.blueprints.subscriptions import bp as sub_bp
    from web.blueprints.settings_bp import bp as settings_bp
    from web.blueprints.files_bp import bp as files_bp
    from web.blueprints.logs_bp import bp as logs_bp
    from web.blueprints.subscription_detail_bp import bp as sub_detail_bp
    from web.blueprints.tg_monitor_bp import bp as tg_monitor_bp
    from web.blueprints.sync_bp import bp as sync_bp
    from web.blueprints.storage_picker_bp import bp as storage_picker_bp

    app.register_blueprint(sub_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(sub_detail_bp)
    app.register_blueprint(tg_monitor_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(storage_picker_bp)

    @app.route("/")
    def index():
        """仪表盘：聚合订阅 / 转存 / 监控的核心统计，失败时降级为全 0 不阻断页面。"""
        stats = dict(
            sub_total=0, sub_active=0, sub_completed=0,
            transfer_success=0, transfer_failed=0,
            tg_logs=0, token_saved=False,
            version="dev",
        )
        try:
            services = current_app.config.get("SERVICES")
            if services is not None:
                with services.session_factory() as session:
                    from models import (Subscription, TransferRecord,
                                        TGMonitorLog, Token)
                    stats["sub_total"] = session.query(Subscription).count()
                    stats["sub_active"] = session.query(Subscription).filter(Subscription.status == "active").count()
                    stats["sub_completed"] = session.query(Subscription).filter(Subscription.status == "completed").count()
                    stats["transfer_success"] = session.query(TransferRecord).filter(TransferRecord.status == "success").count()
                    stats["transfer_failed"] = session.query(TransferRecord).filter(TransferRecord.status == "failed").count()
                    stats["tg_logs"] = session.query(TGMonitorLog).count()
                    tok = session.query(Token).first()
                    stats["token_saved"] = bool(tok and tok.refresh_token)
                stats["version"] = getattr(services, "version", "dev")
        except Exception:
            logger.exception("仪表盘统计失败")
        return render_template("dashboard.html", **stats)

    @app.route("/healthz")
    def healthz() -> tuple:
        """运行期健康检查（无需鉴权，仅应在内网 / 反代后暴露）。

        尝试用 ``services.session_factory()`` 执行 ``SELECT 1`` 验证 DB 可达：
          - 成功：200 ``{"status":"ok","db":"up"}``
          - 失败：503 ``{"status":"error","detail":"<简短原因>"}``
        """
        try:
            with services.session_factory() as session:
                session.execute(text("SELECT 1"))
            return jsonify(status="ok", db="up"), 200
        except Exception as exc:  # noqa: BLE001  # 健康检查需捕获任意 DB 异常
            return jsonify(status="error", detail=str(exc)), 503

    @app.context_processor
    def _inject() -> dict:
        return {"app_name": "阿里云盘订阅转存"}

    logger.info("Web 应用已创建，蓝图已注册")
    return app
