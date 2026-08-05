# 宝塔面板 Docker 部署指南 · 阿里云盘订阅转存服务

本文档说明如何把本项目打包为 Docker 镜像，并在**宝塔面板**中直接导入运行，无需修改代码。

---

## 一、产物清单

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 镜像构建文件（基础环境 / 依赖 / 启动命令 / 端口 / 持久化卷） |
| `.dockerignore` | 构建时排除本地环境、缓存、测试、开发库，保证镜像干净 |
| `docker-compose.yml` | 一键编排（端口映射 + 目录挂载 + 环境变量 + 健康检查） |
| `DEPLOY-DOCKER.md` | 本说明 |

---

## 二、构建镜像

构建上下文必须是 **本目录（含 `app.py` 的 `aliyundrive-sub/`）**。

### 方式 A：命令行本地构建（再导入宝塔）

```bash
cd aliyundrive-sub
docker build -t aliyundrive-sub:latest .
# 导出为 tar 包，便于上传到宝塔
docker save aliyundrive-sub:latest -o aliyundrive-sub.tar
```

然后在宝塔「Docker → 镜像」中「导入镜像」选择 `aliyundrive-sub.tar`。

### 方式 B：宝塔「Docker → 编排」直接上传目录

把整个 `aliyundrive-sub/` 目录打包上传到服务器，在宝塔「Docker → 编排」中
指定 `docker-compose.yml` 一键部署。

---

## 三、宝塔面板运行配置（关键项）

在宝塔「Docker → 容器」创建容器时，按下表填写（与 `docker-compose.yml` 对应）：

| 配置项 | 值 | 说明 |
| --- | --- | --- |
| 镜像 | `aliyundrive-sub:latest` | 已导入的镜像 |
| 端口映射 | 容器 `8000` → 宿主机 `8000` | 可改成任意宿主机端口 |
| 目录映射 | 宿主机目录 ↔ 容器 `/app/data` | **必须挂载**，否则数据库随容器销毁丢失 |
| 环境变量 | 见下表 | 至少填 `ALIYUNDRIVE_REFRESH_TOKEN` |

### 必填 / 常用环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ALIYUNDRIVE_REFRESH_TOKEN` | 空 | **必填**，阿里云盘 refresh_token |
| `WEB_HOST` / `WEB_PORT` | `0.0.0.0` / `8000` | 容器内已默认，一般无需改 |
| `DATABASE_URL` | `sqlite:////app/data/app.db` | 已指向持久化卷，无需改 |
| `TG_BOT_TOKEN` / `TG_NOTIFY_CHAT_ID` | 空 | TG 监控 / 通知，按需填写 |
| `TG_MONITOR_ENABLED` | `false` | 是否启用 TG 频道监控 |
| `ARIA2_RPC_ENABLE` / `ARIA2_RPC_URL` / `ARIA2_RPC_SECRET` | `false` / 空 / 空 | 远程 Aria2 下载 |

> 镜像已内置 `/healthz` 健康检查；容器状态可在宝塔「容器」列表看到健康标识。

---

## 四、访问与反向代理（重要）

应用本身**无应用内鉴权**（设计决策）。宝塔中通过「网站 → 反向代理」把
`http://127.0.0.1:8000` 暴露到域名，并在反代或网站中开启**访问鉴权（Basic Auth）**，
切勿将 8000 端口直接公网开放。

反向代理目标：`http://127.0.0.1:8000`（若宿主机端口非 8000，按实际填写）。

---

## 五、数据与升级

- **数据持久化**：`/app/data/app.db`（SQLite）。升级镜像只需停止容器、拉新镜像、
  用同一目录映射重新创建容器，**数据库不丢**。
- **日志查看**：应用日志输出到容器标准输出，宝塔「容器 → 日志」或 `docker logs` 可看。
- **重启**：`docker restart aliyundrive-sub`（或宝塔对应操作）。

---

## 六、常见问题

| 现象 | 处理 |
| --- | --- |
| 容器启动后立即退出 | 看容器日志；多为依赖缺失（正常已装好）或 `ZoneInfo` 报错（镜像已装 tzdata，不会触发） |
| 页面打不开 | 确认端口映射正确，且未把 8000 直接公网暴露被防火墙拦截 |
| 数据丢失 | 确认「目录映射」已挂到 `/app/data` |
| 转存无反应 | 检查 `ALIYUNDRIVE_REFRESH_TOKEN` 是否配置正确、目标目录 `target_folder_id` 是否有效 |
| 健康检查一直 unhealthy | 等待 30s 启动期；仍异常则查日志看 DB 是否可写 |
