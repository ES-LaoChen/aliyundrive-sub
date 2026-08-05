"""命名规则：剧集智能重命名（纯函数，可单测）。

全项目唯一重命名入口 ``NamingRule.apply(filename, template, regex)``。
- 从文件名中提取集数（内置正则集 + 可选自定义正则 + 兜底数字提取）。
- 将集数填入 ``template`` 的 ``{}`` 占位符。
- 无匹配时返回原名（不破坏已有文件）。
- 自动保留原文件扩展名。

内置正则集采用 PRD 给出的原始正则（主理人决策 #11）。
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# 订阅可配置的重命名模式集合。
# - none:          不改名（等价于旧行为：文件名保持原名）
# - template:      命名模板（沿用现有 NamingRule.apply，命中集数才改名）
# - prefix_suffix: 前后缀拼接（如 [A]原名[B]）
# - timestamp:     加转存时间戳（原名_20260715_203000.mp4）
RENAME_MODES = {"none", "template", "prefix_suffix", "timestamp"}


class NamingRule:
    """剧集智能重命名规则。"""

    # PRD 给出的原始正则集（数字集数识别）。
    BUILTIN_REGEXES: list[str] = [
        r"E\d{1,4}",
        r"EP\d{1,4}",
        r"第\d{1,4}话",
        r"第\d{1,4}集",
        r"第\d{1,4}期",
        r"_\d{1,4}_",
        r"\d{1,4}\s",
        r"\[\d{1,4}\]",
        r"\d{1,4}-4K",
        r"【\d{1,4}】",
    ]

    # 兜底：独立出现的 1~4 位数字（如 "01.mp4" -> "01"）。
    FALLBACK_REGEX = r"(?<!\d)(\d{1,4})(?!\d)"

    # 视为分辨率而非集数的数字（兜底时跳过）。
    _RESOLUTIONS = {"240", "360", "480", "576", "720", "900", "1080", "1440", "2160", "4320"}

    @staticmethod
    def apply(filename: str, template: str, regex: str = "") -> str:
        """根据模板对文件名应用重命名。

        Args:
            filename: 原始文件名（如 ``01.mp4``）。
            template: 命名模板（如 ``不会恋爱的我们.E{}``）；为空则返回原名。
            regex: 可选自定义正则，优先于内置正则参与匹配。

        Returns:
            重命名后的文件名；无匹配时返回原名。
        """
        if not template:
            return filename
        episode = NamingRule._extract_episode(filename, regex)
        if episode is None:
            return filename
        return NamingRule._format(filename, template, episode)

    @staticmethod
    def _extract_episode(filename: str, regex: str = "") -> str | None:
        """从文件名提取集数（数字串）。"""
        # 仅对「去扩展名后的主名」匹配，避免 mp4 / 1080p 等误判。
        stem, _ = os.path.splitext(os.path.basename(filename))
        patterns: list[str] = ([regex] if regex else []) + NamingRule.BUILTIN_REGEXES
        for pat in patterns:
            match = re.search(pat, stem)
            if match:
                digits = re.search(r"\d{1,4}", match.group(0))
                if digits:
                    return digits.group(0)
        # 兜底：独立数字（跳过分辨率等伪集数）。
        for fm in re.finditer(NamingRule.FALLBACK_REGEX, stem):
            num = fm.group(1)
            after = stem[fm.end(): fm.end() + 1]
            if after.lower() in ("p", "i", "x", "k"):
                continue
            if num in NamingRule._RESOLUTIONS:
                continue
            return num
        return None

    @staticmethod
    def _format(filename: str, template: str, episode: str) -> str:
        """将集数填入模板，并尽量保留原扩展名。"""
        formatted = template.replace("{}", episode) if "{}" in template else f"{template}{episode}"
        base, ext = os.path.splitext(filename)
        if ext and not formatted.endswith(ext):
            formatted += ext
        return formatted

    @staticmethod
    def sanitize_filename(value: str) -> str:
        """清洗文件名中的非法字符（对齐 app_daSSe 的 ``_safe_filename``）。

        阿里云盘文件名不允许 ``\\ / : * ? " < > |``；这里替换为空格、压缩空白、
        去除首尾空格与点，超长截断到 180（与 app_daSSe 一致，留足余量）。
        目的是保证 ``compute_new_name`` 产出的目标名能被 aliyun 接受，避免
        copy 因非法字符失败而让改名静默失效。空结果回退为「未命名」。
        """
        if not value:
            return value
        text = re.sub(r'[\\/:*?"<>|]', " ", value)
        text = re.sub(r"\s+", " ", text).strip(" .")
        if not text:
            return "未命名"
        # 阿里云盘文件名上限约 255，取 180 留余量（与 app_daSSe 一致）。
        return text[:180]

    @staticmethod
    def compute_new_name(
        filename: str,
        mode: str,
        *,
        template: str = "",
        regex: str = "",
        prefix: str = "",
        suffix: str = "",
        now=None,
    ) -> str:
        """根据订阅配置计算转存后的目标文件名（纯函数，可单测）。

        改名在 ``/v2/file/copy`` 时随 ``new_name`` 字段一次性完成，避免二次改名
        端点异常被静默吞掉导致文件名未改。未知 / 空 mode 一律返回原名，
        向后兼容老数据（``rename_mode`` 为空字符串时按 ``none`` 处理）。

        所有非原名的产出都会经 ``sanitize_filename`` 清洗（对齐 app_daSSe），
        确保目标名不含非法字符、可被 aliyun 接受。

        Args:
            filename: 原始分享文件名（如 ``01.mp4``）。
            mode: 重命名模式，取值见 ``RENAME_MODES``。
            template: template 模式的命名模板（如 ``剧名.E{}``）。
            regex: template 模式的自定义正则（优先于内置正则）。
            prefix: prefix_suffix 模式的前缀。
            suffix: prefix_suffix 模式的后缀。
            now: 可注入的 ``datetime``（测试用）；未注入则用上海时区当前时间。

        Returns:
            目标文件名；无需改名或模式不支持时返回原名。
        """
        # 未知 / 空 mode 一律视为 none（保持原名，向后兼容老数据）。
        if mode not in RENAME_MODES:
            return filename

        if mode == "template":
            # 沿用现有命名模板逻辑；无匹配时返回原名（原名为分享侧合法名，无需清洗）。
            new = NamingRule.apply(filename, template, regex)
            return NamingRule.sanitize_filename(new) if new != filename else filename

        if mode == "prefix_suffix":
            if not prefix and not suffix:
                return filename
            return NamingRule.sanitize_filename(f"{prefix}{filename}{suffix}")

        if mode == "timestamp":
            from datetime import datetime
            from zoneinfo import ZoneInfo

            # ZoneInfo 不可用时回退到本地/UTC 时间，保证改名仍可进行。
            try:
                dt = now or datetime.now(ZoneInfo("Asia/Shanghai"))
            except Exception:
                dt = now or datetime.now()
            ts = dt.strftime("%Y%m%d_%H%M%S")
            base, ext = os.path.splitext(filename)
            return NamingRule.sanitize_filename(f"{base}_{ts}{ext}")

        # 其余情况（含 none）保持原名。
        return filename
