# 宝塔容器部署指南

本项目为 Flask 应用，内置 APScheduler 调度器与 Telegram 监控线程（均在 `app.py` 的 `main()` 内启动），因此容器必须以 `python app.py` 运行，**不可用 gunicorn 工厂模式**。

本文面向有宝塔面板与服务器命令行使用经验的用户。

---

## 一、前置条件

- 已安装宝塔面板（建议 9.x+）并启用「Docker 管理器 / 容器」插件。
- 服务器已安装 Docker 与 Git（多数 Linux 发行版可 `apt install docker.io git -y`，并在宝塔「Docker」插件中确认守护进程运行）。
- 已获取阿里云盘 `refresh_token`（用于真实转存，应用设置页或环境变量注入均可）。

---

## 二、方式一：容器编排一键部署（推荐他人使用）

仓库已提供 `docker-compose.yml`，通过编排从仓库代码本地构建并启动，**无需手动 build，也无需公共镜像仓库**（镜像为私有，直接 pull 会 `pull access denied`）。

### 2.1 命令行（服务器 / 宝塔终端）

```bash
git clone https://github.com/ES-LaoChen/aliyundrive-sub.git
cd aliyundrive-sub
docker compose up -d          # 自动 build + 启动
```

- 启动后容器名 `aliyundrive-sub`，端口 `8000`，数据挂载到 `./data`。
- 查看状态：`docker compose ps`
- 查看日志：`docker compose logs -f`
- 更新代码后重建：`git pull && docker compose up -d --build`
- 停止：`docker compose down`（保留 `./data` 数据）

### 2.2 宝塔面板「编排」导入

1. 宝塔 **Docker → 编排**，点击「添加编排」。
2. 编排类型选「**已有目录**」或「**创建编排**」：
   - 若服务器已 `git clone` 过仓库：直接指定 `aliyundrive-sub/docker-compose.yml` 路径导入。
   - 否则在「创建编排」中粘贴本仓库 `docker-compose.yml` 内容，保存。
3. 点击「**构建 / 启动**」。编排会执行 `build`（从仓库代码）后再运行容器。
4. 启动成功后在「容器」列表可见 `aliyundrive-sub` 运行中。

> 关键：compose 使用 `build:` 段从本地代码构建，**不要**改成 `image:` 公共地址，否则宝塔编排会尝试 pull 私有镜像而失败。若你已手动 `docker build` 过镜像，才可按 compose 文件内注释改用 `image:`。

### 2.3 填写环境变量

编辑 `docker-compose.yml` 的 `environment:` 段，至少填入：

| 变量 | 说明 |
| --- | --- |
| `ALIYUNDRIVE_REFRESH_TOKEN` | **必填**，阿里云盘 token；留空可后在 Web 设置页填 |
| `TG_BOT_TOKEN` / `TG_MONITOR_CHANNELS` / `TG_MONITOR_ENABLED` | 按需启用 TG 监控 |
| `TG_NOTIFY_ENABLED` / `TG_NOTIFY_CHAT_ID` | 按需启用 TG 通知 |
| `ARIA2_RPC_*` | 按需启用远程下载 |

改完环境变量后执行 `docker compose up -d` 使其生效。

---

## 三、方式二：手动构建 + 宝塔建容器（备选）

> 适合不熟悉 compose、希望完全在宝塔 UI 操作的用户。

### 3.1 在服务器拉取仓库代码

SSH 登录服务器，任选目录克隆仓库：

```bash
git clone https://github.com/ES-LaoChen/aliyundrive-sub.git
cd aliyundrive-sub
```

> 构建上下文为仓库根目录（含 `Dockerfile` 与 `app.py`）。`.dockerignore` 已排除 `.git`、`.venv`、`data`、`.env` 等，镜像不会包含本地敏感文件。

### 3.2 构建镜像（服务器本地构建）

由于镜像为私有、未推送至公共 Registry，宝塔「编排/拉取」会因 `pull access denied` 失败，**必须先在服务器本地 build**：

```bash
docker build -t aliyundrive-sub:latest .
```

构建完成后确认镜像存在：

```bash
docker images | grep aliyundrive-sub
```

### 3.3 在宝塔面板创建容器

打开 **宝塔面板 → Docker → 容器**，点击「**添加容器**」，按下表填写：

### 1. 基础设置
| 项 | 值 |
| --- | --- |
| 容器名称 | `aliyundrive-sub` |
| 镜像 | `aliyundrive-sub:latest`（选本地镜像，不要填仓库地址） |
| 启动命令 | `python app.py`（默认已在镜像 CMD 中，留空即可） |
| 开机自启 | 勾选 |

### 2. 端口映射
| 容器端口 | 服务器端口 | 说明 |
| --- | --- | --- |
| `8000` | `8000` | Web 服务端口（EXPOSE 8000） |

> 若 8000 被占用，服务器端口可改为其他（如 `9000`），容器端口必须保持 `8000`。

### 3. 目录挂载（持久化）
| 服务器目录 | 容器目录 | 说明 |
| --- | --- | --- |
| `/www/dk/aliyundrive-sub/data` | `/app/data` | SQLite 数据库与运行日志，必须挂载，否则容器重建后数据丢失 |

> 服务器目录请先创建：`mkdir -p /www/dk/aliyundrive-sub/data`

### 4. 环境变量
在「环境变量」中添加（按需，必填项已标注）：

| 变量名 | 示例 / 默认值 | 必填 | 说明 |
| --- | --- | --- | --- |
| `ALIYUNDRIVE_REFRESH_TOKEN` | `你的阿里云盘token` | **必填** | 真实转存凭证；也可留空后在 Web 设置页填写 |
| `DATABASE_URL` | `sqlite:////app/data/app.db` | 否 | 已默认指向持久化卷 |
| `WEB_HOST` | `0.0.0.0` | 否 | 已默认，监听全部网卡 |
| `WEB_PORT` | `8000` | 否 | 已默认 |
| `TZ` | `Asia/Shanghai` | 否 | 已默认 |
| `TG_BOT_TOKEN` | `123456:ABC-DEF...` | 否 | 填则启用 TG 频道监控+通知 |
| `TG_MONITOR_CHANNELS` | `@channel1,@channel2` | 否 | 逗号分隔，需 Bot 已加入频道 |
| `TG_MONITOR_ENABLED` | `true` | 否 | TG 监控总开关 |
| `TG_NOTIFY_ENABLED` | `true` | 否 | TG 命中通知开关 |
| `TG_NOTIFY_CHAT_ID` | `-100123456` | 否 | 通知接收频道/用户 ID |
| `ARIA2_RPC_ENABLE` | `false` | 否 | Aria2 远程下载 |
| `LOG_LEVEL` | `INFO` | 否 | 日志级别 |

填写后点击「**提交 / 创建**」，容器即启动。

---

## 六、验证运行状态

1. 在宝塔「容器」列表查看 `aliyundrive-sub` 状态为「运行中」。
2. 查看容器日志，应见 Web 启动、调度器与 TG 监控线程初始化输出：
   ```bash
   docker logs -f aliyundrive-sub
   ```
3. 健康检查（容器内 `/healthz`）：
   ```bash
   curl -i http://127.0.0.1:8000/healthz
   # 期望返回 200，JSON: {"db":"up","status":"ok"}
   ```
4. 浏览器访问 `http://服务器IP:8000`，应显示应用首页。

---

## 七、宝塔反向代理（推荐，可选）

应用**无内置鉴权**，公网暴露前务必加一层保护：

1. 宝塔「网站 → 反向代理 → 添加反向代理」：
   - 目标 URL：`http://127.0.0.1:8000`
   - 代理名称：`aliyundrive-sub`
2. 在对应网站「设置 → 访问控制 / 密码保护」开启 Basic Auth，设置访问账号密码。
3. 如需 HTTPS，在网站「SSL」中部署证书。

> 仅在服务器端口未直接映射 8000 到公网、或已通过反代+鉴权保护时，才可对外暴露。

---

## 八、常用运维命令

### 编排方式（推荐）
```bash
cd aliyundrive-sub
docker compose ps            # 状态
docker compose logs -f      # 日志
docker compose up -d        # 启动 / 应用改动
docker compose up -d --build  # 代码更新后重建
docker compose down         # 停止（保留 ./data）
```

### 手动建容器方式
```bash
# 查看日志
docker logs -f aliyundrive-sub

# 重启容器
docker restart aliyundrive-sub

# 停止 / 启动
docker stop aliyundrive-sub
docker start aliyundrive-sub

# 更新代码后重建（拉取最新并提交后）
cd aliyundrive-sub
git pull
docker build -t aliyundrive-sub:latest .
docker stop aliyundrive-sub && docker rm aliyundrive-sub
# 回到第六步「添加容器」重新创建（目录挂载与环境变量保持一致）
```

---

## 九、故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| 宝塔编排报 `pull access denied` | `docker-compose.yml` 误用了 `image:` 公共地址。本镜像为私有，请改用 `build:` 段（默认即是）从本地代码构建；仅当你已在服务器手动 `docker build` 后才切到 `image:` |
| 容器启动后退出 | 查看 `docker logs`，多为 `ALIYUNDRIVE_REFRESH_TOKEN` 错误或缺 tzdata（镜像已含 tzdata，正常不会） |
| `/healthz` 返回非 200 | 数据库挂载目录权限不足；确认服务器目录已 `mkdir -p` 且容器有写权限 |
| 调度器/TG 监控不运行 | 确认容器启动命令是 `python app.py`，不是 gunicorn 工厂模式 |
| 阿里云盘转存报会话过期 | 在 Web 设置页更新 `refresh_token` 后重启容器 |

---

## 十、配置项速查（config.py）

完整可配置环境变量见仓库 `config.py`：`ALIYUNDRIVE_REFRESH_TOKEN`、`DATABASE_URL`、`WEB_HOST`、`WEB_PORT`、`TZ`、`LOG_LEVEL`、`TG_*` 系列（监控/通知）、`ARIA2_*` 系列（远程下载）、`SHARE_EXPIRE_THRESHOLD_DAYS`（临期阈值，默认 7 天）。所有字段均有安全默认值，留空仅影响对应功能不启用。
