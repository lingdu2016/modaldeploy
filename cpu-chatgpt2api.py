
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
# 对应官方仓库当前 main 分支的提交版本
IMAGE = "yujunzhixue/qwen2api:sha-799e975"

# ---------- 持久化存储 ----------
# 只使用一个 Volume
VOLUME_NAME = "qwen2api-data"

# Volume 挂载到项目默认数据目录
VOLUME_MOUNT_PATH = "/app/data"

# 数据和日志共用一个 Volume
DATA_DIR = "/app/data"
LOGS_DIR = "/app/data/logs"

# ---------- Secret ----------
# 使用你已经创建的 Secret
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

# 限制最多一个容器，避免多个实例同时修改同一个数据 Volume
MAX_CONTAINERS = 1

# ---------- 容器运行配置 ----------
TIMEOUT = 3600
SCALEDOWN_WINDOW = 300
STARTUP_TIMEOUT = 300

# ---------- 健康检查 ----------
STARTUP_CHECK_TIMEOUT = 240
STARTUP_CHECK_INTERVAL = 2
HEALTH_CHECK_REQUEST_TIMEOUT = 5


# ====================== Modal 初始化 ======================

app = modal.App(APP_NAME)


# ====================== Docker 镜像配置 ======================

# 使用项目官方 Docker Hub 镜像
#
# 官方镜像基于 Debian Bookworm slim
# 已经包含：
# 1. Qwen2API Go 后端程序
# 2. React 前端构建文件
# 3. 浏览器运行依赖
# 4. Playwright 浏览器
#
# add_python 仅为 Modal 提供 Python 运行环境
# 不会替换官方 Go 程序
image = modal.Image.from_registry(
    IMAGE,
    add_python="3.11",
)


# ====================== 持久化 Volume ======================

# 创建或引用一个 Modal Volume
#
# Volume 挂载到 /app/data
#
# 目录结构：
# /app/data/
# ├── accounts.json
# ├── api_keys.json
# └── logs/
#
# 实际文件以程序运行后生成的内容为准
volume = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ====================== Secret 配置 ======================

# 引用已经创建的 qwen2api-secret
#
# 官方项目必需的环境变量：
# ADMIN_KEY
#
# 不设置 API_KEY
# 不将 ADMIN_KEY 改名为 API_KEY
qwen2api_secret = modal.Secret.from_name(
    SECRET_NAME,
    required_keys=["ADMIN_KEY"],
)


# ====================== 服务启动 ======================

@app.function(
    image=image,

    # ---------- CPU 和内存 ----------
    cpu=CPU,
    memory=MEMORY,

    # ---------- 容器运行时间 ----------
    timeout=TIMEOUT,
    scaledown_window=SCALEDOWN_WINDOW,
    startup_timeout=STARTUP_TIMEOUT,

    # ---------- 容器数量 ----------
    max_containers=MAX_CONTAINERS,

    # ---------- 持久化目录 ----------
    volumes={
        VOLUME_MOUNT_PATH: volume,
    },

    # ---------- 环境变量 ----------
    secrets=[
        qwen2api_secret,
    ],
)
@modal.web_server(
    port=WEB_PORT,
    startup_timeout=STARTUP_TIMEOUT,
    requires_proxy_auth=False,
)
def run_qwen2api():

    # ====================== 1. 检查环境变量 ======================

    # 从 qwen2api-secret 中读取 ADMIN_KEY
    admin_key = os.environ.get("ADMIN_KEY", "").strip()

    if not admin_key:
        raise RuntimeError(
            f"缺少 ADMIN_KEY，请检查 Modal Secret：{SECRET_NAME}"
        )

    print("ADMIN_KEY 环境变量检查通过")

    # 不打印密钥，避免敏感信息进入日志


    # ====================== 2. 检查官方启动程序 ======================

    # 官方 Dockerfile 指定的启动程序
    executable = "/usr/local/bin/qwen2api"

    if not os.path.isfile(executable):
        raise RuntimeError(
            f"找不到官方启动程序：{executable}"
        )

    if not os.access(executable, os.X_OK):
        raise RuntimeError(
            f"启动程序没有执行权限：{executable}"
        )

    print("官方启动程序检查通过")


    # ====================== 3. 创建数据和日志目录 ======================

    # 数据目录位于 Volume 挂载位置
    os.makedirs(DATA_DIR, exist_ok=True)

    # 日志目录放在同一个 Volume 内
    os.makedirs(LOGS_DIR, exist_ok=True)

    print("持久化目录检查通过")
    print(f"数据目录：{DATA_DIR}")
    print(f"日志目录：{LOGS_DIR}")


    # ====================== 4. 设置项目环境变量 ======================

    # 继承 Secret 注入的环境变量
    # QWEN_ACCOUNT_N、QWEN_API_KEY 等可选变量也会保留
    env = os.environ.copy()

    # 设置官方项目支持的运行参数
    env.update({
        # Web 服务端口
        "PORT": str(WEB_PORT),

        # 后端 Worker 数量
        "WORKERS": str(WORKERS),

        # 浏览器池大小
        "BROWSER_POOL_SIZE": str(BROWSER_POOL_SIZE),

        # 日志等级
        "LOG_LEVEL": LOG_LEVEL,

        # 项目数据目录
        "DATA_DIR": DATA_DIR,

        # 项目日志目录
        "LOGS_DIR": LOGS_DIR,
    })


    # ====================== 5. 输出启动信息 ======================

    print("")
    print("==========================================")
    print("             Qwen2API 启动")
    print("==========================================")
    print(f"应用名称：{APP_NAME}")
    print(f"官方镜像：{IMAGE}")
    print(f"服务端口：{WEB_PORT}")
    print(f"CPU：{CPU}")
    print(f"内存：{MEMORY} MB")
    print(f"最大容器数：{MAX_CONTAINERS}")
    print(f"Secret：{SECRET_NAME}")
    print(f"Volume：{VOLUME_NAME}")
    print(f"数据目录：{DATA_DIR}")
    print(f"日志目录：{LOGS_DIR}")
    print(f"Workers：{WORKERS}")
    print(f"浏览器池：{BROWSER_POOL_SIZE}")
    print("==========================================")
    print("")


    # ====================== 6. 启动 Go 后端 ======================

    # 直接启动官方 Dockerfile 中的 Go 程序
    #
    # stdout 和 stderr 继承容器输出
    # 可以直接在 Modal Logs 中查看项目日志
    process = subprocess.Popen(
        [executable],
        cwd="/app",
        env=env,
        stdout=None,
        stderr=None,
    )

    print("Qwen2API 进程已经启动")
    print(f"进程 PID：{process.pid}")


    # ====================== 7. 等待健康检查 ======================

    start_time = time.time()

    while time.time() - start_time < STARTUP_CHECK_TIMEOUT:

        # 检查 Go 服务是否意外退出
        return_code = process.poll()

        if return_code is not None:
            raise RuntimeError(
                f"Qwen2API 启动失败，"
                f"进程退出代码：{return_code}"
            )

        try:
            # 请求官方健康检查接口
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
                    print("          Qwen2API 启动成功")
                    print("==========================================")
                    print(f"健康检查：{HEALTH_URL}")
                    print(f"HTTP 状态码：{response.status}")
                    print("服务已经就绪")
                    print("==========================================")
                    print("")

                    return

        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ):
            # 服务尚未就绪，继续等待
            pass

        time.sleep(STARTUP_CHECK_INTERVAL)


    # ====================== 8. 启动超时处理 ======================

    # 超时后终止启动流程
    raise RuntimeError(
        f"Qwen2API 启动超时，"
        f"等待 {STARTUP_CHECK_TIMEOUT} 秒后，"
        f"健康检查仍未通过：{HEALTH_URL}"
    )
