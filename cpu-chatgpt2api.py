
import os
import time
import socket
import subprocess
import modal

# ====================== 配置区（所有可调参数都在这里） ======================

APP_NAME = "qwen2api-go"
VOLUME_NAME = "qwen2api-go-data"

# XxxXTeam 官方 GHCR 镜像
# 官方 README 提供的固定版本
IMAGE = "ghcr.io/xxxxteam/qwen2api_go:latest"

# ---------- 服务配置 ----------
WEB_PORT = 3000

# 个人自用：最多运行一个容器
MAX_CONTAINERS = 1

# 单容器最大并发输入
MAX_CONCURRENT_INPUTS = 3

# 单次函数最长运行时间
TIMEOUT_SECONDS = 3600

# 空闲 300 秒后缩容
SCALEDOWN_WINDOW_SECONDS = 300

# Modal Web Server 启动等待时间
WEB_STARTUP_TIMEOUT_SECONDS = 300

# ---------- 启动检测 ----------
STARTUP_CHECK_TIMEOUT_SECONDS = 180
STARTUP_CHECK_INTERVAL_SECONDS = 1

# ---------- 资源配置 ----------
CPU = 0.5
MEMORY_MB = 768

# ---------- Modal Secret ----------
# 在 Modal Settings -> Secrets 中提前创建
SECRET_NAME = "qwen2api-secret"


# ====================================================
# 1. 创建持久化数据卷
# ====================================================

# 官方 Docker 部署使用：
# ./data:/app/data
#
# Modal 中使用 Volume 替代本地目录。
# 账号、Token、会话映射等数据保存在这个目录。
#
# 不要随意删除这个 Volume，否则可能丢失数据。

vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ====================================================
# 2. 创建运行镜像
# ====================================================

# 使用 XxxXTeam 官方预构建镜像。
#
# 官方最终镜像基于 Alpine 3.22，
# 运行程序是 Go 编译后的独立二进制。
#
# Modal Function 需要 Python 运行环境，
# 因此只额外安装 Python 3。
#
# 不安装 Go、Node.js、Chromium，
# 不在 Modal 启动时重新编译项目。

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
    # 最多一个容器
    max_containers=MAX_CONTAINERS,

    # CPU 和内存
    cpu=CPU,
    memory=MEMORY_MB,

    # 将 Modal Volume 挂载到官方数据目录
    volumes={
        "/app/data": vol,
    },

    # 函数最长运行时间
    timeout=TIMEOUT_SECONDS,

    # 空闲缩容时间
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,

    # 加载管理员密钥
    secrets=[
        modal.Secret.from_name(SECRET_NAME),
    ],
)

# 设置单容器并发
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS,
)

# 创建 Web 服务
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,

    # 不要求访问者提供 Modal Proxy Token
    requires_proxy_auth=False,
)

def ui():

    print("=" * 50, flush=True)
    print("正在启动 XxxXTeam Qwen2API_Go...", flush=True)
    print("=" * 50, flush=True)

    # ------------------------------------------------
    # 4.1 检查管理员 API_KEY
    # ------------------------------------------------

    api_key = os.environ.get("API_KEY", "").strip()

    if not api_key:
        raise ValueError(
            "API_KEY 未设置！"
            "请检查 Modal Secret：" + SECRET_NAME
        )

    # ------------------------------------------------
    # 4.2 组装官方环境变量
    # ------------------------------------------------

    env = os.environ.copy()

    # 管理员密钥
    # 第一个 API_KEY 用于登录后台
    env["API_KEY"] = api_key

    # 监听所有网卡
    env["LISTEN_ADDRESS"] = "0.0.0.0"

    # 官方服务端口
    env["SERVICE_PORT"] = str(WEB_PORT)

    # 使用文件持久化
    # 数据写入 /app/data
    env["DATA_SAVE_MODE"] = "file"

    # Qwen 官方上游地址
    env["QWEN_CHAT_PROXY_URL"] = "https://chat.qwen.ai"

    # 默认关闭思考内容输出
    env["OUTPUT_THINK"] = "false"

    # ------------------------------------------------
    # 4.3 打印配置（不泄露密钥）
    # ------------------------------------------------

    print("项目：XxxXTeam/Qwen2API_Go", flush=True)
    print("镜像：" + IMAGE, flush=True)
    print("API_KEY：已设置", flush=True)
    print("监听地址：0.0.0.0", flush=True)
    print(f"监听端口：{WEB_PORT}", flush=True)
    print("数据模式：file", flush=True)
    print("数据目录：/app/data", flush=True)
    print(f"CPU：{CPU}", flush=True)
    print(f"内存：{MEMORY_MB} MB", flush=True)

    # ------------------------------------------------
    # 4.4 检查官方程序文件
    # ------------------------------------------------

    executable = "/app/qwen2api"

    if not os.path.isfile(executable):
        raise FileNotFoundError(
            "官方程序不存在：" + executable
        )

    # 检查程序是否具有执行权限
    if not os.access(executable, os.X_OK):
        raise PermissionError(
            "官方程序没有执行权限：" + executable
        )

    # ------------------------------------------------
    # 4.5 启动官方 Go 程序
    # ------------------------------------------------

    process = subprocess.Popen(
        [executable],
        cwd="/app",
        env=env,
    )

    print(
        f"Qwen2API_Go 进程已启动，PID：{process.pid}",
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

        # 检查主程序是否提前退出
        return_code = process.poll()

        if return_code is not None:
            raise RuntimeError(
                f"Qwen2API_Go 提前退出，"
                f"退出代码：{return_code}"
            )

        # 检查本地 3000 端口
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
    # 4.7 检查最终启动状态
    # ------------------------------------------------

    if not server_ready:

        return_code = process.poll()

        raise RuntimeError(
            f"服务启动超时！"
            f"端口 {WEB_PORT} 无法连接，"
            f"进程退出代码：{return_code}"
        )

    print("=" * 50, flush=True)
    print("Qwen2API_Go 启动成功！", flush=True)
    print("项目：XxxXTeam/Qwen2API_Go", flush=True)
    print("端口：3000", flush=True)
    print("本地端口检测：通过", flush=True)
    print("等待 Modal 接管 Web 请求。", flush=True)
    print("=" * 50, flush=True)
