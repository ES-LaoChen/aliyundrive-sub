"""阿里云盘开放接口地址常量。

集中定义，便于后续用真实 refresh_token 实测校准（接口路径可能随官方调整）。
"""
from __future__ import annotations

AUTH_BASE = "https://auth.aliyundrive.com"
API_BASE = "https://api.aliyundrive.com"
USER_BASE = "https://user.aliyundrive.com"
# 新版域名（阿里云盘已迁移到 alipan.com）
ALIPAN_BASE = "https://api.alipan.com"

ENDPOINTS: dict[str, str] = {
    "token": f"{AUTH_BASE}/v2/account/token",
    "user_info": f"{USER_BASE}/v2/user/get",  # 返回 default/resource/backup drive_id
    "drive_info": f"{USER_BASE}/v2/user/get_drive_info",  # 已 404，保留仅作历史记录
    # 分享端点：旧路径 `/v2/share/link/get_share_token` + 旧域名 aliyundrive.com 已 404
    # 改用新版：alipan.com + 路径 `/v2/share_link/get_share_token`（实测 200）
    "share_info": f"{ALIPAN_BASE}/v2/share_link/get_share_info",
    "share_token": f"{ALIPAN_BASE}/v2/share_link/get_share_token",
    "file_list": f"{API_BASE}/adrive/v3/file/list",
    "create_with_proof": f"{API_BASE}/adrive/v2/file/create_with_proof",  # 旧端点，已弃用
    "file_copy": f"{ALIPAN_BASE}/v2/file/copy",  # 新版：转存分享文件（不要 proof）
    "create_folder": f"{API_BASE}/adrive/v2/file/create_folder",
    "rename": f"{API_BASE}/v3/file/update",
    "delete": f"{API_BASE}/v2/recyclebin/trash",
    "download_url": f"{API_BASE}/v2/file/get_download_url",
}
