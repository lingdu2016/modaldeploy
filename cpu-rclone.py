
import os
import subprocess
import time
import modal

# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "rclone"
VOLUME_NAME = "rclone-data"

# ---------- 官方镜像 ----------
# 使用 Rclone 官方 Docker 镜像
# 不使用自定义 Dockerfile，不进行镜像预热
IMAGE = "rclone/rclone:latest"

# ---------- Web 服务配置 ----------
WEB_PORT = 5572

MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 10

TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 300
WEB_STARTUP_TIMEOUT_SECONDS = 180

# ---------- 资源配置 ----------
CPU = 0.5
MEMORY_MB = 1024

# ---------- Modal Secret ----------
SECRET_NAME = "rclone-secret"

# ---------- 持久化目录 ----------
CONFIG_DIR = "/config"
CACHE_DIR = "/cache"
CONFIG_FILE = "/config/rclone.conf"

# ---------- 身份验证环境变量 ----------
# RCLONE_WEB_USER：Web GUI 登录用户名
# RCLONE_WEB_PASS：Web GUI 登录密码
# 两项均需要在 Modal Secret 中设置

# ====================================================


# ====================================================
# 1. 持久化数据卷
# ====================================================
# 保存 Rclone 的配置文件和远程存储认证信息
# 不要删除这个 Volume
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ====================================================
# 2. 官方镜像
# ====================================================
# 直接使用官方镜像
# 官方镜像自带 rclone 二进制文件
# 无需安装 Python 依赖或编译程序
image = modal.Image.from_registry(IMAGE)

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 3. 启动 Rclone Web GUI
# ====================================================
@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={
        CONFIG_DIR: vol,
    },
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    secrets=[
        modal.Secret.from_name(SECRET_NAME),
    ],
)
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS,
)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
)
def ui():
    """启动 Rclone 官方 Web GUI。"""

    print("======================================", flush=True)
    print("正在启动 Rclone...", flush=True)
    print("======================================", flush=True)

    # ------------------------------------------------
    # 3.1 检查身份验证配置
    # ------------------------------------------------
    web_user = os.environ.get("RCLONE_WEB_USER", "").strip()
    web_pass = os.environ.get("RCLONE_WEB_PASS", "").strip()

    if not web_user:
        raise ValueError(
            "RCLONE_WEB_USER 未设置，请检查 Modal Secret。"
        )

    if not web_pass:
        raise ValueError(
            "RCLONE_WEB_PASS 未设置，请检查 Modal Secret。"
        )

    if len(web_pass) < 12:
        raise ValueError(
            "RCLONE_WEB_PASS 长度不能少于 12 个字符。"
        )

    # ------------------------------------------------
    # 3.2 准备目录
    # ------------------------------------------------
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    print(f"配置目录：{CONFIG_DIR}", flush=True)
    print(f"配置文件：{CONFIG_FILE}", flush=True)
    print(f"缓存目录：{CACHE_DIR}", flush=True)

    # ------------------------------------------------
    # 3.3 组装环境变量
    # ------------------------------------------------
    env = os.environ.copy()

    env["TZ"] = "Asia/Shanghai"
    env["XDG_CONFIG_HOME"] = CONFIG_DIR

    # 明确指定配置文件位置
    env["RCLONE_CONFIG"] = CONFIG_FILE

    # ------------------------------------------------
    # 3.4 启动参数
    # ------------------------------------------------
    # rclone rcd：
    # 启动 Remote Control API
    #
    # --rc-web-gui：
    # 在同一个端口提供官方 Web GUI
    #
    # --rc-user / --rc-pass：
    # 启用身份验证
    #
    # --rc-web-gui-no-open-browser：
    # 云服务器不需要自动打开浏览器
    #
    # --config：
    # 确保远程配置写入持久化 Volume
    #
    # --cache-dir：
    # 缓存放在临时目录，不占用持久化 Volume

    command = [
        "rclone",
        "rcd",

        "--rc-addr",
        f"0.0.0.0:{WEB_PORT}",

        "--rc-user",
        web_user,

        "--rc-pass",
        web_pass,

        "--rc-web-gui",
        "--rc-web-gui-no-open-browser",

        "--config",
        CONFIG_FILE,

        "--cache-dir",
        CACHE_DIR,

        "--log-level",
        "INFO",
    ]

    # ------------------------------------------------
    # 3.5 启动服务
    # ------------------------------------------------
    print("======================================", flush=True)
    print("Rclone 启动配置：", flush=True)
    print(f"Web 端口：{WEB_PORT}", flush=True)
    print("身份验证：已启用", flush=True)
    print("持久化配置：已启用", flush=True)
    print(f"CPU：{CPU}", flush=True)
    print(f"内存：{MEMORY_MB} MB", flush=True)
    print("======================================", flush=True)

    process = subprocess.Popen(
        command,
        env=env,
    )

    print(
        f"Rclone 进程已启动，PID：{process.pid}",
        flush=True,
    )

    # ------------------------------------------------
    # 3.6 检查进程是否立即退出
    # ------------------------------------------------
    time.sleep(3)

    return_code = process.poll()

    if return_code is not None:
        raise RuntimeError(
            f"Rclone 启动失败，退出代码：{return_code}"
        )

    print("======================================", flush=True)
    print("Rclone 进程运行正常。", flush=True)
    print("等待 Web GUI 服务就绪。", flush=True)
    print("======================================", flush=True)
