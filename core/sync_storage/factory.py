"""存储驱动工厂（移植自 TaoSync service/storage/factory.py，范围收敛为 local）。

本项目的同步后端仅保留两类：
- ``local``：进程可见的本地绝对目录（Docker 部署需把宿主机目录挂进容器）。
- ``alist``：外部 OpenList/AList 实例，由 ``core/sync_storage.engine`` 中的
  ``TaoSyncClient`` 以 AList 兼容外观统一封装，不在 factory 直接建驱动。

驱动构造签名保持与 TaoSync 一致（``createDriver(driverType, config, ...)``），
便于 ``engine.py`` 在挂载时按需实例化。
"""
from __future__ import annotations

from core.sync_storage.drivers.local import LocalDriver


# 仅 local 走真实驱动；alist 由 engine 层以 facade 实现，不在此登记驱动类。
DRIVER_TYPES = {
    "local": LocalDriver,
}

# 各驱动类型的敏感字段（返回给前端时置空，仅保留「是否已配置」状态）。
SECRET_FIELDS = {
    "local": set(),
}


def createDriver(
    driverType,
    config,
    save_config=None,
    load_config=None,
    refresh_lock=None,
    auth_version=None,
):
    """构造一个存储驱动实例。

    移植层签名与 TaoSync 保持一致，尽管 local 驱动当前不使用 save/load 回调。
    """
    driver_class = DRIVER_TYPES.get(driverType)
    if driver_class is None:
        raise ValueError("unsupported storage driver: {}".format(driverType))
    return driver_class(config)


def getDriverTypes():
    """返回受支持的驱动类型列表（供 UI 存储目录表单渲染选项）。"""
    return list(DRIVER_TYPES.keys())
