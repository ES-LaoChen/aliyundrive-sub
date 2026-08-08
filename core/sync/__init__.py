"""多后端存储 + 同步引擎核心包。

整合移植自 TaoSync 的 service/storage 与 service/engine，统一用 SQLAlchemy
会话持久化（替代原 sqlBase 手写 SQL）。对外只暴露：

- ``StorageEngine``：内置 taosync 引擎的存储目录（挂载）管理与 AList 风格 facade。
- ``get_storage_client``：按引擎 id 取得缓存的 ``TaoSyncClient``（驱动懒加载）。
"""
