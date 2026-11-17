import os
import json
import time
import base64
import socket
import subprocess
import atexit
import urllib.request
import urllib.error
import stat

import modal


# ============================================================
# Modal 环境变量（在创建 Secret 时设置）
# ============================================================
# GROK2API_JWT_SECRET
#   openssl rand -hex 32
#
# GROK2API_CREDENTIAL_ENCRYPTION_KEY
#   openssl rand -base64 32
#
# GROK2API_ADMIN_USERNAME
#   admin
#
# GROK2API_ADMIN_PASSWORD
#   设置一个强密码。官方示例没有规定必须至少 12 个字符，
#   本部署代码不再额外设置 12 位长度限制。
#
# Secret 名称：grok2api-secret
# ============================================================


# ============================================================
#                    配置区（集中管理）
# ============================================================

# ---------- Modal 应用 ----------
APP_NAME = "grok2api"

# ---------- Modal Volume ----------
# 只使用一个 Volume，保存 SQLite 数据库和媒体文件。
VOLUME_NAME = "grok2api-data"

# ---------- 官方 Docker 镜像 ----------
IMAGE = "ghcr.io/chenyme/grok2api:latest"

# ---------- Web 服务 ----------
WEB_PORT = 8000
WEB_STARTUP_TIMEOUT_SECONDS = 300

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

# SQLite 数据库和媒体文件目录
DATA_DIR = "/app/data"
MEDIA_DIR = "/app/data/media"
DATABASE_FILE = "/app/data/backend.db"

# 官方配置文件来源
CONFIG_SOURCE_DIR = "/run/grok2api"
CONFIG_SOURCE_FILE = f"{CONFIG_SOURCE_DIR}/config.yaml"

# 官方 ENTRYPOINT 和 Go 二进制
ENTRYPOINT_PATH = "/usr/local/bin/grok2api-entrypoint"
BINARY_PATH = "/app/grok2api"

# 官方 Quality Guard 状态目录
# 当前 config.yaml 中关闭 Quality Guard，因此不单独挂载 Volume。
QUALITY_GUARD_DIR = "/var/lib/grok2api-quality-guard"

# 官方 Dockerfile 创建的运行用户
APP_UID = 10001
APP_GID = 10001

# ---------- 初始化管理员 ----------
DEFAULT_ADMIN_USERNAME = "admin"

# ---------- 时区 ----------
TIMEZONE = "Asia/Shanghai"


# ============================================================
#                    创建 Modal Volume
# ============================================================

# 一个 Volume 同时保存：
# /app/data/backend.db
# /app/data/media/
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ============================================================
#                    加载官方 Docker 镜像
# ============================================================

# 官方镜像基于 Alpine。
# Modal Python Function 需要 Python 运行环境，因此安装 Python。
# 不修改官方 Go 二进制和官方 ENTRYPOINT。
image = modal.Image.from_registry(
    IMAGE,
    setup_dockerfile_commands=[
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
        "RUN ln -sf /usr/bin/pip3 /usr/bin/pip",
    ],
)

# 使用 Shell 包装器接收 Modal 的启动参数。
# ui() 内部仍会显式调用官方 ENTRYPOINT。
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
    """从 Modal Secret 读取密钥并生成官方配置文件。"""

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

    # 官方要求凭据加密密钥为 Base64 编码的 32 字节密钥。
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

    # 不额外规定管理员密码必须达到 12 位。
    # 只检查是否为空；密码强度由用户自行设置。
    if not admin_password:
        raise ValueError(
            "GROK2API_ADMIN_PASSWORD 未设置。"
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
        MEDIA_DIR,
        exist_ok=True,
    )

    # Quality Guard 当前关闭，不挂载独立 Volume。
    # 官方 ENTRYPOINT 启动时会创建并设置该目录权限。

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
    # 官方 Docker ENTRYPOINT 会把该配置复制到
    # /app/config.yaml，再以 UID/GID 10001 运行程序。

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

    # 配置文件先由 root 创建。
    # 官方 ENTRYPOINT 会复制到 /app/config.yaml，
    # 并将目标文件 chown 给 grok2api 用户。
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
#             修复 Modal Volume 的目录和文件权限
# ============================================================

def ensure_volume_permissions():
    """
    将 Volume 中的目录和文件所有权调整为官方运行用户：
    UID 10001 / GID 10001。

    原因：
    Modal Volume 挂载到 /app/data 后，会覆盖 Docker 镜像
    原本创建并 chown 的 /app/data 目录。

    因此不能只依赖 Dockerfile 中的 chown；
    必须在启动官方 ENTRYPOINT 之前处理挂载目录权限。
    """

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)

    if os.geteuid() != 0:
        raise PermissionError(
            "当前容器不是 root 用户，无法修复 Modal Volume 权限。"
            f"当前 EUID={os.geteuid()}，"
            f"需要在启动官方 ENTRYPOINT 前以 root 执行权限修复。"
        )

    print(
        "[LOG] 正在检查 Modal Volume 文件权限...",
        flush=True,
    )

    changed_count = 0

    # 遍历数据目录，修正目录和文件的 UID/GID。
    # 仅在发现所有权不正确时执行 chown。
    for current_root, dir_names, file_names in os.walk(
        DATA_DIR,
        topdown=True,
        followlinks=False,
    ):
        paths = [current_root]

        paths.extend(
            os.path.join(current_root, name)
            for name in dir_names
        )

        paths.extend(
            os.path.join(current_root, name)
            for name in file_names
        )

        for path in paths:
            try:
                file_stat = os.lstat(path)
            except FileNotFoundError:
                # 文件可能在遍历过程中被清理，跳过即可。
                continue

            if (
                file_stat.st_uid != APP_UID
                or file_stat.st_gid != APP_GID
            ):
                os.chown(
                    path,
                    APP_UID,
                    APP_GID,
                    follow_symlinks=False,
                )
                changed_count += 1

            # 确保目录所有者具有读、写、执行权限。
            if stat.S_ISDIR(file_stat.st_mode):
                current_mode = stat.S_IMODE(file_stat.st_mode)
                required_mode = (
                    current_mode
                    | stat.S_IRUSR
                    | stat.S_IWUSR
                    | stat.S_IXUSR
                )

                if required_mode != current_mode:
                    os.chmod(path, required_mode)

            # 确保普通文件所有者至少具有读、写权限。
            elif stat.S_ISREG(file_stat.st_mode):
                current_mode = stat.S_IMODE(file_stat.st_mode)
                required_mode = (
                    current_mode
                    | stat.S_IRUSR
                    | stat.S_IWUSR
                )

                if required_mode != current_mode:
                    os.chmod(path, required_mode)

    # 再次明确确保关键目录权限。
    os.chown(
        DATA_DIR,
        APP_UID,
        APP_GID,
    )
    os.chown(
        MEDIA_DIR,
        APP_UID,
        APP_GID,
    )

    os.chmod(DATA_DIR, 0o750)
    os.chmod(MEDIA_DIR, 0o750)

    # 如果数据库已经存在，确保运行用户可读写。
    if os.path.exists(DATABASE_FILE):
        os.chown(
            DATABASE_FILE,
            APP_UID,
            APP_GID,
        )
        os.chmod(
            DATABASE_FILE,
            0o600,
        )

    # 检查目录最终所有权。
    data_stat = os.stat(DATA_DIR)
    media_stat = os.stat(MEDIA_DIR)

    if (
        data_stat.st_uid != APP_UID
        or data_stat.st_gid != APP_GID
    ):
        raise PermissionError(
            f"{DATA_DIR} 所有权修复失败。"
        )

    if (
        media_stat.st_uid != APP_UID
        or media_stat.st_gid != APP_GID
    ):
        raise PermissionError(
            f"{MEDIA_DIR} 所有权修复失败。"
        )

    print(
        f"[LOG] Volume 权限检查完成，修正项目数：{changed_count}",
        flush=True,
    )
    print(
        f"[LOG] {DATA_DIR} 所有者：{APP_UID}:{APP_GID}",
        flush=True,
    )
    print(
        f"[LOG] {MEDIA_DIR} 所有者：{APP_UID}:{APP_GID}",
        flush=True,
    )


# ============================================================
#                    Volume 最终提交
# ============================================================

def commit_on_exit():
    """容器正常退出时提交主数据 Volume。"""

    try:
        vol.commit()
        print(
            f"[LOG] {VOLUME_NAME} 最终提交成功",
            flush=True,
        )
    except Exception as error:
        print(
            f"[LOG] {VOLUME_NAME} 最终提交失败：{error}",
            flush=True,
        )


# ============================================================
#                    启动 Grok2API
# ============================================================

@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,

    # SQLite 数据库和媒体文件共用一个 Volume。
    volumes={
        DATA_DIR: vol,
    },

    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,

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
    # 1. 检查官方程序文件
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
    # 4. 设置运行环境变量
    # ========================================================

    env = os.environ.copy()

    env["TZ"] = TIMEZONE
    env["GROK2API_CONFIG_SOURCE"] = CONFIG_SOURCE_FILE
    env["GROK2API_QUALITY_GUARD_DIR"] = QUALITY_GUARD_DIR
    env["PYTHONUNBUFFERED"] = "1"

    # ========================================================
    # 5. 修复 Volume 权限（本次重点修复）
    # ========================================================

    ensure_volume_permissions()

    print(
        "[LOG] 运行环境准备完成",
        flush=True,
    )

    # ========================================================
    # 6. 注册退出时 Volume 最终提交
    # ========================================================

    atexit.register(commit_on_exit)

    # ========================================================
    # 7. 启动官方 Docker ENTRYPOINT
    # ========================================================
    #
    # 官方 Dockerfile 的 CMD：
    # /app/grok2api
    # --config /app/config.yaml
    # --listen 0.0.0.0:8000
    #
    # 官方 ENTRYPOINT 会：
    # 1. 检查配置文件来源。
    # 2. 将配置复制到 /app/config.yaml。
    # 3. 将配置文件所有权设置为 grok2api 用户。
    # 4. 设置配置文件权限。
    # 5. 使用 UID/GID 10001 启动 Go 程序。

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
    # 8. 等待 HTTP 健康检查
    # ========================================================

    health_url = (
        f"http://127.0.0.1:{WEB_PORT}/healthz"
    )

    start_time = time.monotonic()

    while (
        time.monotonic() - start_time
        < WEB_STARTUP_TIMEOUT_SECONDS
    ):

        # Go 进程提前退出，立即显示退出代码。
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
    # 9. 启动超时处理
    # ========================================================

    if process.poll() is None:
        process.terminate()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    raise RuntimeError(
        "Grok2API 启动超时，请检查 Modal 容器日志。"
    )
