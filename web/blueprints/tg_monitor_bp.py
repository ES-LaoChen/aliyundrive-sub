"""Telegram 频道监控看板蓝图（T-TG UI）。

提供：
- ``GET /``：监控配置表单 + 实时状态面板（复用 ``Setting`` KV 持久化）。
- ``POST /``：保存配置（写入 KV + 热更新运行时 settings / tg_monitor）。
- ``POST /trigger``：手动立即轮询一次（调用 ``tg_monitor.poll_all``）。
- ``GET /api/status``：返回 JSON 监控状态，供前端 / 外部轮询。

注意：Bot Token 与通知 Chat ID 由系统设置页面统一管理（``settings_bp``），
本蓝图不涉及通知渠道的热更新，避免重复配置产生冲突。

遵循 ``settings_bp`` 的范式：用 ``Setting`` KV 表做持久化，GET 读 KV（fallback
``svc.settings.*``），POST 写 KV 并热更新运行时对象。
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from models import Setting, Subscription, TGMonitorState, TGMonitorLog
from core.tg_channel_parser import TgChannelParser
from web.services import Services

logger = logging.getLogger(__name__)

bp = Blueprint("tg_monitor", __name__, url_prefix="/tg-monitor")


# ===================== KV 读写（与 settings_bp 同范式） =====================
def _services() -> Services:
    """从 Flask app 配置取出 ``Services`` 容器。"""
    return current_app.config["SERVICES"]


def _get_kv(db, key: str, default: str = "") -> str:
    """读取 ``Setting`` KV；缺省返回 default。"""
    row = db.query(Setting).filter_by(key=key).first()
    return row.value if row else default


def _set_kv(db, key: str, value: str) -> None:
    """写入 ``Setting`` KV（存在则更新，不存在则插入）。"""
    row = db.query(Setting).filter_by(key=key).first()
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def _to_bool(value: Any) -> bool:
    """把任意值解析为布尔（兼容 'on'/'true'/'1'/'yes' 与 py 布尔）。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _to_int(value: Any, default: int = 0) -> int:
    """把任意值解析为 int，失败回退 default。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ===================== 运行时热更新 =====================
def _apply_runtime_config(svc: Services, cfg: Dict[str, Any]) -> None:
    """把保存后的配置热更新到运行时对象（settings / tg_monitor）。

    注意：Bot Token 与 Chat ID 由系统设置页面统一管理，此处不更新通知渠道。
    通知配置（`configure_telegram`）由 ``settings_bp.save`` 调用。

    pydantic-settings 实例默认允许属性赋值（pydantic v2，未设置 ``frozen``）；
    ``tg_monitor`` 内部每次都从 ``self._settings`` 实时读取配置，故直接改
    ``svc.settings.TG_*`` 即可令 UI 开关 / 频道变更免重启生效。
    """
    settings = svc.settings
    try:
        settings.TG_MONITOR_ENABLED = _to_bool(cfg["tg_enabled"])
        settings.TG_MONITOR_CHANNELS = (cfg["tg_channels"] or "").strip()
        settings.TG_POLL_INTERVAL = _to_int(cfg["tg_poll_interval"], 300)
        # Bot Token 与 Chat ID 由系统设置页面统一管理，此处不更新。
        settings.TG_PROXY = (cfg["tg_proxy"] or "").strip()

    except (AttributeError, TypeError, KeyError):
        # 极罕见：settings 被冻结或 cfg 缺字段时降级为仅刷新 tg_monitor 内部缓存，
        # 不再抛未捕获异常导致 500。
        logger.warning("无法直接更新 settings 属性，尝试经 tg_monitor.reconfigure 刷新")

    # 通知配置（Bot Token / Chat ID）由系统设置页面统一管理，此处不热更新。
    # 刷新 tg_monitor 内部缓存（proxy 变化需重建 fetcher）；若尚未构造则懒构造。
    if svc.tg_monitor is not None:
        reconf = getattr(svc.tg_monitor, "reconfigure", None)
        if callable(reconf):
            try:
                reconf()
            except Exception:
                logger.exception("tg_monitor.reconfigure 失败")
    else:
        # 启用且配置了频道时，懒构造 tg_monitor 并回填 scheduler，实现真正免重启。
        if _to_bool(cfg["tg_enabled"]) and (cfg["tg_channels"] or "").strip():
            _lazy_build_tg_monitor(svc)


def _lazy_build_tg_monitor(svc: Services) -> None:
    """当 ``tg_monitor`` 尚未构造但配置已满足启用条件时，懒构造并回填 scheduler。"""
    try:
        from core.tg_monitor import TGMonitorService

        svc.tg_monitor = TGMonitorService(
            svc.settings, svc.session_factory, svc.scheduler, svc.notifier
        )
        sched = getattr(svc.scheduler, "scheduler", None)
        if sched is not None and hasattr(sched, "add_job"):
            interval = max(60, _to_int(getattr(svc.settings, "TG_POLL_INTERVAL", 300), 300))
            sched.add_job(
                svc.tg_monitor.poll_all,
                "interval",
                seconds=interval,
                id="tg_monitor_poll",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        logger.info("已懒构造 TGMonitorService 并注册轮询任务")
    except Exception:
        logger.exception("懒构造 TGMonitorService 失败（保存仍生效）")


# ===================== 状态查询 =====================
def _count_processed(svc: Services) -> int:
    """统计 ``tg_monitor_state`` 中已处理链接总数（用于 trigger 反馈新链接数）。"""
    total = 0
    try:
        with svc.session_factory() as db:
            for row in db.query(TGMonitorState).all():
                try:
                    total += len(json.loads(row.processed_links or "[]") or [])
                except Exception:
                    pass
    except Exception:
        pass
    return total


def _build_status(svc: Services) -> Dict[str, Any]:
    """构造看板状态数据（enabled / 各频道明细 / 已建订阅数 / job 状态 / 周期）。"""
    enabled = False
    if svc.tg_monitor is not None:
        try:
            enabled = bool(svc.tg_monitor.enabled())
        except Exception:
            enabled = False

    channels: List[Dict[str, Any]] = []
    if svc.tg_monitor is not None:
        try:
            channel_list = list(svc.tg_monitor._channels())
        except Exception:
            logger.exception("读取频道列表失败")
            channel_list = []

        for ch in channel_list:
            last_message_id = 0
            processed_count = 0
            updated_at: Optional[Any] = None
            latest_text = ""
            latest_links: List[str] = []
            try:
                with svc.session_factory() as db:
                    row = db.query(TGMonitorState).filter_by(channel=ch).first()
                    if row is not None:
                        last_message_id = row.last_message_id or 0
                        if row.processed_links:
                            try:
                                processed_count = len(json.loads(row.processed_links) or [])
                            except Exception:
                                processed_count = 0
                        updated_at = row.updated_at
                    # 抓取该频道最近一条「真实推送内容」（content 非空），解析出：
                    # 正文（去除分享链接后的描述文本）+ 链接（原文中抽取的全部分享链接）。
                    # 只用真正的频道推送，不回退到“轮询完成 / 系统状态”等 detail 信息，
                    # 避免「频道明细」表把状态消息当成推送内容展示。
                    latest = (
                        db.query(TGMonitorLog)
                        .filter_by(channel=ch)
                        .filter(TGMonitorLog.content.isnot(None), TGMonitorLog.content != "")
                        .order_by(TGMonitorLog.created_at.desc(), TGMonitorLog.id.desc())
                        .first()
                    )
                    if latest is not None:
                        content = (getattr(latest, "content", "") or "").strip()
                        stored_link = (getattr(latest, "link", "") or "").strip()
                        links = TgChannelParser.extract_share_links(content) if content else []
                        if stored_link and stored_link not in links:
                            links = [stored_link] + links
                        text = TgChannelParser.strip_share_links(content) if content else ""
                        latest_text = text
                        latest_links = links

            except Exception:
                logger.exception("查询频道 %s 状态失败", ch)
            channels.append(
                {
                    "channel": ch,
                    "last_message_id": last_message_id,
                    "processed_count": processed_count,
                    "updated_at": updated_at,
                    "latest_text": latest_text,
                    "latest_links": latest_links,
                }
            )




    # 轮询 job 是否注册（scheduler 暴露 .scheduler 为 APScheduler 实例）。
    poll_job_active: Optional[bool] = None
    sched = getattr(svc.scheduler, "scheduler", None)
    if sched is not None and hasattr(sched, "get_job"):
        try:
            poll_job_active = sched.get_job("tg_monitor_poll") is not None
        except Exception:
            poll_job_active = None

    return {
        "enabled": enabled,
        "channels": channels,
        "poll_job_active": poll_job_active,
        "poll_interval": _to_int(getattr(svc.settings, "TG_POLL_INTERVAL", 300), 300),
    }


# ===================== 监控日志（T-TG 日志模块） =====================
def _parse_dt(s: Optional[str]) -> datetime:
    """把查询参数里的时间字符串解析为 ``datetime``。

    支持 ``YYYY-MM-DDTHH:MM`` 与 ``YYYY-MM-DD``；末位 ``Z`` 视为 UTC。
    非法输入抛 ``ValueError``（调用处 try/except 忽略该条件）。
    """
    if not s:
        raise ValueError("empty datetime")
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _log_row_to_dict(row: Any) -> Dict[str, Any]:
    """把 ``TGMonitorLog`` 行转换为 JSON 友好的 dict。"""
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "channel": row.channel,
        "message_id": row.message_id,
        "message_type": row.message_type,
        "event_type": row.event_type,
        "status": row.status,
        "link": row.link,
        "detail": row.detail,
    }


def _query_logs(svc: Services, args: Any, page: int, per_page: int):
    """按筛选条件分页查询 TG 监控日志。

    供 ``/logs`` / ``/api/logs`` / ``/api/logs/export`` 三个路由复用。

    Args:
        svc: 服务容器。
        args: ``request.args``（MultiDict）或等价映射。
        page: 页码（从 1 开始）。
        per_page: 每页条数（导出时传大值拉全量）。

    Returns:
        ``(logs, total)``：当页日志 dict 列表 + 匹配总数。
    """
    channel = (args.get("channel") or "").strip()
    status = (args.get("status") or "").strip()
    event_type = (args.get("type") or args.get("event_type") or "").strip()
    message_type = (args.get("message_type") or "").strip()
    start = (args.get("start") or "").strip()
    end = (args.get("end") or "").strip()

    with svc.session_factory() as db:
        q = db.query(TGMonitorLog)
        if channel:
            q = q.filter_by(channel=channel)
        if status:
            q = q.filter_by(status=status)
        if event_type:
            q = q.filter_by(event_type=event_type)
        if message_type:
            q = q.filter_by(message_type=message_type)
        if start:
            try:
                q = q.filter(TGMonitorLog.created_at >= _parse_dt(start))
            except ValueError:
                pass
        if end:
            try:
                q = q.filter(TGMonitorLog.created_at <= _parse_dt(end))
            except ValueError:
                pass
        total = q.count()
        rows = (
            q.order_by(TGMonitorLog.created_at.desc())
            .limit(per_page)
            .offset((page - 1) * per_page)
            .all()
        )
        logs = [_log_row_to_dict(r) for r in rows]
    return logs, total


def _build_log_url(base_args: Dict[str, Any], page: int) -> str:
    """构造带筛选条件与页码的日志页 URL。"""
    params = {**base_args, "page": page}
    return url_for("tg_monitor.logs") + "?" + urlencode(params)


# ===================== 路由 =====================
@bp.route("", methods=["GET"])
def index() -> str:
    """看板页：渲染配置表单 + 实时状态面板。"""
    svc = _services()
    with svc.session_factory() as db:
        cfg = {
            "tg_enabled": _to_bool(
                _get_kv(db, "tg_monitor_enabled", svc.settings.TG_MONITOR_ENABLED)
            ),
            "tg_channels": _get_kv(
                db, "tg_monitor_channels", svc.settings.TG_MONITOR_CHANNELS
            ),
            "tg_poll_interval": _to_int(
                _get_kv(db, "tg_poll_interval", svc.settings.TG_POLL_INTERVAL), 300
            ),
            # 代理地址（爬取用）。
            "tg_proxy": _get_kv(db, "tg_proxy", svc.settings.TG_PROXY),
        }
    status = _build_status(svc)
    return render_template("tg_monitor.html", cfg=cfg, status=status)


@bp.route("", methods=["POST"])
def save() -> Any:
    """保存监控配置：写入 KV 并热更新运行时。"""
    svc = _services()
    cfg = {
        "tg_enabled": request.form.get("tg_enabled", "") not in ("", "off", "false", "0"),
        "tg_channels": request.form.get("tg_channels", ""),
        "tg_poll_interval": request.form.get("tg_poll_interval", ""),
        "tg_proxy": request.form.get("tg_proxy", ""),
    }
    with svc.session_factory() as db:
        _set_kv(db, "tg_monitor_enabled", "true" if cfg["tg_enabled"] else "false")
        _set_kv(db, "tg_monitor_channels", cfg["tg_channels"].strip())
        # Bot Token 与 Chat ID 由系统设置页面统一管理，此处不再重复写入。
        _set_kv(db, "tg_poll_interval", str(_to_int(cfg["tg_poll_interval"], 300)))

        _set_kv(db, "tg_proxy", cfg["tg_proxy"].strip())

    _apply_runtime_config(svc, cfg)
    flash("Telegram 监控配置已保存", "success")
    return redirect(url_for("tg_monitor.index"))


@bp.route("/trigger", methods=["POST"])
def trigger() -> Any:
    """手动立即轮询一次。"""
    svc = _services()
    if svc.tg_monitor is None:
        flash("Telegram 监控未启用（未配置或未启动）", "warning")
        return redirect(url_for("tg_monitor.index"))
    try:
        before = _count_processed(svc)
        svc.tg_monitor.poll_all()
        after = _count_processed(svc)
        new_links = max(0, after - before)
        flash(f"已触发轮询，新增 {new_links} 条链接", "success")
    except Exception as exc:
        logger.exception("手动触发 TG 轮询失败")
        flash(f"轮询失败：{exc}", "error")
    return redirect(url_for("tg_monitor.index"))


@bp.route("/api/status", methods=["GET"])
def api_status() -> Any:
    """监控 API 数据接口：返回 JSON 状态。"""
    svc = _services()
    status = _build_status(svc)
    return jsonify(
        {
            "enabled": status["enabled"],
            "channels": status["channels"],
            "poll_job_active": status["poll_job_active"],
            "poll_interval": status["poll_interval"],
        }
    )


# ===================== 监控日志路由（T-TG 日志模块） =====================
@bp.route("/logs", endpoint="logs", methods=["GET"])
def logs_view() -> str:
    """监控日志列表页：筛选表单 + 表格 + 分页。"""
    svc = _services()
    page = _to_int(request.args.get("page"), 1) or 1
    logs, total = _query_logs(svc, request.args, page, 100)

    # 频道下拉：取自 tg_monitor._channels()（若可用）。
    channels: List[str] = []
    if svc.tg_monitor is not None:
        try:
            channels = list(svc.tg_monitor._channels())
        except Exception:
            logger.exception("读取 TG 频道列表失败")

    filters: Dict[str, str] = {
        "channel": (request.args.get("channel") or "").strip(),
        "message_type": (request.args.get("message_type") or "").strip(),
        "status": (request.args.get("status") or "").strip(),
        "type": (request.args.get("type") or "").strip(),
        "event_type": (request.args.get("event_type") or "").strip(),
        "start": (request.args.get("start") or "").strip(),
        "end": (request.args.get("end") or "").strip(),
    }

    # 分页链接：保留筛选条件（去掉 page）。
    base_args = {k: v for k, v in request.args.items() if k != "page"}
    prev_url = _build_log_url(base_args, page - 1) if page > 1 else None
    next_url = _build_log_url(base_args, page + 1) if (page * 100) < total else None

    return render_template(
        "tg_monitor_logs.html",
        logs=logs,
        channels=channels,
        total=total,
        page=page,
        filters=filters,
        prev_url=prev_url,
        next_url=next_url,
    )


@bp.route("/channel/<channel>", endpoint="channel_detail", methods=["GET"])
def channel_detail_view(channel: str) -> str:
    """频道日记：按时间轴展示该频道推送的具体内容信息。

    数据来源为 ``TGMonitorLog``（按频道过滤、按时间倒序）。每条日志的 ``content`` 字段
    保存的是频道推送的**原文**，本页不直接把原文当作展示主体，而是先解析它：

    - 用 ``TgChannelParser.extract_share_links`` 抽取推送里包含的**全部分享链接**；
    - 用 ``TgChannelParser.strip_share_links`` 剥离链接，得到可读的**描述性正文**；

    从而把「一条监控事件」还原成「频道到底推送了什么内容 + 其中的链接是什么」，
    让用户在时间轴上直接阅读频道真实推送，而不是只看到事件类型或消息 ID。
    """
    with get_session() as db:
        state = db.query(TGMonitorState).filter_by(channel=channel).first()
        raw_entries = (
            db.query(TGMonitorLog)
            .filter_by(channel=channel)
            .order_by(TGMonitorLog.created_at.desc(), TGMonitorLog.id.desc())
            .limit(300)
            .all()
        )
        all_channels = [
            r[0] for r in db.query(TGMonitorState.channel)
            .order_by(TGMonitorState.channel).all()
        ]
    processed_count = 0
    if state is not None and state.processed_links:
        try:
            processed_count = len(json.loads(state.processed_links))
        except (ValueError, TypeError):
            processed_count = 0
    entries = _build_diary_entries(raw_entries)
    # 最近一次「真实推送」（用于概览卡片直接展示内容，而非系统状态/消息 ID）。
    # 优先取带正文或链接的条目，跳过“轮询完成”等无内容的系统事件。
    latest = next((e for e in entries if e.get("text") or e.get("links")), None)
    return render_template(
        "tg_monitor_channel.html",
        channel=channel,
        state=state,
        processed_count=processed_count,
        entries=entries,
        latest=latest,
        all_channels=all_channels,
    )


def _build_diary_entries(raw_entries: List[Any]) -> List[Dict[str, Any]]:
    """把 ``TGMonitorLog`` 事件聚合为「频道真实推送内容」的展示条目。

    关键差异（相对原实现）：不再把 ``message_id`` / 事件类型当作主体，而是从存储的
    ``content``（推送原文）中解析出：

    - ``text``：去除分享链接后的描述性正文（频道实际说了什么）；
    - ``links``：推送中包含的全部阿里云盘分享链接（含单条日志单独记录的 ``link``）。

    若原文与链接都为空，则回退用事件 ``detail`` 作为内容说明，保证时间轴不为空。
    """
    out: List[Dict[str, Any]] = []
    for e in raw_entries:
        content = (getattr(e, "content", "") or "").strip()
        stored_link = (getattr(e, "link", "") or "").strip()
        # 1) 解析真实推送内容：抽取全部分享链接。
        links = TgChannelParser.extract_share_links(content) if content else []
        if stored_link and stored_link not in links:
            links = [stored_link] + links
        # 2) 剥离链接，得到可读的描述性正文。
        text = TgChannelParser.strip_share_links(content) if content else ""
        detail = (getattr(e, "detail", "") or "").strip()
        # 3) 兜底：既无正文也无链接时，用事件说明填充内容。
        if not text and not links and detail:
            text = detail
        out.append({
            "id": e.id,
            "created_at": e.created_at,
            "message_id": e.message_id,
            "event_type": e.event_type,
            "status": e.status,
            "message_type": e.message_type,
            "text": text,
            "links": links,
            "detail": detail,
        })
    return out



@bp.route("/api/logs", methods=["GET"])
def api_logs() -> Any:
    """监控日志 JSON 接口：分页 + 筛选，返回 ``{total, page, logs}``。"""
    svc = _services()
    page = _to_int(request.args.get("page"), 1) or 1
    logs, total = _query_logs(svc, request.args, page, 100)
    return jsonify({"total": total, "page": page, "logs": logs})


@bp.route("/api/logs/export", methods=["GET"])
def api_logs_export() -> Any:
    """监控日志 CSV 导出：按相同筛选条件拉取全部匹配行。"""
    svc = _services()
    # 大 per_page 拉全量（导出不受单页 100 限制）。
    logs, _total = _query_logs(svc, request.args, 1, 10_000)

    output = io.StringIO()
    output.write("\ufeff")  # BOM：提升 Excel 中文兼容性。
    writer = csv.writer(output)
    writer.writerow(["时间", "频道", "消息ID", "消息类型", "事件类型", "状态", "链接", "说明"])
    for row in logs:
        writer.writerow([
            row["created_at"],
            row["channel"],
            row["message_id"] if row["message_id"] is not None else "",
            row["message_type"],
            row["event_type"],
            row["status"],
            row["link"],
            row["detail"],
        ])
    body = output.getvalue()
    resp = make_response(body)
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=tg_monitor_logs.csv"
    return resp


@bp.route("/logs/clear", methods=["POST"])
def logs_clear() -> Any:
    """清空全部 Telegram 监控日志。
    使用 DELETE FROM（非 TRUNCATE，兼容 SQLite），完成后 flash 提示。
    """
    svc = _services()
    try:
        with svc.session_factory() as db:
            deleted = db.query(TGMonitorLog).delete()
            db.commit()
        flash(f"已清空 {deleted} 条监控日志", "success")
    except Exception as exc:
        logger.exception("清空 TG 监控日志失败")
        flash(f"清空失败：{exc}", "error")
    return redirect(url_for("tg_monitor.logs"))
