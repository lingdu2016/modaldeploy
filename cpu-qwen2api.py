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
MAX_CONCURRENT_INPUTS = 8                            # 单容器并发输入
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
# LISTEN_ADDRESS        代码里强制写 0.0.0.0，必须           监听地址（容器内必须是 0.0.0.0，否则 Modal 访问不到）
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
# 官方镜像是 Alpine（musl），本身没有 Python
# Modal Function 必须能检测到 Python，所以用 apk 安装
# （Vaultwarden 那套 add_python 对 Alpine 经常失效）
image = (
    modal.Image.from_registry(IMAGE)
    .dockerfile_commands(
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
    )
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
    # 3.2 组装环境变量 + 写入 .env
    # ------------------------------------------------
    # 官方二进制会读当前目录的 .env，文件优先级往往高于进程环境变量
    # 所以这里强制写一份，避免被默认的 localhost 覆盖
    env = os.environ.copy()

    listen_address = "0.0.0.0"                        # 容器内必须 0.0.0.0
    service_port = str(WEB_PORT)
    # 先用 none 排查监听问题；确认界面能开后再改回 file
    data_save_mode = os.environ.get("DATA_SAVE_MODE", "none").strip() or "none"

    env["API_KEY"] = api_key
    env["LISTEN_ADDRESS"] = listen_address
    env["SERVICE_PORT"] = service_port
    env["DATA_SAVE_MODE"] = data_save_mode

    env_file = "/app/.env"
    with open(env_file, "w", encoding="utf-8") as f:
        f.write(f"API_KEY={api_key}\n")
        f.write(f"LISTEN_ADDRESS={listen_address}\n")
        f.write(f"SERVICE_PORT={service_port}\n")
        f.write(f"DATA_SAVE_MODE={data_save_mode}\n")

    # ------------------------------------------------
    # 3.3 启动信息
    # ------------------------------------------------
    print(f"已写入 {env_file}", flush=True)
    print(f"API_KEY：已设置（第一个为管理员密钥）", flush=True)
    print(f"LISTEN_ADDRESS：{listen_address}", flush=True)
    print(f"SERVICE_PORT：{service_port}", flush=True)
    print(f"DATA_SAVE_MODE：{data_save_mode}", flush=True)
    print(f"数据目录：/app/data", flush=True)
    print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)
    print("其余配置（账号、思考开关等）可在部署后登录管理面板填写", flush=True)

    # ------------------------------------------------
    # 3.4 按官方镜像入口启动
    # ------------------------------------------------
    # cwd=/app，确保进程读到上面写的 /app/.env
    process = subprocess.Popen(
        ["/usr/local/bin/qwen2api"],
        env=env,
        cwd="/app",
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
