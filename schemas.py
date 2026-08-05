"""Web 层 pydantic 模型（表单校验）。

订阅增改与设置表单的数据结构，蓝图路由用它做基本校验与默认值。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SubscriptionCreate(BaseModel):
    """新增订阅表单。"""

    name: str = ""
    share_url: str = ""
    target_folder_id: str = ""
    target_folder_path: str = ""
    target_drive_type: str = ""
    interval: str = "3600"
    naming_template: str = ""
    naming_regex: str = ""
    # 重命名规则：none / template / prefix_suffix / timestamp
    rename_mode: str = "none"
    rename_prefix: str = ""
    rename_suffix: str = ""
    status: str = "active"
    remark: str = ""
    # TMDB 识别（新增订阅回填）：数字 ID 与海报地址（由前端获取后写入）
    tmdb_id: str = ""
    poster_url: str = ""


class SubscriptionUpdate(SubscriptionCreate):
    """编辑订阅表单（字段相同）。"""


class SettingsForm(BaseModel):
    """设置页表单（TMDB / 订阅状态巡检 / Telegram 通知）。"""

    # TMDB v3 API Key（存于 Setting KV 表，新增订阅时自动读取调用）
    tmdb_api_key: str = ""
    # 订阅状态巡检（SubStatus）配置：全部以字符串承载，保存时按 KV 落库，
    # 运行时由 core.substatus_poller.load_poll_config 解析为 int / bool。
    link_fail_threshold: str = "3"               # 链接访问失败次数阈值
    sub_check_interval: str = "3600"             # 状态巡检轮询周期（秒）
    substatus_concurrency_enabled: str = "false"  # 是否开启并发检查
    substatus_concurrency_workers: str = "3"     # 并发数量（仅 enabled 时生效）
    substatus_poll_wait_seconds: str = "2"       # 轮询中途等待时间（秒，节流防爬虫）
    # Telegram 频道监控通知（可选功能）
    tg_notify_enabled: str = "false"   # 启用开关（checkbox 未勾选时表单不含该字段，回退 "false"）
    tg_notify_chat_id: str = ""        # 频道 / Chat ID
    tg_bot_token: str = ""             # Bot Token（密码框，不回显）
