"""Telegram 公开频道监控自动转存服务（T-TG）。

职责：
1. 按配置周期爬取 ``https://t.me/s/{channel}`` 页面（经可注入的 ``TgFetcher``）。
2. 解析消息，提取阿里云盘分享链接。
3. 命中链接 → 与「订阅管理中的全部订阅」做名称联动：
   - 逻辑 1+2：若某条已有订阅的 ``name`` 是消息 ``blob`` 的子串 → 仅更新其
     ``share_url``（不新建），并在链接确变更后立即 ``trigger_once`` 即时转存，
     避免分享链接在下一轮统一轮询前失效。


   - 逻辑 3：所有含阿里云盘链接的频道消息直接进入名称联动，不再做关键词过滤。
4. 去重状态持久化到 ``tg_monitor_state`` 表，重启不重复触发 / 不重复转存。

设计要点：
- 所有外部依赖（session_factory / scheduler / notifier / fetcher）经构造函数注入，
  便于单测用 stub 替代、不依赖真实网络 / 真实阿里云盘。注意：本监控服务自身
  **不再主动发通知**（命中 / 跳过均不弹通知），通知统一由设置模块负责；``notifier``
  仅保留注入以兼容调用方与未来扩展。
- 监控与转存解耦：本服务只负责「发现 + 名称联动 + 更新链接 + 触发」，
  真实转存逻辑完全复用 ``SchedulerService``（由 trigger_once 调用）。
- ``poll_channel`` 内复用同一会话：一次性把全部订阅载入内存（``subs_snapshot``），
  后续名称匹配在内存做，避免每条 link 都开 session。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, List, Optional, Set

from core.tg_channel_parser import TgChannelParser
from core.tg_http import TgFetcher
from db import utc_now
from models import Subscription, TGMonitorState, TGMonitorLog

logger = logging.getLogger(__name__)

# 资源名最大长度（避免超长 blob 直接当订阅名）。
_MAX_RESOURCE_NAME_LEN = 120




@dataclass
class _Row:
    """内存中的单频道监控状态（最后消息 id + 已处理链接集合）。"""

    last_message_id: int = 0
    processed: Set[str] = field(default_factory=set)


class TGMonitorService:
    """Telegram 频道监控自动转存编排器。"""

    def __init__(
        self,
        settings: Any,
        session_factory: Any,
        scheduler: Any,
        notifier: Any,
        fetcher: Optional["TgFetcher"] = None,
    ) -> None:
        """构造监控服务。

        Args:
            settings: ``config.Settings`` 实例（实时读取 TG_* 配置）。
            session_factory: ``Callable[[], Session]``，返回数据库会话。
            scheduler: ``SchedulerService`` 实例（用于 register / trigger_once）。
            notifier: 注入的通知器（保留以兼容调用方 / 未来扩展；本监控**不再主动发通知**，
            命中与跳过均不弹通知，通知由设置模块统一负责）。
            fetcher: ``TgFetcher`` 或兼容 stub；为空则按 settings.TG_PROXY 构造真实抓取器。
        """
        self._settings = settings
        self._session_factory = session_factory
        self._scheduler = scheduler  # SchedulerService
        self._notifier = notifier  # 保留注入以兼容调用方 / 未来扩展（当前监控不再主动发通知）
        self._fetcher = fetcher or TgFetcher(proxy=getattr(settings, "TG_PROXY", "") or "")
        # channel -> 内存状态（每次 poll_all 从 DB 载入 / 回写）。
        self._state: dict[str, _Row] = {}
        # 跨频道全局已处理链接集合（进程内去重，避免同链接在多频道重复建订阅）。
        self._global_processed: Set[str] = set()
        # 状态读写锁（Bot 实时线程与轮询线程并发保护 self._state / 落库）。
        self._lock = threading.RLock()
        # Bot 实时监听线程与监听器句柄（start() 时创建；None 表示未启动）。
        self._bot_thread = None
        self._bot_listener = None

    # ===================== 对外接口 =====================
    def enabled(self) -> bool:
        """功能是否启用：总开关开启且至少配置了一个有效频道。"""
        return bool(getattr(self._settings, "TG_MONITOR_ENABLED", False)) and bool(self._channels())

    def reconfigure(self) -> None:
        """运行时热更新内部缓存（不影响去重 / 抓取核心逻辑）。

        监控开关、频道、等配置均从 ``self._settings`` 实时读取，
        无需刷新；此处仅重建 fetcher 以套用最新的 ``TG_PROXY``（代理地址变化后生效）。
        """
        proxy = getattr(self._settings, "TG_PROXY", "") or ""
        self._fetcher = TgFetcher(proxy=proxy)

    def poll_all(self) -> None:
        """轮询全部配置频道一次（由调度器定时触发）。

        流程：载入状态 → 逐频道爬取解析去重建订阅 → 保存状态。
        单频道异常不影响其他频道。
        """
        if not self.enabled():
            return
        self._load_state()
        for ch in self._channels():
            try:
                self.poll_channel(ch)
            except Exception:
                logger.exception("TG监控频道 %s 失败(跳过)", ch)
                # 频道级异常日志（best-effort，独立会话，不拖累主流程）。
                self._log(ch, None, "system", "error", "频道轮询异常(已跳过)")
        self._save_state()

    # ===================== 日志（T-TG 日志模块，best-effort） =====================
    def _log(
        self,
        channel: str,
        message_id: Optional[int],
        event_type: str,
        status: str,
        detail: str,
        message_type: str = "",
        link: str = "",
        content: str = "",
    ) -> None:
        """写入一条监控日志（best-effort，独立会话，失败静默忽略）。

        设计：独立开一个 session 提交，绝不参与 ``poll_channel`` 内订阅变更的事务，
        因此即使日志写入失败也不会回滚订阅创建 / 链接更新；也不阻塞主流程。

        Args:
            channel: 频道名（空串表示非频道级 / 系统级事件）。
            message_id: 关联消息 id；频道级 / 系统级事件传 ``None``。
            event_type: 事件类型（message / filter / match / create / system ...）。
            status: 处理状态（received / skipped / updated / created / completed / error ...）。
            detail: 说明文本。
            message_type: 消息类型（share / text），可选。
            link: 关联分享链接，可选。
            content: 频道推送的具体内容原文（消息正文），供「频道日记」时间轴展示。
        """
        try:
            with self._session_factory() as db:
                db.add(
                    TGMonitorLog(
                        channel=channel or "",
                        message_id=message_id,
                        event_type=event_type,
                        status=status,
                        message_type=message_type,
                        link=link or "",
                        detail=detail or "",
                        content=content or "",
                    )
                )
                db.commit()
        except Exception:
            logger.exception("写入 TG 监控日志失败(已忽略)")

    # ===================== 单频道 =====================
    def poll_channel(self, channel: str) -> None:
        """爬取并解析单个频道，对新增消息经统一核心 process_message 处理。

        说明：网页抓取仅作为『手动重扫 / 兜底』入口；实时监控由 Bot 监听
        （start()）驱动 handle_bot_message → process_message。两者共用同一套
        「名称匹配 + 状态恢复 + 链接比对 + 触发」逻辑。按需求『仅联动已有订阅』，
        不再自动新建订阅。
        """
        try:
            html = self._fetcher.fetch(f"https://t.me/s/{channel}")
        except Exception:
            logger.exception("拉取频道 %s 失败", channel)
            return
        try:
            msgs = TgChannelParser.parse_messages(html)
        except Exception:
            logger.exception("解析频道 %s 失败", channel)
            return
        st = self._state.get(channel)
        if st is None:
            st = _Row(last_message_id=0, processed=set())
            self._state[channel] = st
        # 仅处理「最后已见消息 id」之后的新消息；按 id 升序更稳妥。
        news = [m for m in msgs if m.message_id > st.last_message_id]
        news.sort(key=lambda m: m.message_id)
        for m in news:
            blob = (m.text or "") + " " + " ".join(m.links or [])
            self._log(channel, m.message_id, "message", "received", "收到新消息",
                      message_type="share" if m.links else "text", content=blob)
            self.process_message(
                channel, blob, links=list(m.links or []),
                message_id=m.message_id, dedup=True,
            )
        if news:
            self._touch_and_persist(
                channel, max([st.last_message_id] + [m.message_id for m in news])
            )
        self._log(channel, None, "system", "completed",
                  f"轮询完成，处理 {len(news)} 条新消息")





    # ===================== 逻辑 1+2：名称匹配 + 链接更新 =====================
    def _find_subscription_by_name(
        self, subs_snapshot: List[Subscription], blob: str
    ) -> Optional[Subscription]:
        """在内存订阅快照中查找「名称是 blob 子串」的第一个订阅。

        联动范围 = 全部订阅（含手动创建与 TG 自动创建），符合用户需求
        「联动订阅管理中已有订阅」。

        Args:
            subs_snapshot: 一次性载入内存的 ``Subscription`` 列表（含 id/name/share_url）。
            blob: 待匹配的文本（正文 + 链接）。

        Returns:
            命中的 ``Subscription``；无命中返回 None。
        """
        if not blob:
            return None
        for sub in subs_snapshot:
            name = getattr(sub, "name", "") or ""
            if name and name in blob:
                return sub
        return None

    # ===================== 统一处理核心（需求 1 + 2） =====================
    def process_message(self, channel, blob, links=None, message_id=None, dedup=True):
        """处理单条频道消息（Bot 实时 / 网页抓取共用核心）。

        需求 1（内容推送监控与链接匹配）+ 需求 2（失效或完结内容处理），
        统一规则为「仅联动已有订阅」：

        1) 若消息正文中的『名字』匹配到现有订阅（sub.name 是 blob 子串）：
           - 需求 2：若 sub 当前状态为 失效(pending_update)/完结(completed)
             → 自动转为进行中(active)，并清零链接失败计数；
           - 比对阿里云盘链接：
             · 一致 → 自动单独触发该订阅检查（需求 1 一致情形）；
             · 不一致 → 用频道推送的新链接更新订阅，再触发检查（需求 1/2 不一致情形）。
        2) 未匹配到任何现有订阅 → 忽略（按需求『仅联动已有订阅』，不再自动新建）。

        Args:
            channel: 频道 @username（不含 @）
            blob:    消息正文文本
            links:   已提取的阿里云盘链接列表（str）；为 None 时按 blob 自动提取
            message_id: 频道消息 id（用于重启去重）；None 时跳过消息级去重
            dedup:   是否启用消息级去重（Bot 实时消息建议 True）
        """
        if not channel:
            return
        # 消息级去重：已处理过的消息 id 直接跳过（重启不重复）
        if dedup and message_id is not None:
            with self._lock:
                st = self._state.get(channel)
                if st is not None and message_id <= st.last_message_id:
                    return
        if links is None:
            links = TgChannelParser.extract_share_links(blob or "")
        links = links or []
        # 仅保留阿里云盘分享链接，过滤掉其他无关链接（Telegram / 百度网盘 / 普通网页等），
        # 确保后续名称联动与订阅更新只基于真实阿里云盘链接，避免误更新。
        links = [u for u in links if TgChannelParser.is_aliyun_share_link(u)]
        with self._session_factory() as db:
            if not links:
                self._log(channel, message_id, "no_link", "skip", "",
                          message_type="text", content=blob or "")
                self._touch_and_persist(channel, message_id)
                db.commit()
                return
            subs_snapshot = db.query(Subscription).all()
            for url in links:
                matched = self._find_subscription_by_name(subs_snapshot, blob or "")
                if matched is None:
                    # 仅联动已有订阅：未匹配则忽略（需求：不再自动新建）
                    self._log(
                        channel, message_id, "no_match", "skip",
                        "未匹配到现有订阅，已忽略（仅联动已有订阅）",
                        message_type="link", link=url, content=blob or "",
                    )
                    continue
                # 匹配到现有订阅 → 链接一致性校验 + 状态恢复 + 触发检查
                self._link_subscription(matched, url, db, channel, message_id,
                                        content=blob or "")
            # 维护 last_message_id（去重），保证重启不重复处理同一条消息
            self._touch_and_persist(channel, message_id)
            db.commit()

    def _link_subscription(self, sub, url, db, channel, message_id, content=""):
        """匹配到现有订阅后，按需求的三步完成「链接校验 → 联动」。

        严格对应需求：
          1) 校验：推送内容中的链接(url)与对应订阅项目的链接(sub.share_url)是否一致；
          2) 若链接一致 → 保持订阅链接不变，单独触发该项目的检查流程；
          3) 若链接不一致 → 先用频道推送的新链接更新订阅的链接，再触发该项目的检查流程。
        此外：若订阅处于「失效(pending_update)/完结(completed)」状态，联动时一并恢复为
        「进行中(active)」并清零链接失效计数（需求 2：失效/完结内容处理）。

        链接安全检测（需求：仅当识别到阿里云盘分享链接才更新）：
        入口 ``url`` 通常来自 ``extract_share_links``（已只抽取阿里云盘链接），这里再做一次
        显式校验——只有通过 ``is_aliyun_share_link`` 的链接才允许更新订阅 ``share_url``，
        其余无关链接（Telegram / 百度网盘 / 普通网页等）一律过滤，避免误更新。
        """
        # 链接检测：仅当识别到阿里云盘分享链接时才执行更新，过滤其他无关链接。
        if not TgChannelParser.is_aliyun_share_link(url):
            self._log(
                channel, message_id, "link_filtered", "skip",
                "识别到非阿里云盘链接，已过滤，不更新订阅",
                message_type="link", link=url, content=content,
            )
            return
        old_status = getattr(sub, "status", "active")
        restored = False
        # 需求 2：失效(pending_update) / 完结(completed) → 转为进行中(active)
        if sub.status in ("pending_update", "completed"):
            sub.status = "active"
            sub.link_fail_count = 0
            restored = True

        # 步骤 1：校验推送链接与订阅链接是否一致。
        link_consistent = bool(sub.share_url) and (sub.share_url == url)

        if link_consistent:
            # 步骤 2：链接一致 → 不更新链接，仅记录并触发检查。
            self._log(
                channel, message_id, "link_same", "matched",
                f"sub={sub.id} name={sub.name} status={sub.status} link 一致",
                message_type="link", link=url, content=content,
            )
        else:
            # 步骤 3：链接不一致 → 先更新订阅链接，再触发检查。
            if restored:
                self._log(
                    channel, message_id, "status_restored", "matched",
                    f"sub={sub.id} name={sub.name} old_status={old_status} → active",
                    message_type="link", link=url, content=content,
                )
            sub.share_url = url
            self._log(
                channel, message_id, "link_updated", "matched",
                f"sub={sub.id} name={sub.name} 链接已更新为 {url}",
                message_type="link", link=url, content=content,
            )

        try:
            db.commit()
        except Exception:
            logger.exception("更新订阅失败: sub=%s", getattr(sub, "id", None))
            return
        # 状态恢复时重新注册 per-sub 转存 job
        if restored:
            try:
                self._scheduler.register(sub)
            except Exception:
                logger.exception("恢复订阅注册失败: sub=%s", sub.id)
        # 步骤 2 / 3 收尾：单独触发该项目的检查流程（链接一致或不一致均触发）。
        self._scheduler.trigger_once(sub.id)

    # ===================== 状态落库辅助 =====================
    def _touch_and_persist(self, channel, message_id):
        """更新内存状态并落库（去重用 last_message_id）。"""
        with self._lock:
            st = self._state.get(channel)
            if st is None:
                st = _Row(last_message_id=0, processed=set())
                self._state[channel] = st
            if message_id is not None and message_id > st.last_message_id:
                st.last_message_id = message_id
            self._save_state()

    # ===================== TG 机器人实时监听 =====================
    def handle_bot_message(self, channel, blob, links, message_id):
        """Bot 实时收到频道消息时的入口（由 core.tg_bot 回调）。"""
        try:
            self.process_message(channel, blob, links, message_id=message_id, dedup=True)
        except Exception:
            logger.exception(
                "TG_MONITOR: 处理 Bot 消息异常 (channel=%s mid=%s)", channel, message_id
            )

    def start(self):
        """启动 TG 机器人实时监听（独立 daemon 线程）。

        前提：配置了 TG_BOT_TOKEN 且监控频道非空。Bot 需已加入目标频道
        （作为普通成员即可，无需管理员权限）。未安装 pytelegrambotapi 时给出提示。
        Bot Token 优先读 env（TG_BOT_TOKEN），为空时回退到 UI 设置页持久化的 KV。
        """
        token = getattr(self._settings, "TG_BOT_TOKEN", "") or ""
        if not token:
            # 回退：UI 设置页填入并持久化到 KV 的 bot token
            try:
                from models import Setting
                with self._session_factory() as db:
                    row = db.query(Setting).filter_by(key="tg_bot_token").first()
                    if row is not None:
                        token = (row.value or "").strip()
            except Exception:
                logger.exception("TG_MONITOR: 读取 tg_bot_token KV 失败")
        if not token:
            logger.warning("TG_MONITOR: 未配置 TG_BOT_TOKEN，Bot 监听不启动（仅保留手动扫描）")
            return
        channels = self._channels()
        if not channels:
            logger.warning("TG_MONITOR: 未配置监控频道，Bot 监听不启动")
            return
        try:
            from core.tg_bot import BotListener
        except Exception:
            logger.warning(
                "TG_MONITOR: 无法导入 Bot 监听模块（请先 `pip install pytelegrambotapi`）"
            )
            return
        try:
            with self._lock:
                if self._bot_thread is not None and self._bot_thread.is_alive():
                    logger.info("TG_MONITOR: Bot 监听线程已在运行，跳过重复启动")
                    return
                self._load_state()
                self._bot_listener = BotListener(token, channels, self.handle_bot_message)
                self._bot_thread = threading.Thread(
                    target=self._bot_listener.run,
                    name="tg-bot-monitor", daemon=True,
                )
                self._bot_thread.start()
            logger.info("TG_MONITOR: Bot 实时监听线程已启动，频道=%s", channels)
        except Exception:
            logger.exception("TG_MONITOR: Bot 监听线程启动失败")

    def stop_listener(self) -> dict:
        """停止 Bot 实时监听线程与轮询定时任务（幂等，释放系统资源）。

        - Bot 监听：调用 ``BotListener.stop()`` 终止长轮询，join 等待线程退出；
          未启动 / 已停止时安全跳过。
        - 定时任务：从 APScheduler 移除 ``tg_monitor_poll`` job；不存在时安全跳过。

        Returns:
            ``{"listener_stopped": bool, "poll_job_removed": bool}``
        """
        out = {"listener_stopped": False, "poll_job_removed": False}
        # ---- 停 Bot 监听线程 ----
        with self._lock:
            listener, thread = self._bot_listener, self._bot_thread
            self._bot_listener = None
            self._bot_thread = None
        if listener is not None:
            try:
                listener.stop()
                out["listener_stopped"] = True
                logger.info("TG_MONITOR: 已请求停止 Bot 监听")
            except Exception:
                logger.exception("TG_MONITOR: 停止 Bot 监听失败(已忽略)")
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=5)
            except Exception:
                logger.exception("TG_MONITOR: 等待 Bot 监听线程退出失败(已忽略)")
        # ---- 移除轮询定时任务 ----
        sched = getattr(self._scheduler, "scheduler", None)
        if sched is not None and hasattr(sched, "get_job"):
            try:
                if sched.get_job("tg_monitor_poll") is not None:
                    sched.remove_job("tg_monitor_poll")
                    out["poll_job_removed"] = True
                    logger.info("TG_MONITOR: 已移除 tg_monitor_poll 定时任务")
                else:
                    logger.info("TG_MONITOR: tg_monitor_poll 定时任务不存在，无需移除")
            except Exception:
                logger.exception("TG_MONITOR: 移除 tg_monitor_poll 定时任务失败(已忽略)")
        return out

    # ===================== 状态持久化 =====================
    def _load_state(self) -> None:
        """从 ``tg_monitor_state`` 表载入全部频道状态到内存。"""
        with self._lock:
            self._state = {}
            self._global_processed = set()
            try:
                with self._session_factory() as db:
                    rows = db.query(TGMonitorState).all()
                    for row in rows:
                        processed: Set[str] = set()
                        try:
                            data = json.loads(row.processed_links or "[]")
                            if isinstance(data, list):
                                processed = set(str(x) for x in data)
                        except Exception:
                            processed = set()
                        self._state[row.channel] = _Row(
                            last_message_id=row.last_message_id or 0,
                            processed=processed,
                        )
                        self._global_processed |= processed
            except Exception:
                logger.exception("载入 TG 监控状态失败（从空状态启动）")

    def _save_state(self) -> None:
        """把内存中的频道状态 upsert 回 ``tg_monitor_state`` 表。"""
        with self._lock:
            try:
                with self._session_factory() as db:
                    for channel, st in self._state.items():
                        row = db.query(TGMonitorState).filter_by(channel=channel).first()
                        links = json.dumps(sorted(set(st.processed)), ensure_ascii=False)
                        if row is None:
                            row = TGMonitorState(
                                channel=channel,
                                last_message_id=st.last_message_id,
                                processed_links=links,
                            )
                            db.add(row)
                        else:
                            row.last_message_id = st.last_message_id
                            row.processed_links = links
                    db.commit()
            except Exception:
                logger.exception("保存 TG 监控状态失败")

    # ===================== 内部工具 =====================
    def _channels(self) -> list[str]:
        """解析配置中的频道列表，归一化为末段用户名（过滤无效项）。"""
        raw = (getattr(self._settings, "TG_MONITOR_CHANNELS", "") or "").split(",")
        out: list[str] = []
        for c in raw:
            c = c.strip()
            if not c:
                continue
            n = TgChannelParser._normalize_channel(c)
            if n:
                out.append(n)
        return out
