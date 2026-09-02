import os
import subprocess
import time
from pathlib import Path
import modal
import urllib.request
import urllib.error

# ====================== 配置区 (完全还原原版) ======================
VOLUME_NAME = "wan22-5b-cache"          # 模型缓存卷
OUTPUT_VOLUME_NAME = "wan22-output"   # 生成视频保存卷
APP_NAME = "comfyui-wan22-5b"
GPU_TYPE = "L4"
MAX_CONTAINERS = 1
MAX_INPUTS = 10
TIMEOUT_SECONDS = 1800
SCALEDOWN_WINDOW_SECONDS = 300
WEB_PORT = 8000
WEB_STARTUP_TIMEOUT_SECONDS = 300
HF_SECRET_NAME = "huggingface-secret"
# ====================================================================

vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
output_vol = modal.Volume.from_name(OUTPUT_VOLUME_NAME, create_if_missing=True)

# 100% 还原您原本的 Image 镜像定义，确保命中缓存（不加任何新 apt 包）
image = (
    modal.Image.debian_slim(python_version="3.11")
    .env({"DEBIAN_FRONTEND": "noninteractive"})
    .apt_install(
        "git",
        "nano",
        "libgl1",        
        "libglx-mesa0",  
        "libglib2.0-0",
    )
    .pip_install(
        "comfy-cli==1.7.1",
        "huggingface-hub==0.36.0",
        "Pillow>=10.0.0",
        "python-dotenv>=1.0.1",
    )
    .run_commands(
        "comfy --skip-prompt install --fast-deps --nvidia --version 0.11.1"
    )
)

# 安装自定义节点 (完全保持原版)
image = (
    image
    .run_commands("comfy node install --fast-deps image-resize-comfyui")
    .run_commands("comfy node install --fast-deps https://github.com/Comfy-Org/ComfyUI-Manager")
    .run_commands("comfy node install --fast-deps https://github.com/ltdrdata/ComfyUI-Impact-Pack")
    .run_commands("comfy node install --fast-deps https://github.com/ltdrdata/ComfyUI-Inspire-Pack")
    .run_commands("comfy node install --fast-deps https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite")
    .run_commands("comfy node install --fast-deps https://github.com/city96/ComfyUI-GGUF")
    .run_commands("comfy node install --fast-deps https://github.com/chrisgoringe/cg-use-everywhere")
    .run_commands("comfy node install --fast-deps https://github.com/Fannovel16/ComfyUI-Frame-Interpolation")
    .run_commands("comfy node install --fast-deps https://github.com/M1kep/ComfyLiterals")
    .run_commands("comfy node install --fast-deps https://github.com/ClownsharkBatwing/RES4LYF")
    .run_commands("comfy node install --fast-deps https://github.com/aria1th/ComfyUI-LogicUtils")
    .run_commands("comfy node install --fast-deps https://github.com/kijai/ComfyUI-KJNodes")
    .run_commands("comfy node install --fast-deps https://github.com/rgthree/rgthree-comfy")
    .run_commands("comfy node install --fast-deps https://github.com/yolain/ComfyUI-Easy-Use")
    .run_commands("comfy node install --fast-deps https://github.com/lihaoyun6/ComfyUI-FlashVSR_Ultra_Fast")
    .run_commands("comfy node install --fast-deps https://github.com/cubiq/ComfyUI_essentials")
    .run_commands("comfy node install --fast-deps https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler")
)


def symlink_into_comfy(
    models_root: Path,
    subdir: str,
    source_path: str | Path,
    target_name: str | None = None,
) -> None:
    source = Path(source_path)
    destination_dir = models_root / subdir
    destination_dir.mkdir(parents=True, exist_ok=True)

    destination = destination_dir / (target_name or source.name)
    if destination.exists() or destination.is_symlink():
        destination.unlink()

    destination.symlink_to(source)


def hf_download_wan22_5b_models():
    """下载 Wan 2.2 5B 相关模型"""
    from huggingface_hub import hf_hub_download

    cache_dir = Path("/cache")
    models_root = Path("/root/comfy/ComfyUI/models")

    model_specs = [
        (
            "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
            "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors",
            "diffusion_models",
            "wan2.2_ti2v_5B_fp16.safetensors",
        ),
        (
            "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
            "split_files/vae/wan2.2_vae.safetensors",
            "vae",
            "wan2.2_vae.safetensors",
        ),
        (
            "Comfy-Org/Wan_2.2_ComfyUI_Repackaged",
            "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
            "text_encoders",
            "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        ),
    ]

    for repo_id, filename, subdir, target_name in model_specs:
        cached_file = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            cache_dir=cache_dir,
        )
        symlink_into_comfy(models_root, subdir, cached_file, target_name)


image = (
    image.env({"HF_XET_HIGH_PERFORMANCE": "1"})
    .run_commands("rm -rf /root/comfy/ComfyUI/output")
    .run_function(
        hf_download_wan22_5b_models,
        volumes={"/cache": vol},
        secrets=[modal.Secret.from_name(HF_SECRET_NAME)],
    )
)

app = modal.App(name=APP_NAME, image=image)


@app.function(
    max_containers=MAX_CONTAINERS,
    gpu=GPU_TYPE,
    volumes={
        "/cache": vol,
        "/root/comfy/ComfyUI/output": output_vol,
    },
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
)
@modal.concurrent(max_inputs=MAX_INPUTS)
@modal.web_server(WEB_PORT, startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS)
def ui():
    """启动 ComfyUI Web UI 并通过 HTTP 探测连接就绪状态"""
    print(f"=== [STARTING] Launching ComfyUI on port {WEB_PORT} ===")
    
    # 1. 启动 ComfyUI 后台进程
    process = subprocess.Popen(
        f"comfy launch -- --listen 0.0.0.0 --port {WEB_PORT}",
        shell=True,
    )

    start_time = time.time()
    
    # 2. 轮询探测 8000 端口，服务就绪后直接 return 告知 Modal 网关
    while time.time() - start_time < WEB_STARTUP_TIMEOUT_SECONDS:
        if process.poll() is not None:
            raise RuntimeError("ComfyUI process exited prematurely.")

        try:
            req = urllib.request.Request(f"http://127.0.0.1:{WEB_PORT}")
            with urllib.request.urlopen(req, timeout=2) as response:
                print(f"=== [SUCCESS] ComfyUI is up and running! (Status: {response.status}) ===")
                return
        except urllib.error.HTTPError as e:
            # 即使返回 404/403 也说明 HTTP 服务端已经开启监听
            print(f"=== [SUCCESS] ComfyUI HTTP server active! (Code: {e.code}) ===")
            return
        except Exception as err:
            elapsed = int(time.time() - start_time)
            if elapsed % 10 == 0:
                print(f"[WAITING {elapsed}s] Probing http://127.0.0.1:{WEB_PORT} ...")

        time.sleep(2)

    raise TimeoutError("ComfyUI server failed to respond within startup timeout.")
