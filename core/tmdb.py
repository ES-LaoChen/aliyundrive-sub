"""TMDB v3 API 客户端（电影 / 剧集识别）。

用于「TMDB 识别」功能：给定 TMDB 数字 ID，自动获取标题、海报、简介等信息，
回填到订阅表单中。设计要点：

- 鉴权方式：query 参数 ``api_key``（v3 key）+ ``language=zh-CN`` 获取本地化标题。
- Base URL：``https://api.themoviedb.org/3``。
- 海报基址：``https://image.tmdb.org/t/p/w500{path}``。
- 单次 GET + timeout 即可，无完整退避逻辑（保持轻量）。
- ``test_connection`` 永不抛异常，始终返回 ``(ok, message)``，供前端直接展示。
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


class TMDBError(Exception):
    """TMDB 调用相关的错误，携带清晰可读的中文错误信息。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:  # noqa: D401
        return self.message


class TMDBClient:
    """TMDB v3 API 客户端。

    职责：电影 / 剧集详情获取 + 连接测试。密钥由调用方在路由中从 Setting KV 表
    读取后注入，客户端本身不持有进程级持久状态（安全：密钥不留在 ``Services``）。
    """

    def __init__(
        self,
        api_key: str,
        language: str = "zh-CN",
        timeout: float = 10.0,
        session: "requests.Session | None" = None,
    ) -> None:
        """构造客户端。

        Args:
            api_key: TMDB v3 API Key（必填）。
            language: 本地化语言，默认 ``zh-CN``。
            timeout: 单次请求超时（秒），默认 10 秒。
            session: 可注入自定义 ``requests.Session``；缺省时新建一个。
        """
        self.api_key = api_key
        self.language = language
        self.timeout = timeout
        self._session = session or requests.Session()
        self.base = TMDB_BASE

    def _get(self, path: str, params: dict) -> "requests.Response":
        """发起带鉴权参数的 GET 请求，返回响应对象（不抛业务异常）。

        Args:
            path: 形如 ``/movie/{id}`` 的相对路径（不含 base）。
            params: 附加 query 参数；``api_key`` 与 ``language`` 自动注入。

        Raises:
            requests.exceptions.RequestException: 网络层异常（交由调用方捕获转译）。
        """
        full_params = {**params, "api_key": self.api_key, "language": self.language}
        return self._session.get(
            self.base + path, params=full_params, timeout=self.timeout
        )

    def get_details(self, tmdb_id: int) -> dict:
        """获取 TMDB 媒体详情，归一化为统一字段字典。

        先尝试 ``/movie/{id}``，若返回 404 再回退 ``/tv/{id}``（movie 404 → tv 兜底）。

        Returns:
            归一化字典，字段包含：
            ``tmdb_id / media_type / title / original_title / overview /
            poster_path / poster_url / release_date``。

        Raises:
            TMDBError: 鉴权失败（401）/ 网络异常 / movie 与 tv 都 404 / 其它非 200。
        """
        # 1) 先试电影
        try:
            movie_resp = self._get(f"/movie/{tmdb_id}", {})
        except requests.exceptions.RequestException as exc:
            raise TMDBError(f"网络请求失败：{exc}") from exc

        if movie_resp.status_code == 200:
            data = movie_resp.json()
            return self._normalize(data, tmdb_id, "movie")

        if movie_resp.status_code == 401:
            raise TMDBError("API Key 无效或未授权（HTTP 401）")

        if movie_resp.status_code == 404:
            # 2) 回退剧集
            try:
                tv_resp = self._get(f"/tv/{tmdb_id}", {})
            except requests.exceptions.RequestException as exc:
                raise TMDBError(f"网络请求失败：{exc}") from exc

            if tv_resp.status_code == 200:
                data = tv_resp.json()
                return self._normalize(data, tmdb_id, "tv")

            if tv_resp.status_code == 401:
                raise TMDBError("API Key 无效或未授权（HTTP 401）")

            if tv_resp.status_code == 404:
                raise TMDBError(f"未找到 TMDB ID {tmdb_id} 对应的媒体")

            raise TMDBError(f"TMDB 返回错误：HTTP {tv_resp.status_code}")

        # 电影非 200 且非 401 且非 404（如 403 / 429 / 500 等）
        raise TMDBError(f"TMDB 返回错误：HTTP {movie_resp.status_code}")

    @staticmethod
    def _normalize(data: dict, tmdb_id: int, media_type: str) -> dict:
        """把电影 / 剧集原始响应归一化为统一字段字典。"""
        poster_path = data.get("poster_path") or ""
        poster_url = (TMDB_IMAGE_BASE + poster_path) if poster_path else ""
        return {
            "tmdb_id": int(tmdb_id),
            "media_type": media_type,
            "title": data.get("title") or data.get("name") or "",
            "original_title": data.get("original_title")
            or data.get("original_name")
            or "",
            "overview": data.get("overview") or "",
            "poster_path": poster_path,
            "poster_url": poster_url,
            "release_date": data.get("release_date") or data.get("first_air_date") or "",
        }

    def test_connection(self) -> "tuple[bool, str]":
        """探测 API Key 有效性与网络连通性，永不抛异常。

        探测对象为公开稳定的电影 ``/movie/550``，结果用于「设置页 - 测试连接」按钮。

        Returns:
            ``(ok, message)``：``ok`` 为 True 时 message 为成功提示；
            否则 message 为清晰的错误说明。HTTP 状态码始终为 200，由前端依据 ``ok`` 展示。
        """
        try:
            resp = self._get("/movie/550", {})
        except requests.exceptions.RequestException as exc:
            return False, f"网络不可达：{exc}"

        if resp.status_code == 200:
            return True, "连接成功，API Key 有效"
        if resp.status_code == 401:
            return False, "API Key 无效或未授权（HTTP 401）"
        return False, f"TMDB 返回错误：HTTP {resp.status_code}"

    def search(self, query: str, media_type: str = "multi") -> "list[dict]":
        """按名称搜索 TMDB，返回归一化结果列表（含每个结果的数字 ID）。

        统一拼成 ``/search/{type}``（``multi`` → ``/search/multi``，
        ``movie`` / ``tv`` / ``person`` 同理）。``multi`` 返回结果自带
        ``media_type`` 字段；``movie`` / ``tv`` / ``person`` 端点结果不含该字段，
        此时以入参 ``media_type`` 兜底。

        Args:
            query: 搜索关键词（影视作品名称或人物名）。
            media_type: 搜索类型，支持 ``multi`` / ``movie`` / ``tv`` / ``person``。

        Returns:
            归一化结果列表，每项字段为：
            ``{id, name, media_type, poster_url, overview}``。
            - ``id``：结果的数字 ID（int）。
            - ``name``：标题 / 名称（取 ``title`` / ``name`` /
              ``original_title`` / ``original_name``）。
            - ``poster_url``：海报地址（``movie`` / ``tv`` 取 ``poster_path``，
              ``person`` 取 ``profile_path``，基址同 ``get_details``）。
            - ``overview``：简介。
            results 为空时返回空列表 ``[]``（不抛错，由路由层决定报「未找到」）。

        Raises:
            TMDBError: 鉴权失败（HTTP 401）/ 网络异常 / 其它非 200 错误。
        """
        path = f"/search/{media_type}"
        try:
            resp = self._get(path, {"query": query})
        except requests.exceptions.RequestException as exc:
            raise TMDBError(f"网络请求失败：{exc}") from exc

        if resp.status_code == 401:
            raise TMDBError("API Key 无效或未授权（HTTP 401）")
        if resp.status_code != 200:
            raise TMDBError(f"TMDB 返回错误：HTTP {resp.status_code}")

        data = resp.json()
        raw_results = data.get("results") or []
        if not raw_results:
            return []

        normalized: "list[dict]" = []
        for item in raw_results:
            item_media = item.get("media_type") or media_type
            name = (
                item.get("title")
                or item.get("name")
                or item.get("original_title")
                or item.get("original_name")
                or ""
            )
            # movie/tv 用 poster_path，person 用 profile_path（兜底兼容）。
            poster_path = item.get("poster_path") or item.get("profile_path") or ""
            poster_url = (TMDB_IMAGE_BASE + poster_path) if poster_path else ""
            try:
                item_id = int(item.get("id", 0))
            except (TypeError, ValueError):
                item_id = 0
            normalized.append(
                {
                    "id": item_id,
                    "name": name,
                    "media_type": item_media,
                    "poster_url": poster_url,
                    "overview": item.get("overview") or "",
                }
            )
        return normalized
