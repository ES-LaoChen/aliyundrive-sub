# ============================================================
#  阿里云盘订阅转存服务 - Docker 镜像构建文件
#  构建上下文（build context）：本文件所在目录（即含 app.py 的 aliyundrive-sub/）
#
#  构建示例：
#    docker build -t aliyundrive-sub:latest .
#  运行示例（端口 8000，数据持久化到宿主机 ./data）：
#    docker run -d --name aliyundrive-sub \
#      -p 8000:8000 \
#      -v $(pwd)/data:/app/data \
#      -e ALIYUNDRIVE_REFRESH_TOKEN=你的token \
#      aliyundrive-sub:latest
# ============================================================

# ---- 基础镜像： Debian slim，纯 Python 依赖均有 wheel，无需编译工具链 ----
FROM python:3.11-slim

# ---- 容器化默认环境变量 ----
# PYTHONUNBUFFERED=1           实时输出日志到容器标准流（便于宝塔/ docker logs 查看）
# PYTHONDONTWRITEBYTECODE=1    不写 __pycache__，减少镜像/挂载层污染
# PYTHONUTF8=1                 强制 UTF-8，避免 emoji/中文日志在 GBK 环境崩溃
# LANG/LC_ALL                  容器 locale 固定 UTF-8
# TZ                           时区（与 app 配置默认值一致）
# WEB_HOST=0.0.0.0             容器内监听全部网卡（宝塔/反代负责对外暴露与鉴权）
# WEB_PORT                     服务端口（与 EXPOSE 一致）
# DATABASE_URL                 SQLite 绝对路径，落在持久化卷 /app/data 内
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=Asia/Shanghai \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    DATABASE_URL=sqlite:////app/data/app.db

# ---- 系统依赖：tzdata 提供 IANA 时区库，ZoneInfo("Asia/Shanghai") 才能解析 ----
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# ---- 工作目录 ----
WORKDIR /app

# ---- 先拷贝依赖清单并安装（利用 Docker 层缓存，改源码不必重装依赖） ----
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt tzdata

# ---- 拷贝项目源码 ----
COPY . .

# ---- 持久化目录：SQLite 数据库落在此处，宿主机挂载后数据不随容器销毁丢失 ----
VOLUME ["/app/data"]

# ---- 暴露服务端口（宝塔“端口映射”按此映射即可） ----
EXPOSE 8000

# ---- 健康检查：复用应用内置 /healthz（DB 可达返回 200） ----
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# ---- 启动命令 ----
# 直接运行 app.py：main() 会依次完成 装配服务 -> 建表 -> 启动调度器 -> 启动 TG 监控 -> 启动 Web。
# 注意：请勿改用 gunicorn 工厂式启动，否则后台调度器 / TG 监控线程不会运行。
CMD ["python", "app.py"]
