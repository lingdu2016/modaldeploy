import asyncio
import os
import subprocess
import time

import modal
from modal import App, Image, Volume, Server

MINUTES = 60

########## CONSTANTS ##########

MODEL_NAME = "Qwen/Qwen3-8B-AWQ"
SERVE_MODEL_NAME = "qwen3-8b-awq"
MODEL_DIR = f"/models/{MODEL_NAME}"
GPU = "T4"
PORT = 23333
REGION = "us-east"

# 模型持久化缓存 Volume
model_volume = Volume.from_name(f"lmdeploy-model-cache-{SERVE_MODEL_NAME}", create_if_missing=True)

########## IMAGE DEFINITION ##########

image = (
    Image.from_registry("openmmlab/lmdeploy:v0.7.3-cu12")
    .pip_install(
        "huggingface_hub[hf_transfer,hf_xet]>=0.30.0,<1.0",
        "transformers>=4.51.0",
        "grpclib==0.4.7",
        "requests",
    )
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)

with image.imports():
    import requests

########## HEALTH & SNAPSHOT UTILS ##########

def check_running(p: subprocess.Popen):
    if (rc := p.poll()) is not None:
        raise subprocess.CalledProcessError(rc, cmd=p.args)

def wait_ready(process: subprocess.Popen, timeout: int = 5 * MINUTES):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            check_running(process)
            requests.get(f"http://127.0.0.1:{PORT}/v1/models", timeout=2).raise_for_status()
            return
        except (subprocess.CalledProcessError, requests.exceptions.RequestException):
            time.sleep(2)
    raise TimeoutError(f"LMDeploy server 在 {timeout} 秒内未能成功启动")

def warmup():
    payload = {
        "model": SERVE_MODEL_NAME,
        "messages": [{"role": "user", "content": "Translate: Hello world"}],
        "max_tokens": 16,
    }
    for _ in range(2):
        try:
            requests.post(
                f"http://127.0.0.1:{PORT}/v1/chat/completions",
                json=payload,
                timeout=10
            ).raise_for_status()
        except Exception as e:
            print(f"Warmup warning: {e}", flush=True)

def sleep(level: int = 1):
    try:
        requests.post(f"http://127.0.0.1:{PORT}/sleep?level={level}", timeout=10)
    except Exception:
        pass

def wake_up():
    try:
        requests.post(f"http://127.0.0.1:{PORT}/wake_up", timeout=10)
    except Exception:
        pass

def download_hf_model(model_dir: str, model_name: str):
    from huggingface_hub import snapshot_download
    os.makedirs(model_dir, exist_ok=True)
    snapshot_download(repo_id=model_name, local_dir=model_dir)

########## APP SETUP ##########

APP_NAME = f"lmdeploy-{SERVE_MODEL_NAME}-snapshot"
app = App(name=APP_NAME)

@app.server(
    image=image,
    gpu=GPU,
    cpu=2.0,
    memory=16384,
    volumes={"/models": model_volume},
    scaledown_window=1 * MINUTES,                         # 4分钟无请求自动销毁容器
    enable_memory_snapshot=True,                          # 开启内存快照
    experimental_options={"enable_gpu_snapshot": True},    # 开启 T4 GPU 显存快照 (2-4s 瞬间唤醒)
    compute_region=REGION,
    min_containers=0,                                     # 闲置时 0 费用
    startup_timeout=10 * MINUTES,
    port=PORT,
    routing_region=REGION,
    target_concurrency=16,                                # 单容器最大并发支持
    unauthenticated=True,
)
class LMDeployAWQServer:
    @modal.enter(snap=True)
    def startup(self):
        """【首次部署】初始化模型 -> 加载 T4 最优启动参数 -> 预热 -> 写入 GPU 快照"""
        config_file = os.path.join(MODEL_DIR, "config.json")
        if not os.path.exists(config_file):
            print("⚠️ Volume 中未找到模型权重，触发极速下载...", flush=True)
            download_hf_model(MODEL_DIR, MODEL_NAME)
            model_volume.commit()
            print("💾 模型已成功写入并提交到 Volume！", flush=True)
        else:
            print("✅ 找到已有模型权重，直接加载启动。", flush=True)

        # 针对 T4 显卡 + 翻译场景的最优优化参数
        args = [
            "lmdeploy serve api_server",
            f"--model-name {SERVE_MODEL_NAME}",
            f"--server-name 0.0.0.0",
            f"--server-port {PORT}",
            "--backend turbomind",
            "--model-format awq",
            "--tp 1",
            "--session-len 2048",
            "--cache-max-entry-count 0.25",
            "--max-batch-size 16",
            "--disable-cuda-graph",
            MODEL_DIR
        ]

        cmd = " ".join(args)
        print(f"🔧 启动 LMDeploy: {cmd}\n", flush=True)
        self.process = subprocess.Popen(cmd, shell=True)
        
        wait_ready(self.process)
        warmup()
        sleep(1)  # 挂起显存，由 Modal 固化保存为 GPU Snapshot

    @modal.enter(snap=False)
    def restore(self):
        """【后续看视频/翻网页触发】2~4 秒从快照极速恢复"""
        wake_up()

    @modal.exit()
    def stop(self):
        if hasattr(self, "process") and self.process:
            self.process.terminate()

########## MAIN ENTRY ##########

if __name__ == "__main__":
    lmdeploy_server = Server.from_name(APP_NAME, "LMDeployAWQServer")

    async def main():
        url = await lmdeploy_server.get_url.aio()
        print(f"\n==================================================")
        print(f"沉浸式翻译 API Base URL: {url}/v1")
        print(f"模型名称 (Model Name): {SERVE_MODEL_NAME}")
        print(f"==================================================\n")

    asyncio.run(main())
