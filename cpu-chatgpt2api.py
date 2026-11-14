
import os
import time
import socket
import subprocess
import modal

# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "qwen2api-go"
VOLUME_NAME = "qwen2api-go-data"

# 官方 GHCR 镜像
# 固定使用官方 README 提供的 0.2.0 版本
IMAGE = "ghcr.io/xxxxteam/qwen2api_go:0.2.0"

# ---------- 服务配置 ----------
WEB_PORT = 3000

MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 5

TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 300
WEB_STARTUP_TIMEOUT_SECONDS = 300

# ---------- 启动检测 ----------
STARTUP_CHECK_TIMEOUT_SECONDS = 180
STARTUP_CHECK_INTERVAL_SECONDS = 1

# ---------- 资源配置 ----------
# 个人自用，先使用相对保守的配置
CPU = 0.5
MEMORY_MB = 768

# ---------- Modal Secret ----------
# Secret 中至少需要 API_KEY
SECRET_NAME = "qwen2api-secret"


# ====================================================
# 1. 创建持久化数据卷
# ====================================================

# 官方 Docker Compose 数据目录：
# ./data:/app/data
#
# Modal 将 /app/data 映射到持久化 Volume。
# 账号、Token、会话映射保存在 Volume 中。
#
# 不要删除这个 Volume，否则会丢失保存的数据。

vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ====================================================
# 2. 创建运行镜像
# ====================================================

# 官方镜像最终运行环境为 Alpine 3.22。
# Modal Function 需要 Python 运行环境，
# 所以仅额外安装 Python 3，不安装 Go 和 Node.js。

image = (
    modal.Image.from_registry(IMAGE)
    .dockerfile_commands(
        "RUN apk add --no-cache python3",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
    )
)


# ====================================================
# 3. 创建 Modal App
# ====================================================

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 4. 启动 Qwen2API_Go
# ====================================================

@app.function(
    # 最多运行一个容器
    max_containers=MAX_CONTAINERS,

    # CPU 和内存
    cpu=CPU,
    memory=MEMORY_MB,

    # 持久化数据目录
    volumes={
        "/app/data": vol,
    },

    # 函数最长运行时间
    timeout=TIMEOUT_SECONDS,

    # 空闲 300 秒后允许缩容
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,

    # 读取 Modal Secret
    secrets=[
        modal.Secret.from_name(SECRET_NAME),
    ],
)

# 单容器最大并发输入
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS,
)

# Modal Web Server
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
    requires_proxy_auth=False,
)

def ui():

    print("=" * 50, flush=True)
    print("正在启动 Qwen2API_Go...", flush=True)
    print("=" * 50, flush=True)

    # ------------------------------------------------
    # 4.1 检查管理员密钥
    # ------------------------------------------------

    api_key = os.environ.get("API_KEY", "").strip()

    if not api_key:
        raise ValueError(
            "API_KEY 未设置！"
            "请检查 Modal Secret："
            + SECRET_NAME
        )

    # ------------------------------------------------
    # 4.2 组装环境变量
    # ------------------------------------------------

    env = os.environ.copy()

    # 管理员密钥
    env["API_KEY"] = api_key

    # 服务监听地址
    env["LISTEN_ADDRESS"] = "0.0.0.0"

    # 服务监听端口
    env["SERVICE_PORT"] = str(WEB_PORT)

    # 使用文件持久化
    # 账号、Token、会话映射保存到 /app/data
    env["DATA_SAVE_MODE"] = "file"

    # 官方 Qwen 上游地址
    env["QWEN_CHAT_PROXY_URL"] = "https://chat.qwen.ai"

    # 自用保持默认思考输出关闭
    env["OUTPUT_THINK"] = "false"

    # 日志等级
    env["LOG_LEVEL"] = "INFO"

    # ------------------------------------------------
    # 4.3 输出启动配置
    # ------------------------------------------------

    print("API_KEY：已设置", flush=True)
    print("监听地址：0.0.0.0", flush=True)
    print(f"服务端口：{WEB_PORT}", flush=True)
    print("数据保存模式：file", flush=True)
    print("数据目录：/app/data", flush=True)
    print(f"CPU：{CPU}", flush=True)
    print(f"内存：{MEMORY_MB} MB", flush=True)

    # 不打印真实 API_KEY，避免密钥泄露
    # 不预置 Qwen 账号，后续通过管理后台添加

    # ------------------------------------------------
    # 4.4 检查官方程序文件
    # ------------------------------------------------

    executable = "/app/qwen2api"

    if not os.path.isfile(executable):
        raise FileNotFoundError(
            f"没有找到官方程序文件：{executable}"
        )

    # 确保程序具有执行权限
    os.chmod(executable, 0o755)

    # ------------------------------------------------
    # 4.5 启动官方 Go 二进制
    # ------------------------------------------------

    process = subprocess.Popen(
        [executable],
        cwd="/app",
        env=env,
    )

    print(
        f"Qwen2API_Go 已启动，PID：{process.pid}",
        flush=True,
    )

    # ------------------------------------------------
    # 4.6 等待服务端口就绪
    # ------------------------------------------------

    start_time = time.monotonic()
    server_ready = False

    while (
        time.monotonic() - start_time
        < STARTUP_CHECK_TIMEOUT_SECONDS
    ):

        # 检查主进程是否意外退出
        return_code = process.poll()

        if return_code is not None:
            raise RuntimeError(
                f"Qwen2API_Go 启动失败，"
                f"进程退出代码：{return_code}"
            )

        # 检查 3000 端口是否可以连接
        try:
            with socket.create_connection(
                ("127.0.0.1", WEB_PORT),
                timeout=2,
            ):
                server_ready = True
                break

        except OSError:
            time.sleep(
                STARTUP_CHECK_INTERVAL_SECONDS
            )

    # ------------------------------------------------
    # 4.7 最终启动检查
    # ------------------------------------------------

    if not server_ready:

        return_code = process.poll()

        raise RuntimeError(
            f"Qwen2API_Go 启动超时！"
            f"端口 {WEB_PORT} 尚未就绪，"
            f"进程退出代码：{return_code}"
        )

    print("=" * 50, flush=True)
    print("Qwen2API_Go 启动成功！", flush=True)
    print("3000 端口检测通过。", flush=True)
    print("等待 Modal 接管 Web 请求。", flush=True)
    print("=" * 50, flush=True)
