import asyncio
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

# ------------------------------------------------------------------------------
# 【修改重点】变量名统一为 vol，云端持久卷名称改为 "qwen3-8b-awq-cache"
# ------------------------------------------------------------------------------
vol = modal.Volume.from_name("qwen3-8b-awq-cache", create_if_missing=True)
HF_CACHE_PATH = "/root/.cache/huggingface"

vllm_image = vllm_image.env(
    {"HF_HUB_CACHE": HF_CACHE_PATH, "HF_XET_HIGH_PERFORMANCE": "1"}
)

# ==============================================================================
# 2. 部署区域与并发控制
# ==============================================================================
REGION = "us-east"      
MIN_CONTAINERS = 0      # 无请求时 0 实例，不产生闲置计算费用
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
# 4. 主服务定义 (包含 Modal GPU Memory Snapshot)
# ==============================================================================
APP_NAME = "qwen3-8b-awq-immersive-translate"
app = modal.App(name=APP_NAME)

@app.server(
    image=vllm_image,
    gpu=GPU,
    volumes={HF_CACHE_PATH: vol},                      # 使用名字修改后的 vol 挂载云硬盘[cite: 1, 2]
    scaledown_window=1 * MINUTES,
    enable_memory_snapshot=True,                       # 开启 Modal 内存快照功能[cite: 1, 2]
    experimental_options={"enable_gpu_snapshot": True}, # 开启 GPU 显存快照，实现秒级冷启动[cite: 1, 2]
    compute_region=REGION,
    min_containers=MIN_CONTAINERS,
    startup_timeout=10 * MINUTES,
    port=PORT,
    routing_region=REGION,
    exit_grace_period=5,
    target_concurrency=TARGET_INPUTS,
    unauthenticated=True,                               # 开放免鉴权，便于插件直接连接
)
class QwenAWQServer:
    @modal.enter(snap=True)
    def startup(self):
        """【首次部署】启动 vLLM -> 加载/下载模型 -> 预热 -> 写入内存快照"""
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
        sleep(1) # 休眠并保存显存/内存快照[cite: 1, 2]

    @modal.enter(snap=False)
    def restore(self):
        """【后续看视频触发冷启动】跳过重新加载，直接从快照 2~4 秒内瞬间唤醒"""
        wake_up()

    @modal.exit()
    def stop(self):
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
        print(f"持久卷名称: qwen3-8b-awq-cache")
        print(f"==================================================\n")

    asyncio.run(main())
