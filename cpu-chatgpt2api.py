import os
import subprocess
import time
import modal

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "chatgpt2api"
VOLUME_NAME = "chatgpt2api-data"

# 建议固定版本，避免 :latest 变了导致镜像层重建
# 可到 https://github.com/yukkcat/chatgpt2api/pkgs/container/chatgpt2api 看最新 tag
IMAGE = "ghcr.io/yukkcat/chatgpt2api:latest"   # 或改成你需要的 tag；不要用 latest 更稳
WEB_PORT = 80

MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 10
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 300
WEB_STARTUP_TIMEOUT_SECONDS = 180

CPU = 0.5
MEMORY_MB = 1536

SECRET_NAME = "chatgpt2api-secret"
# ====================================================


# ====================================================
# 1. 持久化数据卷（只存业务数据，不存运行时代码）
# ====================================================
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# ====================================================
# 2. 镜像：官方镜像 + 构建期预热 /app（真正的镜像缓存）
# ====================================================
# 构建时做一次和 entrypoint 相同的 seed + uv sync，
# 结果写进镜像层；之后冷启动直接用缓存，不再每次装 32 个包。
image = (
    modal.Image.from_registry(IMAGE)
    # 避免官方 ENTRYPOINT 干扰 Modal 自己的 Python 启动
    .entrypoint([])
    .run_commands(
        # 与官方 docker-entrypoint.sh 对齐：把种子拷到 /app 并同步依赖
        "set -eu; "
        "seed_root=/opt/chatgpt2api; "
        "runtime_root=/app; "
        "seed_version=$(tr -d '\\r\\n' < \"${seed_root}/VERSION\"); "
        "mkdir -p \"${runtime_root}\"; "
        # 清空 /app 里除 data 外的内容（构建时一般没有 data）
        "find \"${runtime_root}\" -mindepth 1 -maxdepth 1 "
        "! -name data -exec rm -rf -- {} + 2>/dev/null || true; "
        # 拷贝种子（跳过 .venv，依赖后面 uv sync 生成）
        "for source in \"${seed_root}\"/* \"${seed_root}\"/.[!.]* \"${seed_root}\"/..?*; do "
        "  [ -e \"$source\" ] || continue; "
        "  [ \"$(basename \"$source\")\" = \".venv\" ] && continue; "
        "  cp -a \"$source\" \"${runtime_root}/\"; "
        "done; "
        "printf '%s\\n' \"${seed_version}\" > \"${runtime_root}/.chatgpt2api-image-version\"; "
        "cd \"${runtime_root}\"; "
        "uv sync --frozen --no-dev --no-install-project; "
        # 把项目本身也装进 venv，避免运行时 uv run 再 Building/Installed
        "uv pip install --no-deps -e . || uv pip install --no-deps .; "
        "echo \"[modal-image] /app prewarm done, version=${seed_version}\"",
    )
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

    auth_key = os.environ.get("CHATGPT2API_AUTH_KEY", "").strip()
    base_url = os.environ.get("CHATGPT2API_BASE_URL", "").strip()
    thread_tokens = os.environ.get("CHATGPT2API_THREAD_TOKENS", "40").strip()

    if not auth_key:
        raise ValueError(
            "CHATGPT2API_AUTH_KEY 未设置。官方要求必填，请在 Modal Secret 中配置。"
        )

    if base_url and not (
        base_url.startswith("http://") or base_url.startswith("https://")
    ):
        raise ValueError(
            "CHATGPT2API_BASE_URL 若填写，须是完整 http(s) 地址。"
        )

    env = os.environ.copy()
    env["CHATGPT2API_AUTH_KEY"] = auth_key
    env["CHATGPT2API_THREAD_TOKENS"] = thread_tokens
    env["TZ"] = os.environ.get("TZ", "Asia/Shanghai")
    env["DATABASE_URL"] = "sqlite:////app/data/chatgpt2api.db"

    if base_url:
        env["CHATGPT2API_BASE_URL"] = base_url
    else:
        env.pop("CHATGPT2API_BASE_URL", None)

    print("AUTH_KEY：已设置", flush=True)
    print(
        f"BASE_URL：{base_url if base_url else '(未设置，使用当前服务地址)'}",
        flush=True,
    )
    print(f"THREAD_TOKENS：{thread_tokens}", flush=True)
    print(f"DATABASE_URL：{env['DATABASE_URL']}", flush=True)
    print("数据目录：/app/data", flush=True)
    print(f"监听端口：{WEB_PORT}", flush=True)
    print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)

    # 优先用镜像里已预热的 venv，避免 uv run 再次装包/构建
    uvicorn_bin = "/app/.venv/bin/uvicorn"
    if os.path.isfile(uvicorn_bin):
        cmd = [
            uvicorn_bin,
            "main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(WEB_PORT),
            "--access-log",
        ]
        cwd = "/app"
        print("启动方式：预热 venv 直接 uvicorn（跳过运行时 uv sync）", flush=True)
    else:
        # 回退：走官方 entrypoint（仍会 uv sync，但镜像已预热时通常很快）
        cmd = [
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
        ]
        cwd = None
        print("启动方式：官方 entrypoint（回退）", flush=True)

    process = subprocess.Popen(cmd, env=env, cwd=cwd)

    print(f"进程已启动，PID：{process.pid}", flush=True)

    time.sleep(3)
    return_code = process.poll()
    if return_code is not None:
        raise RuntimeError(
            f"chatgpt2api 启动失败，退出代码：{return_code}"
        )

    print("进程仍在运行，等待 Web 服务就绪。", flush=True)
