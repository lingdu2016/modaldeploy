# =============================================================================
# unsloth/Qwen3-14B-GGUF (Q5_K_XL)
# Modal L4 + llama.cpp + Gradio
# 最终修正版：正确使用 @modal.asgi_app()
# =============================================================================

import os
import re
import queue
import threading
import asyncio
import modal

# =============================================================================
# 模型配置
# =============================================================================
MODEL_REPO = "unsloth/Qwen3-14B-GGUF"
MODEL_FILE = "Qwen3-14B-UD-Q5_K_XL.gguf"

# =============================================================================
# S1: 环境镜像定义
# =============================================================================
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
        "requests",
    )
    .pip_install(
        "llama-cpp-python",
        extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cu121",
    )
)

# =============================================================================
# S2: 模型预下载函数
# =============================================================================
def hf_download():
    """将指定 GGUF 模型文件下载至 Volume 缓存卷中"""
    from huggingface_hub import hf_hub_download
    
    print(f"📦 开始平稳下载模型 {MODEL_REPO}/{MODEL_FILE} 到持久化卷...")
    hf_hub_download(
        repo_id=MODEL_REPO,
        filename=MODEL_FILE,
        local_dir="/cache",
        resume_download=True,
    )
    print("🎉 模型预下载成功完成并已持久化!")

# =============================================================================
# S3: 持久化卷挂载与构建绑定
# =============================================================================
vol = modal.Volume.from_name("qwen3-14b-cache", create_if_missing=True)

image = image.run_function(
    hf_download,
    volumes={"/cache": vol}
)

app = modal.App(name="qwen3-14b-fable-gradio", image=image)

# =============================================================================
# S4: 模型服务类
# =============================================================================
@app.cls(
    gpu="L4",
    volumes={"/cache": vol},
    scaledown_window=300,
    timeout=600,
    max_containers=1,
)
@modal.concurrent(max_inputs=20)
class ModelService:
    @modal.enter()
    def load_model(self):
        from llama_cpp import Llama

        model_path = f"/cache/{MODEL_FILE}"
        print(f"正在从缓存卷加载模型: {model_path} ...")
        
        self.llm = Llama(
            model_path=model_path,
            n_gpu_layers=-1,
            n_ctx=32768,
            verbose=False,
        )
        print("模型加载完成，GPU 已就绪！")

    # -------------------------------------------------------------------------
    # 预测接口（流式增量输出）
    # -------------------------------------------------------------------------
    async def predict(self, message, history):
        history = history[-10:]

        system_prompt = """
你是专精于简体中文创作的作家。
你的全部输出必须用流畅、地道的中文直接呈现，不要添加任何注释、分析、括号说明或推理过程。
若用户用中文提问，请用中文回答; 若用户明确要求英文，可切换。
你的风格自然、连贯，像一位友好的中国作家。
"""
        messages = [{"role": "system", "content": system_prompt}]

        for turn in history:
            if isinstance(turn, dict) and isinstance(turn.get("content"), str):
                messages.append({"role": turn["role"], "content": turn["content"]})

        messages.append({"role": "user", "content": message})

        result_queue = queue.Queue()
        END = object()
        stop_event = threading.Event()

        def worker():
            try:
                response = self.llm.create_chat_completion(
                    messages=messages,
                    max_tokens=8192,
                    temperature=0.65,
                    top_p=0.9,
                    min_p=0.05,
                    repeat_penalty=1.25,
                    repeat_last_n=256,
                    frequency_penalty=0.2,
                    presence_penalty=0.2,
                    mirostat=2,
                    mirostat_tau=5.0,
                    mirostat_eta=0.1,
                    top_k=40,
                    stream=True,
                    stop=[
                        "<|im_end|>",
                        "<|endoftext|>",
                        "<|eot_id|>",
                        "User:",
                        "用户:",
                        "```",
                    ]
                )

                for chunk in response:
                    if stop_event.is_set():
                        break
                    delta = chunk["choices"][0].get("delta", {})
                    text = delta.get("content")
                    if text:
                        result_queue.put(text)

            except Exception as e:
                result_queue.put(e)
            finally:
                result_queue.put(END)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        loop = asyncio.get_event_loop()
        output = ""                     
        last_yield_len = 0              
        chinese_start = -1              
        MAX_PREFIX_LEN = 80             

        chinese_pattern = re.compile(r'[\u4e00-\u9fa5]{2,}')

        while True:
            item = await loop.run_in_executor(None, result_queue.get)

            if item is END:
                break
            if isinstance(item, Exception):
                raise item

            output += item

            if chinese_start == -1:
                match = chinese_pattern.search(output)
                if match:
                    chinese_start = match.start()
                    clean_output = output[chinese_start:]
                    yield clean_output
                    last_yield_len = len(clean_output)
                else:
                    if len(output) > MAX_PREFIX_LEN:
                        stop_event.set()
                        yield "抱歉，生成过程中出现了技术问题，请重新提问。"
                        return
            else:
                current_full = output[chinese_start:]   
                if len(current_full) > last_yield_len:
                    new_part = current_full[last_yield_len:]
                    yield new_part
                    last_yield_len = len(current_full)

    # -------------------------------------------------------------------------
    # Gradio UI (修正为正确的 @modal.asgi_app 装饰器)
    # -------------------------------------------------------------------------
    @modal.asgi_app()
    def ui(self):
        import gradio as gr
        from fastapi import FastAPI

        web_app = FastAPI()

        async def chat(message, history):
            async for x in self.predict(message, history):
                yield x

        demo = gr.ChatInterface(
            fn=chat,
            type="messages",
            title="Qwen3-14B 中文聊天助手",
            description="Modal L4 + llama.cpp (unsloth/Qwen3-14B-GGUF - Q5_K_XL + Volume Cache)"
        )

        demo.queue(default_concurrency_limit=1)
        return gr.mount_gradio_app(web_app, demo, path="/")

# =============================================================================
# 部署入口
# =============================================================================
@app.local_entrypoint()
def main():
    print("部署完成")
    print("执行部署: modal deploy app.py")
