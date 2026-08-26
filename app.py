# =============================================================================
# Qwen3.6-14B-A3B-FableVibes-GGUF
# Modal L4 + llama.cpp + Gradio
# 聊天 + 长篇小说生成版
# =============================================================================

import os
import modal

MODEL_REPO = "tvall43/Qwen3.6-14B-A3B-FableVibes-GGUF"
MODEL_FILE = "Qwen3.6-14B-A3B-FableVibes-Q4_K_M.gguf"

# =============================================================================
# 环境
# =============================================================================

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-runtime-ubuntu22.04",
        add_python="3.11"
    )
    .apt_install(
        "git",
        "wget",
        "curl"
    )
    .pip_install(
        "fastapi",
        "gradio==5.4.0",
        "huggingface_hub>=0.23.0,<0.26.0",
        "requests",
    )
    .pip_install(
        "llama-cpp-python",
        extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cu121",
    )
)

# 模型缓存
vol = modal.Volume.from_name(
    "qwen36-14b-cache",
    create_if_missing=True
)

app = modal.App(
    name="qwen36-14b-fable-gradio"
)

# =============================================================================
# 模型服务
# =============================================================================

@app.cls(
    image=image,
    gpu="L4",
    volumes={
        "/cache": vol
    },
    scaledown_window=300,
    timeout=600,
    max_containers=1,
)
@modal.concurrent(max_inputs=20)
class ModelService:

    @modal.enter()
    def load_model(self):
        from huggingface_hub import hf_hub_download
        from llama_cpp import Llama

        self.lock = None
        model_path = f"/cache/{MODEL_FILE}"

        if not os.path.exists(model_path):
            print(f"下载模型: {MODEL_FILE}")
            hf_hub_download(
                repo_id=MODEL_REPO,
                filename=MODEL_FILE,
                local_dir="/cache"
            )
            vol.commit()

        print("加载 llama.cpp ...")

        self.llm = Llama(
            model_path=model_path,
            n_gpu_layers=-1,
            n_ctx=32768,
            verbose=False,
        )

        print("模型加载完成")

    async def predict(
        self,
        message,
        history
    ):
        import asyncio
        import queue
        import re
        import threading

        history = history[-10:]

        # 极简提示词：删除了所有条件判定，防止模型进入逻辑死循环
        messages = [
            {
                "role": "system",
                "content": """你是一个优秀的中文 AI 助手（基于 Qwen 架构）。
请直接用流畅、地道的简体中文回答问题。
严禁在输出中包含任何自我纠错（如 Wait, Let me re-read）、计划草稿或英文内部思考，直接输出最终的中文正文即可。"""
            }
        ]

        for turn in history:
            if (
                isinstance(turn, dict)
                and isinstance(turn.get("content"), str)
            ):
                messages.append(
                    {
                        "role": turn["role"],
                        "content": turn["content"]
                    }
                )

        messages.append(
            {
                "role": "user",
                "content": message
            }
        )

        result_queue = queue.Queue()
        END = object()

        def worker():
            try:
                response = (
                    self.llm
                    .create_chat_completion(
                        messages=messages,
                        max_tokens=16384,
                        temperature=0.4,
                        top_p=0.85,
                        min_p=0.05,
                        repeat_penalty=1.18,
                        stream=True,
                        stop=[
                            "<|im_end|>",
                            "<|endoftext|>",
                            "<|eot_id|>",
                            "User:",
                            "用户:",
                        ]
                    )
                )

                for chunk in response:
                    delta = (
                        chunk["choices"][0]
                        .get("delta", {})
                    )
                    text = delta.get("content")
                    if text:
                        result_queue.put(text)

            except Exception as e:
                result_queue.put(e)
            finally:
                result_queue.put(END)

        thread = threading.Thread(
            target=worker,
            daemon=True
        )
        thread.start()

        loop = asyncio.get_event_loop()
        
        raw_accumulator = ""
        has_passed_think = False
        display_output = ""

        while True:
            item = await loop.run_in_executor(
                None,
                result_queue.get
            )

            if item is END:
                break

            if isinstance(item, Exception):
                raise item

            if not has_passed_think:
                raw_accumulator += item
                
                # 场景 1：如果存在标准的 </think> 结束标签
                if "</think>" in raw_accumulator:
                    has_passed_think = True
                    display_output = raw_accumulator.split("</think>", 1)[1].lstrip()
                    if display_output:
                        yield display_output
                else:
                    # 场景 2：“认字拦截法”——寻找连续的两个中文字符作为正文的真实起点
                    chinese_match = re.search(r'[\u4e00-\u9fa5]{2,}', raw_accumulator)
                    
                    if chinese_match:
                        start_idx = chinese_match.start()
                        prefix = raw_accumulator[:start_idx]
                        
                        # 核心判断：如果中文开始前的“前缀”里包含了英文单词(>=3个字母组合)，
                        # 说明前面的部分全是模型偷偷思考的英文草稿，予以果断抛弃。
                        if bool(re.search(r'[a-zA-Z]{3,}', prefix)):
                            has_passed_think = True
                            display_output = raw_accumulator[start_idx:]
                            yield display_output
                        else:
                            # 如果前缀非常干净（只有空格或换行），则是正常的中文开头
                            has_passed_think = True
                            display_output = raw_accumulator
                            yield display_output
                    # 如果积累了 300 个字符还没见到中文，兜底放行（防止死循环撑爆内存或者用户本身要求写英文）
                    elif len(raw_accumulator) > 300:
                        has_passed_think = True
                        display_output = raw_accumulator
                        yield display_output
            else:
                display_output += item
                yield display_output

    # =========================================================================
    # Gradio
    # =========================================================================

    @modal.asgi_app()
    def ui(self):
        import gradio as gr
        from fastapi import FastAPI

        web_app = FastAPI()

        async def chat(
            message,
            history
        ):
            async for x in self.predict(
                message,
                history
            ):
                yield x

        demo = gr.ChatInterface(
            fn=chat,
            type="messages",
            title="Qwen3.6-14B 中文聊天 + 小说助手",
            description="Modal L4 + llama.cpp"
        )

        demo.queue(
            default_concurrency_limit=1
        )

        return gr.mount_gradio_app(
            web_app,
            demo,
            path="/"
        )

# =============================================================================
# deploy
# =============================================================================

@app.local_entrypoint()
def main():
    print("部署完成")
    print("modal deploy app.py")
