import asyncio
from datetime import datetime
import json
import os
import subprocess
import time

import aiohttp
import modal
from modal.server import Server

MINUTES = 60  # 时间单位设置（秒）

# ==============================================================================
# 1. 镜像构建与环境配置
# ==============================================================================
vllm_image = (
    modal.Image.from_registry("nvidia/cuda:12.4.0-devel-ubuntu22.04", add_python="3.11")
    .uv_pip_install("vllm==0.11.2", "huggingface-hub==0.36.0")
    .env(
        {
            "VLLM_SERVER_DEV_MODE": "1",  # 开启 vLLM 开发者模式以支持 sleep/wake_up API
            "TORCH_CPP_LOG_LEVEL": "FATAL",
        }
    )
)

GPU = "T4" 
MODEL_NAME = "Qwen/Qwen3-8B-AWQ"

# 云端持久卷
vol = modal.Volume.from_name("qwen3-8b-awq-cache", create_if_missing=True)
HF_CACHE_PATH = "/root/.cache/huggingface"

vllm_image = vllm_image.env(
    {"HF_HUB_CACHE": HF_CACHE_PATH, "HF_XET_HIGH_PERFORMANCE": "1"}
)

# ==============================================================================
# 2. 部署区域与并发控制
# ==============================================================================
REGION = "us-east"      
MIN_CONTAINERS = 0      # 无请求且超过 scaledown_window 后自动缩容到 0
TARGET_INPUTS = 20      # 适应沉浸式翻译同时推送多条字幕的并发需求

with vllm_image.imports():
    import requests

PORT = 8000             

# ==============================================================================
# 3. 健康检查与内存快照休眠/唤醒函数
# ==============================================================================
def wait_ready(process: subprocess.Popen, timeout: int = 5 * MINUTES):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            check_running(process)
            requests.get(f"http://127.0.0.1:{PORT}/health").raise_for_status()
            return
        except (
            subprocess.CalledProcessError,
            requests.exceptions.ConnectionError,
            requests.exceptions.HTTPError,
        ):
            time.sleep(3)
    raise TimeoutError(f"vLLM server 在 {timeout} 秒内未能成功启动")

def check_running(p: subprocess.Popen):
    if (rc := p.poll()) is not None:
        raise subprocess.CalledProcessError(rc, cmd=p.args)

def warmup():
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": "Translate: Hello world"}],
        "max_tokens": 16,
    }
    for _ in range(2):
        requests.post(
            f"http://127.0.0.1:{PORT}/v1/chat/completions", json=payload, timeout=10
        ).raise_for_status()

def sleep(level: int = 1):
    requests.post(f"http://127.0.0.1:{PORT}/sleep?level={level}").raise_for_status()

def wake_up():
    requests.post(f"http://127.0.0.1:{PORT}/wake_up").raise_for_status()

# ==============================================================================
# 4. 主服务定义 (包含 Modal GPU Memory Snapshot、1分钟空闲关闭与启动时间日志)
# ==============================================================================
APP_NAME = "qwen3-8b-awq-immersive-translate"
app = modal.App(name=APP_NAME)

@app.server(
    image=vllm_image,
    gpu=GPU,
    volumes={HF_CACHE_PATH: vol},                      # 挂载云硬盘
    scaledown_window=1 * MINUTES,                      # 无请求后保持在线 1 分钟（60秒），超时关机
    enable_memory_snapshot=True,                       # 开启 Modal 内存快照功能
    experimental_options={"enable_gpu_snapshot": True}, # 开启 GPU 显存快照
    compute_region=REGION,
    min_containers=MIN_CONTAINERS,
    startup_timeout=10 * MINUTES,
    port=PORT,
    routing_region=REGION,
    exit_grace_period=5,
    target_concurrency=TARGET_INPUTS,
    unauthenticated=True,                               # 开放免鉴权
)
class QwenAWQServer:
    @modal.enter(snap=True)
    def startup(self):
        """【首次部署构建快照】启动 vLLM -> 加载/下载模型 -> 预热 -> 写入内存快照"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n==================================================")
        print(f"🚀 [首次部署启动] 时间: {now_str}")
        print(f"==================================================\n")

        cmd = [
            "vllm",
            "serve",
            MODEL_NAME,
            "--served-model-name", MODEL_NAME,
            "--quantization", "awq",                    
            "--dtype", "float16",                       
            "--gpu-memory-utilization", "0.85",        
            "--max-model-len", "8192",                  
            "--host", "0.0.0.0",
            "--port", f"{PORT}",
            "--enable-sleep-mode",                      
            "--disable-uvicorn-access-log",
            "--disable-log-requests",
        ]

        self.process = subprocess.Popen(cmd)
        wait_ready(self.process)
        warmup()
        sleep(1) # 休眠并保存显存/内存快照

    @modal.enter(snap=False)
    def restore(self):
        """【冷启动/唤醒】从快照秒级唤醒并打印时间日志"""
        start_time = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        print(f"\n==================================================")
        print(f"⚡ [实例唤醒启动] 当前时间: {now_str}")
        print(f"正在从 GPU 快照唤醒 vLLM 引擎...")
        
        wake_up()
        
        elapsed = time.time() - start_time
        print(f"✅ [唤醒完成] 耗时: {elapsed:.2f} 秒")
        print(f"==================================================\n")

    @modal.exit()
    def stop(self):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"🛑 [实例停止/休眠] 时间: {now_str}")
        self.process.terminate()

# ==============================================================================
# 5. 部署与测试入口
# ==============================================================================
if __name__ == "__main__":
    qwen_server = Server.from_name(APP_NAME, "QwenAWQServer")

    async def main():
        url = await qwen_server.get_url.aio()
        print(f"\n==================================================")
        print(f"沉浸式翻译 API Base URL: {url}/v1")
        print(f"模型名称 (Model Name): {MODEL_NAME}")
        print(f"空闲留存时间: 1 分钟 (scaledown_window=60s)")
        print(f"==================================================\n")

    asyncio.run(main())
