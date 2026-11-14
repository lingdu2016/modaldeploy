import os
import subprocess
import time
import modal

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "qwen2api"                                 # Modal App 名称
VOLUME_NAME = "qwen2api-data"                         # 持久化数据卷名称

# 镜像配置（官方 Docker Hub 镜像，Alpine 单静态二进制，约 46MB）
IMAGE = "rfym21/qwen2api:latest"
WEB_PORT = 3000                                       # 官方默认 SERVICE_PORT=3000

MAX_CONTAINERS = 1                                    # 自用最多 1 个容器
MAX_CONCURRENT_INPUTS = 10                            # 单容器并发输入
TIMEOUT_SECONDS = 3600                                # 单次函数最长运行时间（秒）
SCALEDOWN_WINDOW_SECONDS = 300                        # 空闲后缩容等待
WEB_STARTUP_TIMEOUT_SECONDS = 180                     # Web 启动等待

# 资源（官方镜像很轻，自用可低配）
CPU = 0.25                                            # 0.25 核足够
MEMORY_MB = 512                                       # 512MB 足够，紧张再提到 768

# Modal Secret 名称（必须提前创建）
SECRET_NAME = "qwen2api-secret"

# ---------- 环境变量说明（严格按官方行为） ----------
# 名称                  是否必须写进环境变量/Secret          作用
# ---------------------------------------------------------------------------
# API_KEY               【唯一强制必填】                     访问密钥。第一个=管理员（可登录管理面板），后面的=普通（只能调 API）
# DATA_SAVE_MODE        建议填写（推荐 file）               数据持久化方式：none=不存 / file=存到 /app/data / redis=用 Redis
# ACCOUNTS              可不写（部署后进面板添加）           账号。格式：email:password 或 email:password|proxy_url，多个用逗号
# OUTPUT_THINK          可不写（面板可改）                  是否输出思考过程 true/false
# SEARCH_INFO_MODE      可不写（面板可改）                  搜索结果展示 table / text
# CACHE_MODE            可不写（面板可改）                  图片缓存 default=内存 / file=落盘
# SERVICE_PORT          代码里已写死 3000，一般不用再写      服务端口
# LISTEN_ADDRESS        代码里已写死 0.0.0.0，一般不用再写   监听地址（容器内必须是 0.0.0.0）
# ====================================================


# ====================================================
# 1. 持久化数据卷
# ====================================================
# 官方 Docker Compose 把数据挂到 /app/data
# DATA_SAVE_MODE=file 时，账号和配置会写到这里
# 不要随便删这个 Volume，否则可能丢账号
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# ====================================================
# 2. 镜像
# ====================================================
# 官方镜像本身没有 Python，Modal 的 Function 必须依赖 Python
# 用 add_python 注入，这样既能跑官方二进制，又能让 Modal 正常工作
image = modal.Image.from_registry(
    IMAGE,
    add_python="3.12",
)

app = modal.App(
    name=APP_NAME,
    image=image,
)

# ====================================================
# 3. 启动 qwen2api
# ====================================================
@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={"/app/data": vol},                       # 官方数据目录
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
    """启动 Qwen2API（官方二进制入口）。"""
    print("======================================", flush=True)
    print("正在启动 Qwen2API...", flush=True)
    print("======================================", flush=True)

    # ------------------------------------------------
    # 3.1 只强制校验官方真正必填的项
    # ------------------------------------------------
    api_key = os.environ.get("API_KEY", "").strip()

    if not api_key:
        raise ValueError(
            "API_KEY 未设置。"
            "这是官方唯一强制必填项，请在 Modal Secret 中配置。"
            "第一个密钥为管理员，可登录管理面板。"
        )

    # ------------------------------------------------
    # 3.2 组装环境变量（尽量少写）
    # ------------------------------------------------
    env = os.environ.copy()

    # 必填
    env["API_KEY"] = api_key

    # 服务配置（容器内固定写法）
    env["SERVICE_PORT"] = str(WEB_PORT)
    env["LISTEN_ADDRESS"] = "0.0.0.0"

    # 推荐用 file，账号等会落到挂载的 /app/data
    # 不写的话官方默认是 none（重启就丢）
    env["DATA_SAVE_MODE"] = os.environ.get("DATA_SAVE_MODE", "file")

    # ------------------------------------------------
    # 3.3 启动信息
    # ------------------------------------------------
    print(f"API_KEY：已设置（第一个为管理员密钥）", flush=True)
    print(f"DATA_SAVE_MODE：{env['DATA_SAVE_MODE']}", flush=True)
    print(f"数据目录：/app/data", flush=True)
    print(f"监听端口：{WEB_PORT}", flush=True)
    print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)
    print("其余配置（账号、思考开关等）可在部署后登录管理面板填写", flush=True)

    # ------------------------------------------------
    # 3.4 按官方镜像入口启动
    # ------------------------------------------------
    # 官方 Docker 镜像 ENTRYPOINT 为 /usr/local/bin/qwen2api
    process = subprocess.Popen(
        ["/usr/local/bin/qwen2api"],
        env=env,
    )

    print(f"进程已启动，PID：{process.pid}", flush=True)

    # ------------------------------------------------
    # 3.5 检查是否立即退出
    # ------------------------------------------------
    time.sleep(3)
    return_code = process.poll()
    if return_code is not None:
        raise RuntimeError(
            f"Qwen2API 启动失败，退出代码：{return_code}"
        )

    print("进程仍在运行，等待 Web 服务就绪。", flush=True)
