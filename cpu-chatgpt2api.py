import os
import subprocess
import time
import modal

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "chatgpt2api"                              # Modal App 名称
VOLUME_NAME = "chatgpt2api-data"                      # 持久化数据卷名称

# 镜像配置（与官方 Compose 默认一致）
IMAGE = "ghcr.io/yukkcat/chatgpt2api:latest"
WEB_PORT = 80                                         # 官方 CMD 监听 80

MAX_CONTAINERS = 1                                    # 最多 1 个容器
MAX_CONCURRENT_INPUTS = 10                            # 单容器并发输入
TIMEOUT_SECONDS = 3600                                # 单次函数最长运行时间（秒）
SCALEDOWN_WINDOW_SECONDS = 300                        # 空闲后缩容等待
WEB_STARTUP_TIMEOUT_SECONDS = 180                     # Web 启动等待

# 资源（比 Vaultwarden 加大）
CPU = 0.5
MEMORY_MB = 1536

# Modal Secret 名称（必须提前创建）
SECRET_NAME = "chatgpt2api-secret"

# ---------- 环境变量说明（对齐官方） ----------
# CHATGPT2API_AUTH_KEY      ：必填，管理后台 & 默认 API Key
# CHATGPT2API_BASE_URL      ：可选，仅用于生成图片/文件对外 URL
# CHATGPT2API_THREAD_TOKENS ：可选，默认 120；自用可调低省内存
# DATABASE_URL              ：可选，不设则用 SQLite data/chatgpt2api.db
# ====================================================


# ====================================================
# 1. 持久化数据卷
# ====================================================
# 官方业务数据在 /app/data
# 不要删除这个 Volume，否则可能丢账号和数据库
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)


# ====================================================
# 2. 镜像
# ====================================================
image = modal.Image.from_registry(
    IMAGE,
    add_python="3.11",
)

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 3. 启动 chatgpt2api
# ====================================================
@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={"/app/data": vol},
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
    """启动 chatgpt2api Web 服务。"""
    print("======================================", flush=True)
    print("正在启动 chatgpt2api...", flush=True)
    print("======================================", flush=True)

    # ------------------------------------------------
    # 3.1 读取 Secret（只强制校验官方必填项）
    # ------------------------------------------------
    auth_key = os.environ.get("CHATGPT2API_AUTH_KEY", "").strip()
    base_url = os.environ.get("CHATGPT2API_BASE_URL", "").strip()
    thread_tokens = os.environ.get("CHATGPT2API_THREAD_TOKENS", "40").strip()

    if not auth_key:
        raise ValueError(
            "CHATGPT2API_AUTH_KEY 未设置。官方要求必填，请在 Modal Secret 中配置。"
        )

    # BASE_URL 可选：官方说明用于生成图片/文件 URL
    # 不填时使用当前服务地址，文本对话不受影响
    if base_url and not (
        base_url.startswith("http://") or base_url.startswith("https://")
    ):
        raise ValueError(
            "CHATGPT2API_BASE_URL 若填写，须是完整 http(s) 地址。"
        )

    # ------------------------------------------------
    # 3.2 组装环境变量（对齐官方 docker-compose）
    # ------------------------------------------------
    env = os.environ.copy()
    env["CHATGPT2API_AUTH_KEY"] = auth_key
    env["CHATGPT2API_THREAD_TOKENS"] = thread_tokens
    env["TZ"] = os.environ.get("TZ", "Asia/Shanghai")

    # 明确指向 Volume 内 SQLite（官方未设 DATABASE_URL 时默认 data/chatgpt2api.db）
    env["DATABASE_URL"] = "sqlite:////app/data/chatgpt2api.db"

    if base_url:
        env["CHATGPT2API_BASE_URL"] = base_url
    else:
        # 不传时让程序走官方默认：当前服务地址
        env.pop("CHATGPT2API_BASE_URL", None)

    # ------------------------------------------------
    # 3.3 启动信息
    # ------------------------------------------------
    print(f"AUTH_KEY：已设置", flush=True)
    print(
        f"BASE_URL：{base_url if base_url else '(未设置，使用当前服务地址)'}",
        flush=True,
    )
    print(f"THREAD_TOKENS：{thread_tokens}", flush=True)
    print(f"DATABASE_URL：{env['DATABASE_URL']}", flush=True)
    print(f"数据目录：/app/data", flush=True)
    print(f"监听端口：{WEB_PORT}", flush=True)
    print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)

    # ------------------------------------------------
    # 3.4 按官方 ENTRYPOINT + CMD 启动
    # ------------------------------------------------
    # ENTRYPOINT ["chatgpt2api-entrypoint"]
    # CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--access-log"]
    process = subprocess.Popen(
        [
            "chatgpt2api-entrypoint",
            "uv",
            "run",
            "uvicorn",
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(WEB_PORT),
            "--access-log",
        ],
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
            f"chatgpt2api 启动失败，退出代码：{return_code}"
        )

    print("进程仍在运行，等待 Web 服务就绪。", flush=True)
