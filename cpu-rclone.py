
import os
import subprocess
import time
import threading
import atexit
import modal

# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "rclone"
VOLUME_NAME = "rclone-data"

# ---------- 官方镜像 ----------
IMAGE = "rclone/rclone:latest"

# 给官方 Alpine 镜像添加 Modal 所需的 Python
PYTHON_VERSION = "3.12"

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

# ====================================================


# ====================================================
# 1. 持久化数据卷
# ====================================================
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ====================================================
# 2. 官方镜像
# ====================================================
# 保留 Rclone 官方镜像
# 仅增加 Modal Function 所需的 Python 3.12
# 不更换基础镜像，不自行编译 Rclone

image = modal.Image.from_registry(
    IMAGE,
    add_python=PYTHON_VERSION,
)

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 3. Volume 自动提交
# ====================================================
def commit_worker():
    """定期提交 Rclone 配置变化。"""

    while True:
        time.sleep(30)

        try:
            vol.commit()
            print("Volume 自动提交成功。", flush=True)

        except Exception as e:
            print(
                f"Volume 自动提交失败：{e}",
                flush=True,
            )


def commit_on_exit():
    """退出时尝试最后一次提交。"""

    try:
        vol.commit()
        print("Volume 最终提交成功。", flush=True)

    except Exception as e:
        print(
            f"Volume 最终提交失败：{e}",
            flush=True,
        )


# ====================================================
# 4. 启动 Rclone
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

    print("======================================", flush=True)
    print("正在启动 Rclone...", flush=True)
    print("======================================", flush=True)

    # ------------------------------------------------
    # 4.1 检查 Python 环境
    # ------------------------------------------------
    import sys

    print(
        f"Python 版本：{sys.version}",
        flush=True,
    )

    # ------------------------------------------------
    # 4.2 检查 Rclone
    # ------------------------------------------------
    result = subprocess.run(
        ["rclone", "version"],
        capture_output=True,
        text=True,
        check=True,
    )

    print(result.stdout, flush=True)

    # ------------------------------------------------
    # 4.3 检查 Secret
    # ------------------------------------------------
    web_user = os.environ.get(
        "RCLONE_WEB_USER", ""
    ).strip()

    web_pass = os.environ.get(
        "RCLONE_WEB_PASS", ""
    ).strip()

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
            "RCLONE_WEB_PASS 不能少于 12 个字符。"
        )

    # ------------------------------------------------
    # 4.4 创建目录
    # ------------------------------------------------
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    # ------------------------------------------------
    # 4.5 设置环境变量
    # ------------------------------------------------
    env = os.environ.copy()

    env["TZ"] = "Asia/Shanghai"
    env["RCLONE_CONFIG"] = CONFIG_FILE
    env["XDG_CONFIG_HOME"] = CONFIG_DIR

    # ------------------------------------------------
    # 4.6 启动 Volume 提交线程
    # ------------------------------------------------
    threading.Thread(
        target=commit_worker,
        daemon=True,
    ).start()

    atexit.register(commit_on_exit)

    # ------------------------------------------------
    # 4.7 Rclone 启动命令
    # ------------------------------------------------
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
    # 4.8 启动服务
    # ------------------------------------------------
    print("======================================", flush=True)
    print("Rclone 启动配置：", flush=True)
    print(f"官方镜像：{IMAGE}", flush=True)
    print(f"Python：{PYTHON_VERSION}", flush=True)
    print(f"Web 端口：{WEB_PORT}", flush=True)
    print("身份验证：已启用", flush=True)
    print(f"配置目录：{CONFIG_DIR}", flush=True)
    print(f"配置文件：{CONFIG_FILE}", flush=True)
    print(f"缓存目录：{CACHE_DIR}", flush=True)
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
    # 4.9 检查是否立即退出
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
