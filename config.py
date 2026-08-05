"""应用配置（pydantic-settings）。

从环境变量 / .env 读取，类型安全，代码内零明文凭证。
详见 ARCHITECTURE.md §7 共享约定与 §8 待明确事项（已由主理人拍板）。
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。所有字段均有安全默认值，便于离线/测试启动。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 云盘凭证（仅环境变量 / 反代注入） ----------
    ALIYUNDRIVE_REFRESH_TOKEN: str = ""

    # ---------- 数据库（SQLite 单库，单账号起步） ----------
    DATABASE_URL: str = "sqlite:///data/app.db"

    # ---------- Web 绑定（不暴露公网，由反代层负责鉴权） ----------
    WEB_HOST: str = "127.0.0.1"
    WEB_PORT: int = 8000

    # ---------- Aria2 远程下载 ----------
    ARIA2_RPC_URL: str = ""
    ARIA2_RPC_SECRET: str = ""
    ARIA2_RPC_ENABLE: bool = False

    # ---------- Telegram 公开频道监控自动转存（T-TG） ----------
    # 监控机制：TG 机器人实时监听频道推送（Bot 需加入频道，作为普通成员即可，无需管理员权限）。
    # TG_BOT_TOKEN 现在同时用于「监控监听」与「命中通知」；留空即不启用 Telegram 通知与监听。
    TG_MONITOR_ENABLED: bool = False
    TG_MONITOR_CHANNELS: str = ""          # 逗号分隔，支持 @user / https://t.me/user / 纯 user / 数字 ID
    TG_BOT_TOKEN: str = ""
    TG_NOTIFY_CHAT_ID: str = ""
    # Telegram 通知（命中通知 Bot）：与频道监控同模块，可独立开关（默认关，需显式开启）。
    TG_NOTIFY_ENABLED: bool = False
    TG_POLL_INTERVAL: int = 300            # 轮询周期（秒），调度器内部 clamp >= 60

    TG_PROXY: str = ""                     # 爬取用代理（http/https/socks5），留空直连

    # ---------- 限时分享临期阈值（天） ----------
    # 主理人决策 #12：距 share_expire_at 不足该天数视为临期，触发提醒。
    SHARE_EXPIRE_THRESHOLD_DAYS: int = 7

    # ---------- 日志与展示时区 ----------
    LOG_LEVEL: str = "INFO"
    TZ: str = "Asia/Shanghai"
