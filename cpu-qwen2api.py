import os
import subprocess
import time
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
WEB_STARTUP_TIMEOUT_SECONDS = 180                    

CPU = 0.25                                           
MEMORY_MB = 512                                      

SECRET_NAME = "qwen2api-secret"                      
# ====================================================

# 1. 持久化数据卷
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# 2. 官方镜像环境（Alpine 补充 Python 支持）
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
    print("==================================================", flush=True)
    print("[LOG] 🚀 正在启动 Qwen2API (Modal Web Server 守护模式)...", flush=True)
    print("==================================================", flush=True)

    # 1. 校验核心 API_KEY
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        print("[ERROR] ❌ 致命错误：API_KEY 未设置！请检查 Modal Secret。", flush=True)
        raise ValueError("API_KEY 未设置。")
    print(f"[LOG] ✅ 成功从 Secret 加载 API_KEY", flush=True)

    # 2. 组装官方标准环境变量
    env = os.environ.copy()
    env["API_KEY"] = api_key
    env["LISTEN_ADDRESS"] = "0.0.0.0"
    env["SERVICE_PORT"] = str(WEB_PORT)
    env["DATA_SAVE_MODE"] = "file"
    env["CACHE_MODE"] = "file"
    env["OUTPUT_THINK"] = os.environ.get("OUTPUT_THINK", "true")
    env["SEARCH_INFO_MODE"] = os.environ.get("SEARCH_INFO_MODE", "table")

    # 3. 写入运行时配置文件
    env_file = "/app/.env"
    try:
        with open(env_file, "w", encoding="utf-8") as f:
            for k, v in env.items():
                if k in ["API_KEY", "LISTEN_ADDRESS", "SERVICE_PORT", "DATA_SAVE_MODE", "CACHE_MODE", "OUTPUT_THINK", "SEARCH_INFO_MODE"]:
                    f.write(f"{k}={v}\n")
        print(f"[LOG] ✅ 运行时配置文件写入成功", flush=True)
    except Exception as e:
        print(f"[ERROR] ❌ 写入配置文件失败: {e}", flush=True)
        raise e

    # 4. 启动核心二进制进程
    binary_path = "/usr/local/bin/qwen2api"
    print(f"[LOG] ⚙️ 正在拉起核心二进制进程...", flush=True)
    
    if not os.path.exists(binary_path):
        print(f"[ERROR] ❌ 致命错误：路径 {binary_path} 下未找到二进制文件！", flush=True)

    try:
        # 使用 stdout/stderr 实时把二进制日志输出到 Modal 控制台
        process = subprocess.Popen(
            [binary_path],
            env=env,
            cwd="/app",
            stdout=None,  # 直接输出到标准输出，方便在 Modal 后台看完整日志
            stderr=None,
        )
        print(f"[LOG] ✅ 二进制进程已成功拉起，PID：{process.pid}", flush=True)
    except Exception as e:
        print(f"[ERROR] ❌ 启动子进程异常: {e}", flush=True)
        raise e

    print("[LOG] 🎉 Web Server 已进入主循环阻塞等待状态...", flush=True)
    print("==================================================", flush=True)

    # 【核心修复】：让 Python 主线程阻塞在这里，等待子进程。
    # 只要子进程在跑，Modal 容器就会一直存活并对外提供服务，不会提前退出！
    return_code = process.wait()
    print(f"[LOG] ⚠️ qwen2api 子进程已退出，退出码: {return_code}", flush=True)
