# 作用：在 Modal 云端使用 llama.cpp 部署 Qwen/Qwen3-VL-8B-Instruct-GGUF (多模态视觉版) + 网页 + API
# =============================================================================
# 部署命令: modal deploy llama-Qwen3-vl-8B-Q8-gguf.py
# =============================================================================

import os
import modal

# =============================================================================
# S1: 环境准备 - 安装支持多模态的 llama-cpp-python 与 fastapi
# =============================================================================
MODEL_REPO = "Qwen/Qwen3-VL-8B-Instruct-GGUF"

# 严格对齐官方仓库的 Q8_0 主模型及视觉投影组件（mmproj）文件名
MODEL_FILE = "Qwen3VL-8B-Instruct-Q8_0.gguf"
MMPROJ_FILE = "mmproj-Qwen3VL-8B-Instruct-F16.gguf"

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-runtime-ubuntu22.04",
        add_python="3.11"
    )
    .apt_install("git", "wget", "curl")
    .pip_install(
        "fastapi",
        "gradio==5.4.0",
        "huggingface_hub==0.25.2",
        "pydantic<3",
        "requests",
        "Pillow"
    )
    # 强制匹配 CUDA 12.4 的加速轮子，确保 n_gpu_layers=-1 完美卸载至 GPU 运行
    .pip_install(
        "llama-cpp-python",
        extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cu124"
    )
)


# =============================================================================
# S2: 模型预下载 - 从 Hugging Face 官方仓库下载主模型与视觉组件
# =============================================================================
def hf_download():
    from huggingface_hub import hf_hub_download

    print(f"📦 开始下载官方主模型 {MODEL_REPO}/{MODEL_FILE}...")
    hf_hub_download(
        repo_id=MODEL_REPO,
        filename=MODEL_FILE,
        local_dir="/cache",
        resume_download=True,
    )

    print(f"📦 开始下载官方视觉组件 {MODEL_REPO}/{MMPROJ_FILE}...")
    hf_hub_download(
        repo_id=MODEL_REPO,
        filename=MMPROJ_FILE,
        local_dir="/cache",
        resume_download=True,
    )

    print("🎉 Qwen3-VL 官方 GGUF 模型及视觉组件下载成功完成!")


# =============================================================================
# S3: 持久化卷挂载与 App 初始化
# =============================================================================
vol = modal.Volume.from_name("qwen3-vl-8b-gguf-cache", create_if_missing=True)

image = image.run_function(
    hf_download,
    volumes={"/cache": vol},
)

app = modal.App(name="llama-Qwen3-vl-8B-Q8-gguf", image=image)


# =============================================================================
# S4: 多模态模型服务 (线程安全锁 + L4 显存安全配置)
# =============================================================================
@app.cls(
    gpu="T4",  # L4 运行 Qwen3-VL 速度极快，16GB~24GB 显存非常充裕
    volumes={"/cache": vol},
    scaledown_window=150,
    timeout=600,
    max_containers=1,
)
@modal.concurrent(max_inputs=20)
class ModelService:

    @modal.enter()
    def load_model(self):
        import asyncio
        from llama_cpp import Llama

        self.lock = asyncio.Lock()
        model_path = f"/cache/{MODEL_FILE}"
        mmproj_path = f"/cache/{MMPROJ_FILE}"

        print("加载 Qwen3-VL 多模态 llama.cpp 模型...")

        # 多模态 GGUF 必须同时传入 model_path 和 mmproj_path
        self.llm = Llama(
            model_path=model_path,
            mmproj_path=mmproj_path,
            n_gpu_layers=-1,  # 全量卸载到 GPU 显存
            n_ctx=8192,      # 上下文长度
            n_batch=512,
            flash_attn=True,
            verbose=False,
        )

        print("Qwen3-VL 模型加载完成！")

    async def predict(self, message: str, history: list, image_path: str = None):
        """流式多模态推理核心逻辑"""
        import asyncio

        async with self.lock:
            history = history[-10:]
            
            # 构造符合 llama-cpp-python 多模态接口的 messages 结构
            content_list = [{"type": "text", "text": message}]
            if image_path and os.path.exists(image_path):
                content_list.append({"type": "image_url", "image_url": {"url": image_path}})

            formatted_messages = []
            for turn in history:
                if isinstance(turn, dict) and "role" in turn and "content" in turn:
                    formatted_messages.append(turn)
            
            formatted_messages.append({"role": "user", "content": content_list})

            loop = asyncio.get_event_loop()
            aqueue: asyncio.Queue = asyncio.Queue()
            END = object()

            def worker():
                try:
                    stream = self.llm.create_chat_completion(
                        messages=formatted_messages,
                        max_tokens=2048,
                        temperature=0.7,
                        stream=True,
                    )

                    for chunk in stream:
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            loop.call_soon_threadsafe(aqueue.put_nowait, delta)
                except Exception as e:
                    loop.call_soon_threadsafe(aqueue.put_nowait, e)
                finally:
                    loop.call_soon_threadsafe(aqueue.put_nowait, END)

            import threading
            thread = threading.Thread(target=worker, daemon=True)
            thread.start()

            output = ""
            while True:
                item = await aqueue.get()
                if item is END:
                    break
                if isinstance(item, Exception):
                    raise item
                output += item
                yield output

    # =========================================================================
    # FastAPI + Gradio 混合挂载 (同时提供网页与 API 接口)
    # =========================================================================
    @modal.asgi_app()
    def ui(self):
        import gradio as gr
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from pydantic import BaseModel
        from typing import Optional

        web_app = FastAPI()

        class ChatRequest(BaseModel):
            message: str
            image_url: Optional[str] = None  # 支持传图片 URL 或本地绝对路径

        # 提供自定义 API 接口：POST /api/chat
        @web_app.post("/api/chat")
        async def api_chat(req: ChatRequest):
            async def event_generator():
                async for chunk in self.predict(req.message, [], req.image_url):
                    yield chunk
            return StreamingResponse(event_generator(), media_type="text/plain; charset=utf-8")

        async def chat(message, history):
            # 适配 Gradio 5 多模态组件输入解析
            img_path = None
            text_prompt = message
            if isinstance(message, dict):
                files = message.get("files", [])
                if files:
                    img_path = files[0]
                text_prompt = message.get("text", "")

            async for x in self.predict(text_prompt, history, img_path):
                yield x

        demo = gr.ChatInterface(
            fn=chat,
            type="messages",
            multimodal=True,  # 开启网页多模态传图按钮
            title="Qwen3-VL-8B-Instruct-GGUF 多模态助手",
            description="基于官方 Q8_0 GGUF 模型 + llama.cpp 部署，支持图像理解、OCR 及 API 调用",
        )

        demo.queue(default_concurrency_limit=1)
        return gr.mount_gradio_app(web_app, demo, path="/")


# =============================================================================
# S5: 本地入口点
# =============================================================================
@app.local_entrypoint()
def main():
    print("部署脚本加载完毕，请运行：modal deploy llama-Qwen3-vl-8B-Q8-gguf.py")
