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

# 1. 持久化数据卷（用来保存网页后台添加的账号和配置）
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# 2. 镜像配置（官方 Alpine 镜像需要补充 Python 运行时支持）
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
    volumes={"/app/data": vol},                        # 挂载持久化目录
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
    print("[LOG] 🚀 正在初始化 Qwen2API (极简 KEY 模式)...", flush=True)
    print("==================================================", flush=True)

    # 校验 API_KEY
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        print("[ERROR] ❌ 致命错误：API_KEY 未设置！", flush=True)
        raise ValueError("API_KEY 未设置，请在 Modal Secret 中配置。")
    print(f"[LOG] ✅ 成功加载 API_KEY (长度: {len(api_key)} 位)", flush=True)

    # 组装运行时环境变量
    env = os.environ.copy()
    
    # 强制指定关键参数（即使只传了 KEY，也通过环境变量和参数补全标准行为）
    env["API_KEY"] = api_key
    env["HOST"] = "0.0.0.0"
    env["PORT"] = str(WEB_PORT)
    env["DATA_SAVE_MODE"] = "file"      # 开启文件持久化，配合 Modal Volume 保存后台数据
    env["CACHE_MODE"] = "file"          # 缓存落盘

    # 写入容器内的 .env 保证二进制能读取
    env_file = "/app/.env"
    try:
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(f"API_KEY={api_key}\n")
            f.write(f"HOST=0.0.0.0\n")
            f.write(f"PORT={WEB_PORT}\n")
            f.write(f"DATA_SAVE_MODE=file\n")
            f.write(f"CACHE_MODE=file\n")
        print(f"[LOG] ✅ 成功写入运行时配置文件 {env_file}", flush=True)
    except Exception as e:
        print(f"[ERROR] ❌ 写入配置文件失败: {e}", flush=True)
        raise e

    print("--------------------------------------------------", flush=True)
    print(f"[LOG] 📊 运行配置摘要：", flush=True)
    print(f"       - 绑定监听地址 : 0.0.0.0:{WEB_PORT}", flush=True)
    print(f"       - 数据保存模式 : file (挂载于 /app/data)", flush=True)
    print("--------------------------------------------------", flush=True)

    binary_path = "/usr/local/bin/qwen2api"
    print(f"[LOG] ⚙️ 正在拉起核心二进制，强制绑定 0.0.0.0...", flush=True)
    
    try:
        # 核心修复：通过命令行参数强制将 Host 指定为 0.0.0.0，避免默认监听 localhost 导致外部无法访问
        process = subprocess.Popen(
            [binary_path, "-H", "0.0.0.0", "-p", str(WEB_PORT)],
            env=env,
            cwd="/app",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        print(f"[LOG] ✅ 进程已成功拉起，PID：{process.pid}", flush=True)
    except Exception as e:
        print(f"[ERROR] ❌ 启动子进程发生异常: {e}", flush=True)
        raise e

    # 启动健康检查（监控前 3 秒是否有崩溃输出）
    start_wait = time.time()
    while time.time() - start_wait < 3:
        ret = process.poll()
        if ret is not None:
            output, _ = process.communicate()
            print(f"[ERROR] ❌ qwen2api 进程异常退出，退出码：{ret}", flush=True)
            print(f"[ERROR] 📄 崩溃日志输出:\n{output}", flush=True)
            raise RuntimeError(f"Qwen2API 启动失败，退出代码：{ret}")
        time.sleep(0.5)

    print("[LOG] 🎉 服务运行稳定，Modal Web 网关已成功代理端口 3000。", flush=True)
    print("==================================================", flush=True)
