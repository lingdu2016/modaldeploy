
import os
import time
import socket
import subprocess
import modal

# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "slash"
VOLUME_NAME = "slash-data"

# 官方 Slash 镜像
IMAGE = "yourselfhosted/slash:latest"

# ---------- 服务配置 ----------
WEB_PORT = 5231
DATA_DIR = "/var/opt/slash"
WORK_DIR = "/usr/local/slash"

# ---------- Modal 资源配置 ----------
MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 10

CPU = 0.25
MEMORY_MB = 512

TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 100
WEB_STARTUP_TIMEOUT_SECONDS = 120

# ---------- Slash 配置 ----------
SLASH_MODE = "prod"
SLASH_DRIVER = "sqlite"
TIMEZONE = "Asia/Shanghai"

# ====================================================


# ====================== 创建 Modal 资源 ======================

# 使用原来的持久化 Volume
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True
)

# 使用 Slash 官方镜像
# 安装 Python，供 Modal Function 运行
image = modal.Image.from_registry(
    IMAGE,
    setup_dockerfile_commands=[
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
        "RUN ln -sf /usr/bin/pip3 /usr/bin/pip",
    ],
)

# 关键修复：
# 官方 Slash 镜像原本的 ENTRYPOINT 是 ./slash
# Modal 启动 Python Function 时会传入自己的启动参数
# 通过 shell 转交参数，避免 Slash 错误接收 Modal 的 -u 参数
image = image.entrypoint([
    "/bin/sh",
    "-c",
    'exec "$@"',
    "--",
])

# 创建 Modal App
app = modal.App(
    name=APP_NAME,
    image=image
)

# ====================================================


# ====================== 启动 Slash 服务 ======================

@app.function(
    cpu=CPU,
    memory=MEMORY_MB,
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    max_containers=MAX_CONTAINERS,
    volumes={
        DATA_DIR: vol
    }
)
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS
)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS
)
def ui():

    # ---------- 准备数据目录 ----------
    os.makedirs(DATA_DIR, exist_ok=True)

    # ---------- 配置环境变量 ----------
    env = os.environ.copy()
    env["TZ"] = TIMEZONE

    # ---------- Slash 启动参数 ----------
    # 使用官方支持的命令行参数
    command = [
        "./slash",
        "--mode", SLASH_MODE,
        "--port", str(WEB_PORT),
        "--data", DATA_DIR,
        "--driver", SLASH_DRIVER,
    ]

    # ---------- 启动 Slash ----------
    print("正在启动 Slash...")
    print(f"启动目录：{WORK_DIR}")
    print(f"数据目录：{DATA_DIR}")
    print(f"监听端口：{WEB_PORT}")

    process = subprocess.Popen(
        command,
        cwd=WORK_DIR,
        env=env
    )

    print(f"Slash 进程 PID: {process.pid}")

    # ---------- 等待服务端口启动 ----------
    start_time = time.monotonic()

    while time.monotonic() - start_time < WEB_STARTUP_TIMEOUT_SECONDS:

        # 检查 Slash 进程是否意外退出
        exit_code = process.poll()

        if exit_code is not None:
            raise RuntimeError(
                f"Slash 启动失败，退出代码：{exit_code}"
            )

        # 检查服务端口是否开始监听
        try:
            with socket.create_connection(
                ("127.0.0.1", WEB_PORT),
                timeout=1
            ):
                print(
                    f"Slash 已成功启动，监听端口：{WEB_PORT}"
                )
                break

        except OSError:
            time.sleep(0.5)

    else:
        raise RuntimeError(
            "Slash 启动超时，请检查 Modal 容器日志"
        )

# ====================================================
