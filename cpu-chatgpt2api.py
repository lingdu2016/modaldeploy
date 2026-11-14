
import os
import time
import subprocess
import urllib.request
import urllib.error

import modal


# ====================== 配置区（所有可调参数都在这里） ======================

# ---------- Modal 应用 ----------
APP_NAME = "yangyu-qwen2api"

# ---------- 官方 Docker 镜像 ----------
IMAGE = "yujunzhixue/qwen2api:latest"

# ---------- 持久化存储 ----------
# 只创建一个 Volume，同时保存项目数据和运行日志
VOLUME_NAME = "qwen2api-data"

# Volume 在容器中的统一挂载位置
VOLUME_MOUNT_PATH = "/app/persistent"

# 数据和日志分别放在同一个 Volume 的不同目录中
DATA_DIR = f"{VOLUME_MOUNT_PATH}/data"
LOGS_DIR = f"{VOLUME_MOUNT_PATH}/logs"

# ---------- Secret ----------
# 使用已经创建好的 Secret
SECRET_NAME = "qwen2api-secret"

# ---------- 服务配置 ----------
WEB_PORT = 7860
HEALTH_URL = f"http://127.0.0.1:{WEB_PORT}/healthz"

# ---------- Qwen2API 运行配置 ----------
WORKERS = 1
BROWSER_POOL_SIZE = 1
LOG_LEVEL = "INFO"

# ---------- Modal 资源配置 ----------
CPU = 1.0
MEMORY = 2048

# ---------- 容器运行配置 ----------
TIMEOUT = 3600
SCALEDOWN_WINDOW = 300
STARTUP_TIMEOUT = 300

# ---------- 启动健康检查 ----------
STARTUP_CHECK_TIMEOUT = 240
STARTUP_CHECK_INTERVAL = 2
HEALTH_CHECK_REQUEST_TIMEOUT = 5


# ====================== Modal 初始化 ======================

# 创建 Modal 应用
app = modal.App(APP_NAME)


# ====================== Docker 镜像配置 ======================

# 使用项目官方 Docker Hub 镜像
# 项目原始镜像已经包含 Qwen2API 程序和浏览器依赖
# add_python 用于给镜像添加 Python，供 Modal 执行部署函数
image = modal.Image.from_registry(
    IMAGE,
    add_python="3.11",
)


# ====================== 持久化 Volume ======================

# 创建或引用一个持久化 Volume
# 数据目录和日志目录都保存在这个 Volume 中
volume = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ====================== Secret 配置 ======================

# 引用已经在 Modal 控制台创建的 Secret
# ADMIN_KEY 是项目管理后台必需的环境变量
qwen2api_secret = modal.Secret.from_name(
    SECRET_NAME,
    required_keys=["ADMIN_KEY"],
)


# ====================== 服务启动函数 ======================

@app.function(
    image=image,

    # CPU 和内存
    cpu=CPU,
    memory=MEMORY,

    # 单次容器最长运行时间
    timeout=TIMEOUT,

    # 没有请求后，等待多久关闭容器
    scaledown_window=SCALEDOWN_WINDOW,

    # 容器启动超时时间
    startup_timeout=STARTUP_TIMEOUT,

    # 将一个 Volume 挂载到统一目录
    volumes={
        VOLUME_MOUNT_PATH: volume,
    },

    # 注入 Secret 中的环境变量
    secrets=[
        qwen2api_secret,
    ],
)
@modal.web_server(
    port=WEB_PORT,
    startup_timeout=STARTUP_TIMEOUT,

    # 不额外要求 Modal 代理认证
    # 项目自身的 ADMIN_KEY 负责管理后台认证
    requires_proxy_auth=False,
)
def run_qwen2api():

    # ====================== 1. 检查 Secret ======================

    # 检查 ADMIN_KEY 是否成功注入
    admin_key = os.environ.get("ADMIN_KEY", "").strip()

    if not admin_key:
        raise RuntimeError(
            f"没有读取到 ADMIN_KEY，请检查 Modal Secret：{SECRET_NAME}"
        )

    print("Secret 读取成功：", SECRET_NAME)

    # 不打印 ADMIN_KEY，避免密钥出现在日志中


    # ====================== 2. 检查启动程序 ======================

    # 官方 Dockerfile 中的程序路径
    executable = "/usr/local/bin/qwen2api"

    if not os.path.isfile(executable):
        raise RuntimeError(
            f"没有找到 Qwen2API 启动程序：{executable}"
        )

    if not os.access(executable, os.X_OK):
        raise RuntimeError(
            f"Qwen2API 启动程序没有执行权限：{executable}"
        )


    # ====================== 3. 创建持久化目录 ======================

    # 数据和日志保存在同一个 Volume 中
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)

    print("持久化目录检查完成")
    print("数据目录：", DATA_DIR)
    print("日志目录：", LOGS_DIR)


    # ====================== 4. 设置环境变量 ======================

    # 继承 Secret 注入的全部环境变量
    # 因此 QWEN_ACCOUNT_1、QWEN_API_KEY_1 等变量也会保留
    env = os.environ.copy()

    # 设置项目运行参数
    env.update({
        # Web 服务端口
        "PORT": str(WEB_PORT),

        # Go 服务 Worker 数量
        "WORKERS": str(WORKERS),

        # 浏览器池大小
        "BROWSER_POOL_SIZE": str(BROWSER_POOL_SIZE),

        # 日志级别
        "LOG_LEVEL": LOG_LEVEL,

        # 项目数据目录
        "DATA_DIR": DATA_DIR,

        # 项目日志目录
        "LOGS_DIR": LOGS_DIR,
    })


    # ====================== 5. 打印启动信息 ======================

    print("")
    print("==========================================")
    print("          Qwen2API 正在启动")
    print("==========================================")
    print(f"应用名称：{APP_NAME}")
    print(f"Docker 镜像：{IMAGE}")
    print(f"服务端口：{WEB_PORT}")
    print(f"CPU：{CPU}")
    print(f"内存：{MEMORY} MB")
    print(f"Secret：{SECRET_NAME}")
    print(f"Volume：{VOLUME_NAME}")
    print(f"数据目录：{DATA_DIR}")
    print(f"日志目录：{LOGS_DIR}")
    print(f"Workers：{WORKERS}")
    print(f"浏览器池：{BROWSER_POOL_SIZE}")
    print("==========================================")
    print("")


    # ====================== 6. 启动 Qwen2API ======================

    # 使用官方程序启动服务
    # stdout 和 stderr 继承容器输出，方便在 Modal Logs 中查看日志
    process = subprocess.Popen(
        [executable],
        cwd="/app",
        env=env,
        stdout=None,
        stderr=None,
    )

    print("Qwen2API 进程已启动")
    print("进程 PID：", process.pid)


    # ====================== 7. 等待健康检查 ======================

    start_time = time.time()

    while time.time() - start_time < STARTUP_CHECK_TIMEOUT:

        # 检查程序是否提前退出
        return_code = process.poll()

        if return_code is not None:
            raise RuntimeError(
                f"Qwen2API 启动失败，进程退出代码：{return_code}"
            )

        try:
            # 请求项目官方健康检查接口
            request = urllib.request.Request(
                HEALTH_URL,
                method="GET",
            )

            with urllib.request.urlopen(
                request,
                timeout=HEALTH_CHECK_REQUEST_TIMEOUT,
            ) as response:

                if 200 <= response.status < 300:
                    print("")
                    print("==========================================")
                    print("       Qwen2API 健康检查通过")
                    print("==========================================")
                    print(f"健康检查地址：{HEALTH_URL}")
                    print(f"HTTP 状态码：{response.status}")
                    print("服务启动成功")
                    print("==========================================")
                    print("")

                    return

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ):
            # 服务尚未就绪时继续等待
            pass

        # 间隔一段时间后重新检查
        time.sleep(STARTUP_CHECK_INTERVAL)


    # ====================== 8. 启动超时处理 ======================

    # 超过规定时间仍未通过健康检查，终止部署
    raise RuntimeError(
        f"Qwen2API 启动超时："
        f"等待 {STARTUP_CHECK_TIMEOUT} 秒后，"
        f"{HEALTH_URL} 仍未通过健康检查"
    )
