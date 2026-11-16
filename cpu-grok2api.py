
import os
import json
import time
import base64
import socket
import subprocess
import atexit
import urllib.request
import urllib.error

import modal
################################下面参数需要提前写入modal环境变量中
# echo "GROK2API_JWT_SECRET=$(openssl rand -hex 32)"
# echo "GROK2API_CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -base64 32)"
# echo "GROK2API_ADMIN_USERNAME=admin"
# echo "GROK2API_ADMIN_PASSWORD=$(openssl rand -base64 24)"  至少12位，下面代码有判断
###############################################
# ============================================================
#                    配置区（集中管理）
# ============================================================

# ---------- Modal 应用 ----------
APP_NAME = "grok2api"

# ---------- Modal Volume ----------
VOLUME_NAME = "grok2api-data"
QUALITY_VOLUME_NAME = "grok2api-quality-state"

# ---------- 官方 Docker 镜像 ----------
IMAGE = "ghcr.io/chenyme/grok2api:latest"

# ---------- Web 服务 ----------
WEB_PORT = 8000
WEB_STARTUP_TIMEOUT_SECONDS = 180

# ---------- Modal 资源 ----------
MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 8

CPU = 0.5
MEMORY_MB = 256

TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 120

# ---------- Secret ----------
SECRET_NAME = "grok2api-secret"

# ---------- 容器目录 ----------
APP_DIR = "/app"

# SQLite 数据库、媒体文件
DATA_DIR = "/app/data"

# 官方 Docker 配置文件来源
CONFIG_SOURCE_DIR = "/run/grok2api"
CONFIG_SOURCE_FILE = f"{CONFIG_SOURCE_DIR}/config.yaml"

# 官方入口脚本
ENTRYPOINT_PATH = "/usr/local/bin/grok2api-entrypoint"

# 官方 Go 二进制
BINARY_PATH = "/app/grok2api"

# 官方质量守护状态目录
QUALITY_GUARD_DIR = "/var/lib/grok2api-quality-guard"

# ---------- 初始化管理员 ----------
DEFAULT_ADMIN_USERNAME = "admin"

# ---------- 时区 ----------
TIMEZONE = "Asia/Shanghai"


# ============================================================
#                    创建 Modal Volume
# ============================================================

# 主数据 Volume：
# 保存 SQLite 数据库、账号凭据、媒体文件等。
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# 质量守护状态 Volume：
# 当前默认不启用质量守护，但保留官方 Compose 的持久化目录。
quality_vol = modal.Volume.from_name(
    QUALITY_VOLUME_NAME,
    create_if_missing=True,
)


# ============================================================
#                    加载官方 Docker 镜像
# ============================================================

# 官方镜像基于 Alpine。
# Modal 的 Python 函数需要 Python 运行环境，
# 因此仅额外安装 Python，不修改 Grok2API 二进制。
#
# 官方 ENTRYPOINT 由后面的 Shell 包装器接管，
# 在 ui() 中再显式调用官方 entrypoint。

image = modal.Image.from_registry(
    IMAGE,
    setup_dockerfile_commands=[
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
        "RUN ln -sf /usr/bin/pip3 /usr/bin/pip",
    ],
)

# 避免原始 Docker ENTRYPOINT 与 Modal 函数启动过程冲突。
# 保留通过 subprocess 显式调用官方入口脚本的能力。
image = image.entrypoint([
    "/bin/sh",
    "-c",
    'exec "$@"',
    "--",
])


# ============================================================
#                    创建 Modal App
# ============================================================

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ============================================================
#                    生成官方 config.yaml
# ============================================================

def create_config():

    # 从 Modal Secret 读取密钥
    jwt_secret = os.environ.get(
        "GROK2API_JWT_SECRET", ""
    ).strip()

    encryption_key = os.environ.get(
        "GROK2API_CREDENTIAL_ENCRYPTION_KEY", ""
    ).strip()

    admin_username = os.environ.get(
        "GROK2API_ADMIN_USERNAME",
        DEFAULT_ADMIN_USERNAME,
    ).strip()

    admin_password = os.environ.get(
        "GROK2API_ADMIN_PASSWORD", ""
    ).strip()

    # ---------- 检查必要配置 ----------

    if len(jwt_secret) < 32:
        raise ValueError(
            "GROK2API_JWT_SECRET 不存在或长度不足 32 个字符。"
        )

    if not encryption_key:
        raise ValueError(
            "GROK2API_CREDENTIAL_ENCRYPTION_KEY 未设置。"
        )

    # 确认加密密钥是有效的 Base64 32 字节密钥
    try:
        decoded_key = base64.b64decode(
            encryption_key,
            validate=True,
        )
    except Exception as error:
        raise ValueError(
            "凭据加密密钥不是有效的 Base64 字符串。"
        ) from error

    if len(decoded_key) != 32:
        raise ValueError(
            "凭据加密密钥解码后必须恰好为 32 字节。"
        )

    if not admin_username:
        raise ValueError(
            "管理员用户名不能为空。"
        )

    if len(admin_password) < 12:
        raise ValueError(
            "管理员初始密码至少设置 12 个字符。"
        )

    # ---------- 创建目录 ----------

    os.makedirs(
        CONFIG_SOURCE_DIR,
        exist_ok=True,
    )

    os.makedirs(
        DATA_DIR,
        exist_ok=True,
    )

    os.makedirs(
        QUALITY_GUARD_DIR,
        exist_ok=True,
    )

    # ---------- 生成配置文件 ----------
    #
    # 配置字段依据官方 config.example.yaml。
    #
    # SQLite：
    # /app/data/backend.db
    #
    # 媒体：
    # /app/data/media
    #
    # 官方 Docker ENTRYPOINT 会将配置文件复制到
    # /app/config.yaml，再以 grok2api 用户运行程序。

    config = f"""
server:
  listen: "0.0.0.0:{WEB_PORT}"
  maxBodyBytes: 33554432
  trustedProxies: []
  readTimeout: 15m
  requestTimeout: 2h
  swaggerEnabled: false

auth:
  accessTokenTTL: 15m
  refreshTokenTTL: 720h
  secureCookies: true

secrets:
  jwtSecret: {json.dumps(jwt_secret)}
  credentialEncryptionKey: {json.dumps(encryption_key)}

bootstrapAdmin:
  username: {json.dumps(admin_username)}
  password: {json.dumps(admin_password)}

frontend:
  staticPath: "./frontend/dist"

database:
  driver: sqlite
  sqlite:
    path: "./data/backend.db"

runtimeStore:
  driver: memory

deployment:
  replicas: 1
  instanceID: ""
  clusterID: "grok2api"
  sharedMedia: false

media:
  driver: local
  local:
    path: "./data/media"

routing:
  reasoningReplayEnabled: true
  reasoningReplayTTL: 1h
  reasoningReplayMaxEntries: 10240
  accountIsolatedConnections: false
  segmentedSelectorEnabled: true
  segmentedSelectorMinCandidates: 3000
  segmentedSelectorWindowSize: 64

audit:
  bufferSize: 16384
  batchSize: 256
  flushInterval: 250ms
  commitDelay: 5ms
  retentionDays: 7
  ledgerMode: enforce
  ledgerFailureThreshold: 1
  ledgerUnhealthyGrace: 10s
  ledgerQueueHighWatermarkPercent: 90

qualityGuard:
  enabled: false
  model: "grok-4.6"
  mode: hybrid
  activeInterval: 30m
  passivePollInterval: 5s
  requestRetry:
    enabled: false
    maxAttempts: 6
    holdTimeout: 30s
    minOutputTokens: 8
    onExhausted: fail_closed
    autoDisable: false
"""

    # 配置文件权限仅允许当前容器用户读取
    with open(
        CONFIG_SOURCE_FILE,
        "w",
        encoding="utf-8",
    ) as file:
        file.write(config)

    os.chmod(
        CONFIG_SOURCE_FILE,
        0o600,
    )

    print(
        "[LOG] 官方 config.yaml 已生成",
        flush=True,
    )


# ============================================================
#                    Volume 提交
# ============================================================

def commit_on_exit():

    # Modal Volume 支持后台自动提交。
    # 这里额外在正常退出时执行最终提交，
    # 不创建每 30 秒运行一次的后台提交线程。

    for volume, name in [
        (vol, VOLUME_NAME),
        (quality_vol, QUALITY_VOLUME_NAME),
    ]:

        try:
            volume.commit()

            print(
                f"[LOG] {name} 最终提交成功",
                flush=True,
            )

        except Exception as error:

            print(
                f"[LOG] {name} 最终提交失败：{error}",
                flush=True,
            )


# ============================================================
#                    启动 Grok2API
# ============================================================

@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,

    # 主数据持久化
    volumes={
        DATA_DIR: vol,
        QUALITY_GUARD_DIR: quality_vol,
    },

    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,

    # 加载管理员和加密密钥
    secrets=[
        modal.Secret.from_name(SECRET_NAME),
    ],
)

@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS
)

@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
)

def ui():

    print("=" * 60, flush=True)
    print("[LOG] 正在启动 Grok2API...", flush=True)
    print("=" * 60, flush=True)

    # ========================================================
    # 1. 检查必要文件
    # ========================================================

    if not os.path.isfile(BINARY_PATH):
        raise FileNotFoundError(
            f"找不到 Grok2API 二进制：{BINARY_PATH}"
        )

    if not os.path.isfile(ENTRYPOINT_PATH):
        raise FileNotFoundError(
            f"找不到官方 ENTRYPOINT：{ENTRYPOINT_PATH}"
        )

    print(
        "[LOG] 官方二进制和 ENTRYPOINT 检查通过",
        flush=True,
    )

    # ========================================================
    # 2. 检查 Secret
    # ========================================================

    required_secrets = [
        "GROK2API_JWT_SECRET",
        "GROK2API_CREDENTIAL_ENCRYPTION_KEY",
        "GROK2API_ADMIN_USERNAME",
        "GROK2API_ADMIN_PASSWORD",
    ]

    for key in required_secrets:

        if not os.environ.get(key, "").strip():

            raise ValueError(
                f"Modal Secret 缺少环境变量：{key}"
            )

    print(
        "[LOG] Secret 检查通过",
        flush=True,
    )

    # ========================================================
    # 3. 创建配置文件
    # ========================================================

    create_config()

    # ========================================================
    # 4. 设置环境变量
    # ========================================================

    env = os.environ.copy()

    env["TZ"] = TIMEZONE

    env["GROK2API_CONFIG_SOURCE"] = CONFIG_SOURCE_FILE

    env["GROK2API_QUALITY_GUARD_DIR"] = QUALITY_GUARD_DIR

    env["PYTHONUNBUFFERED"] = "1"

    print(
        "[LOG] 运行环境准备完成",
        flush=True,
    )

    # ========================================================
    # 5. 注册退出时 Volume 最终提交
    # ========================================================

    atexit.register(
        commit_on_exit
    )

    # ========================================================
    # 6. 启动官方 Docker ENTRYPOINT
    # ========================================================
    #
    # 官方 Dockerfile：
    #
    # ENTRYPOINT:
    # /usr/local/bin/grok2api-entrypoint
    #
    # CMD:
    # /app/grok2api
    # --config /app/config.yaml
    # --listen 0.0.0.0:8000
    #
    # ENTRYPOINT 会：
    # 1. 检查配置文件。
    # 2. 将配置复制到 /app/config.yaml。
    # 3. 设置文件权限。
    # 4. 使用 grok2api 用户启动 Go 程序。

    command = [
        ENTRYPOINT_PATH,
        BINARY_PATH,
        "--config",
        "/app/config.yaml",
        "--listen",
        f"0.0.0.0:{WEB_PORT}",
    ]

    print(
        "[LOG] 正在启动官方 Grok2API ENTRYPOINT...",
        flush=True,
    )

    process = subprocess.Popen(
        command,
        cwd=APP_DIR,
        env=env,
        stdout=None,
        stderr=None,
    )

    print(
        f"[LOG] Grok2API 进程已启动，PID：{process.pid}",
        flush=True,
    )

    # ========================================================
    # 7. 等待 HTTP 健康检查
    # ========================================================

    health_url = (
        f"http://127.0.0.1:{WEB_PORT}/healthz"
    )

    start_time = time.monotonic()

    while (
        time.monotonic() - start_time
        < WEB_STARTUP_TIMEOUT_SECONDS
    ):

        # 检查 Go 进程是否提前退出
        exit_code = process.poll()

        if exit_code is not None:

            raise RuntimeError(
                "Grok2API 启动失败，"
                f"进程退出代码：{exit_code}"
            )

        try:

            request = urllib.request.Request(
                health_url,
                headers={
                    "User-Agent": "Modal-Grok2API-HealthCheck",
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=5,
            ) as response:

                if response.status == 200:

                    print(
                        "[LOG] HTTP 健康检查通过",
                        flush=True,
                    )

                    print(
                        f"[LOG] 服务监听端口：{WEB_PORT}",
                        flush=True,
                    )

                    print(
                        f"[LOG] SQLite 数据目录：{DATA_DIR}",
                        flush=True,
                    )

                    print(
                        "[LOG] Grok2API 启动成功",
                        flush=True,
                    )

                    return

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ):

            time.sleep(1)

    # ========================================================
    # 8. 启动超时处理
    # ========================================================

    if process.poll() is None:

        process.terminate()

        try:
            process.wait(timeout=10)

        except subprocess.TimeoutExpired:

            process.kill()

    raise RuntimeError(
        "Grok2API 启动超时，请检查 Modal 容器日志。"
    )
