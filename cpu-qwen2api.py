import os
import subprocess
import modal

# ====================== 配置区 ======================
APP_NAME = "qwen2api"
VOLUME_NAME = "qwen2api-data"

IMAGE = "rfym21/qwen2api:latest"
WEB_PORT = 3000

MAX_CONTAINERS = 1
MAX_CONCURRENT_INPUTS = 8
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 300
WEB_STARTUP_TIMEOUT_SECONDS = 300   # 建议加大

CPU = 0.5          # 建议提高
MEMORY_MB = 1024   # 建议提高

SECRET_NAME = "qwen2api-secret"
# ====================================================

vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

image = (
    modal.Image.from_registry(IMAGE)
    .dockerfile_commands(
        "RUN apk add --no-cache python3 py3-pip",
        "RUN ln -sf /usr/bin/python3 /usr/bin/python",
    )
)

app = modal.App(name=APP_NAME, image=image)


@app.function(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={"/app/data": vol},
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    secrets=[modal.Secret.from_name(SECRET_NAME)],
)
@modal.concurrent(max_inputs=MAX_CONCURRENT_INPUTS)
@modal.web_server(
    WEB_PORT,
    startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
)
def ui():
    print("==================================================", flush=True)
    print("[LOG] 🚀 正在启动 Qwen2API (Modal Web Server)...", flush=True)
    print("==================================================", flush=True)

    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        print("[ERROR] ❌ API_KEY 未设置！请检查 Modal Secret。", flush=True)
        raise ValueError("API_KEY 未设置。")
    print("[LOG] ✅ 成功从 Secret 加载 API_KEY", flush=True)

    env = os.environ.copy()
    env["API_KEY"] = api_key
    env["LISTEN_ADDRESS"] = "0.0.0.0"
    env["SERVICE_PORT"] = str(WEB_PORT)
    env["DATA_SAVE_MODE"] = "file"
    env["CACHE_MODE"] = "file"
    env["OUTPUT_THINK"] = os.environ.get("OUTPUT_THINK", "true")
    env["SEARCH_INFO_MODE"] = os.environ.get("SEARCH_INFO_MODE", "table")

    # 写入 .env（可选，二进制也会读环境变量）
    env_file = "/app/.env"
    with open(env_file, "w", encoding="utf-8") as f:
        for k in ["API_KEY", "LISTEN_ADDRESS", "SERVICE_PORT", "DATA_SAVE_MODE",
                  "CACHE_MODE", "OUTPUT_THINK", "SEARCH_INFO_MODE"]:
            if k in env:
                f.write(f"{k}={env[k]}\n")
    print("[LOG] ✅ 运行时配置文件写入成功", flush=True)

    binary_path = "/usr/local/bin/qwen2api"
    if not os.path.exists(binary_path):
        raise FileNotFoundError(f"未找到二进制: {binary_path}")

    print("[LOG] ⚙️ 正在拉起核心二进制进程...", flush=True)
    # ★ 关键：只 Popen，不要 wait！
    process = subprocess.Popen(
        [binary_path],
        env=env,
        cwd="/app",
        stdout=None,
        stderr=None,
    )
    print(f"[LOG] ✅ 二进制进程已拉起，PID：{process.pid}", flush=True)
    print("[LOG] 🎉 Web Server 启动完成，等待 Modal 接管端口...", flush=True)
    # 函数到这里直接返回，Modal 会检测 3000 端口并保持容器存活
