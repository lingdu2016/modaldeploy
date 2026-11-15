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

# 1. 持久化数据卷（挂载到 /app/data 用于保存账号数据）
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
        modal.Secret.from_name(SECRET_NAME), # 自动注入你在 Modal 后台配置的所有环境变量
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
    print("[LOG] 🚀 正在启动 Qwen2API (Modal Secret 环境变量模式)...", flush=True)
    print("==================================================", flush=True)

    # 1. 校验核心 API_KEY
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        print("[ERROR] ❌ 致命错误：API_KEY 未设置！请检查 Modal Secret。", flush=True)
        raise ValueError("API_KEY 未设置。")
    print(f"[LOG] ✅ 成功从 Secret 加载 API_KEY (长度: {len(api_key)} 位)", flush=True)

    # 2. 读取 Modal Secret 中注入的其他环境变量，并双重保障写入 /app/.env
    env = os.environ.copy()
    
    # 确保关键变量在运行时强制生效
    env["LISTEN_ADDRESS"] = os.environ.get("LISTEN_ADDRESS", "0.0.0.0")
    env["SERVICE_PORT"] = os.environ.get("SERVICE_PORT", str(WEB_PORT))
    env["DATA_SAVE_MODE"] = os.environ.get("DATA_SAVE_MODE", "file")
    env["CACHE_MODE"] = os.environ.get("CACHE_MODE", "file")

    env_file = "/app/.env"
    print(f"[LOG] 📝 正在生成运行时配置文件 {env_file}...", flush=True)
    try:
        with open(env_file, "w", encoding="utf-8") as f:
            for k, v in env.items():
                # 仅把相关配置写入 .env 文件
                if k in ["API_KEY", "LISTEN_ADDRESS", "SERVICE_PORT", "DATA_SAVE_MODE", "CACHE_MODE", "OUTPUT_THINK", "SEARCH_INFO_MODE"]:
                    f.write(f"{k}={v}\n")
        print(f"[LOG] ✅ 配置文件写入成功", flush=True)
    except Exception as e:
        print(f"[ERROR] ❌ 写入配置文件失败: {e}", flush=True)
        raise e

    print("--------------------------------------------------", flush=True)
    print(f"[LOG] 📊 当前生效配置：", flush=True)
    print(f"       - 监听地址 (LISTEN_ADDRESS) : {env['LISTEN_ADDRESS']}", flush=True)
    print(f"       - 服务端口 (SERVICE_PORT)   : {env['SERVICE_PORT']}", flush=True)
    print(f"       - 数据保存模式 (DATA_SAVE)  : {env['DATA_SAVE_MODE']}", flush=True)
    print(f"       - 缓存模式 (CACHE_MODE)     : {env['CACHE_MODE']}", flush=True)
    print("--------------------------------------------------", flush=True)

    # 3. 启动核心二进制进程
    binary_path = "/usr/local/bin/qwen2api"
    print(f"[LOG] ⚙️ 正在拉起核心二进制...", flush=True)
    
    if not os.path.exists(binary_path):
        print(f"[ERROR] ❌ 致命错误：路径 {binary_path} 下未找到二进制文件！", flush=True)

    try:
        process = subprocess.Popen(
            [binary_path],
            env=env,
            cwd="/app",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        print(f"[LOG] ✅ 进程已成功拉起，PID：{process.pid}", flush=True)
    except Exception as e:
        print(f"[ERROR] ❌ 启动子进程异常: {e}", flush=True)
        raise e

    # 4. 健康检查（监控前 3 秒）
    start_wait = time.time()
    while time.time() - start_wait < 3:
        ret = process.poll()
        if ret is not None:
            output, _ = process.communicate()
            print(f"[ERROR] ❌ 进程异常退出，退出码：{ret}", flush=True)
            print(f"[ERROR] 📄 崩溃日志输出:\n{output}", flush=True)
            raise RuntimeError(f"Qwen2API 启动失败，退出代码：{ret}")
        time.sleep(0.5)

    print("[LOG] 🎉 服务运行稳定，Modal 网关已成功接管端口 3000。", flush=True)
    print("==================================================", flush=True)
