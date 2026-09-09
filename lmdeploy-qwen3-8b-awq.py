import os
import subprocess
from modal import App, Image, Volume, web_server, Secret, concurrent


########## CONSTANTS ##########

MODEL_NAME = "Qwen/Qwen3-8B-AWQ"
SERVE_MODEL_NAME = "qwen3-8b-awq"
MODEL_DIR = f"/models/{MODEL_NAME}"
SERVER_PORT = 23333
GPU = "T4"

# 1. 自动处理 Hugging Face Secret 鉴权
# 如果你在 Modal 环境控制台配置了名为 'huggingface-secret' 的 Secret，可以直接自动绑定；
# 同时保留了从本地环境变量读取 HF_TOKEN 的兜底逻辑。
hf_token = os.environ.get("HF_TOKEN")
secrets_list = []

# 优先尝试加载 Modal 控制台预设的 huggingface-secret
try:
    secrets_list.append(Secret.from_name("huggingface-secret"))
except Exception:
    # 兜底：如果控制台没配置，但本地环境变量有 HF_TOKEN，则动态生成 Secret
    if hf_token:
        secrets_list.append(Secret.from_dict({"HF_TOKEN": hf_token}))

# 持久化 Volume 缓存模型权重（根据模型名隔离）
model_volume = Volume.from_name(
    f"lmdeploy-model-cache-{SERVE_MODEL_NAME}",
    create_if_missing=True
)


########## UTILS FUNCTIONS ##########

def download_hf_model(model_dir: str, model_name: str):
    """使用 Rust 驱动的 hf_transfer 实现极速模型下载"""
    import os
    from huggingface_hub import snapshot_download

    os.makedirs(model_dir, exist_ok=True)
    token = os.environ.get("HF_TOKEN")

    print(f"🚀 开始极速下载模型 {model_name} 到 {model_dir} ...", flush=True)
    snapshot_download(
        repo_id=model_name,
        local_dir=model_dir,
        token=token,
    )
    print(f"✅ 模型 {model_name} 下载成功！", flush=True)


########## IMAGE DEFINITION ##########

image = (
    Image.from_registry("openmmlab/lmdeploy:v0.7.3-cu12")
    .pip_install(
        "huggingface_hub[hf_transfer]>=0.30.0,<1.0",
        "transformers>=4.51.0",
        "grpclib==0.4.7",
    )
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)


########## APP SETUP ##########

app = App(f"lmdeploy-{SERVE_MODEL_NAME}")


@app.function(
    image=image,
    gpu=GPU,
    cpu=2.0,
    memory=16384,
    scaledown_window=60,
    timeout=600,
    max_containers=1,
    volumes={"/models": model_volume},
    secrets=secrets_list,
)
@concurrent(max_inputs=8)
@web_server(port=SERVER_PORT, startup_timeout=60 * 10)
def serve():
    print(f"🚀 启动 LMDeploy API 服务 [模型: {MODEL_NAME}] ...", flush=True)
    print(f"📌 模型挂载路径: {MODEL_DIR}", flush=True)

    # 1. 检查模型文件是否已完整存在于 Volume 中
    config_file = os.path.join(MODEL_DIR, "config.json")
    if not os.path.exists(config_file):
        print(f"⚠️ Volume 中未找到模型权重，触发极速下载...", flush=True)
        download_hf_model(MODEL_DIR, MODEL_NAME)
        model_volume.commit()
        print("💾 模型已成功写入并提交到 Modal Volume！", flush=True)
    else:
        print(f"✅ 找到已有模型权重 (config.json 存在)，跳过下载直接启动。", flush=True)

    # 2. 组装 8B-AWQ 专属启动指令
    args = [
        "lmdeploy serve api_server",
        f"--model-name {SERVE_MODEL_NAME}",
        f"--server-name 0.0.0.0",
        f"--server-port {SERVER_PORT}",
        "--backend turbomind",
        "--model-format awq",                  # 👈 AWQ 量化模型关键参数
        "--session-len 16384",
        "--tp 1",
        "--cache-max-entry-count 0.4",         # 👈 T4 16G 显存黄金比例（留 40% 给 KV Cache）
        MODEL_DIR
    ]

    cmd = " ".join(args)
    print(f"🔧 执行命令: {cmd}\n", flush=True)

    # 3. 拉起服务并实时管道化输出日志
    process = subprocess.Popen(cmd, shell=True)
    process.wait()


@app.local_entrypoint()
def main():
    print("✨ 代码格式校验完成，请运行: modal deploy <文件名>.py")
