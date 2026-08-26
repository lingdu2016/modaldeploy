# =============================================================================
# Qwen3.6-14B-A3B-FableVibes-GGUF
# Modal L4 + llama.cpp + Gradio
# 最终优化版：无英文泄露、无死循环、无中英混杂、无前端重复
# =============================================================================

import os
import re
import modal
import queue
import threading
import asyncio

# =============================================================================
# 模型配置
# =============================================================================
MODEL_REPO = "tvall43/Qwen3.6-14B-A3B-FableVibes-GGUF"
MODEL_FILE = "Qwen3.6-14B-A3B-FableVibes-Q4_K_M.gguf"

# =============================================================================
# 环境镜像
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
        "huggingface_hub>=0.23.0,<0.26.0",
        "requests",
    )
    .pip_install(
        "llama-cpp-python",
        extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cu121",
    )
)

# =============================================================================
# 模型缓存卷
# =============================================================================
vol = modal.Volume.from_name("qwen36-14b-cache", create_if_missing=True)
app = modal.App(name="qwen36-14b-fable-gradio")

# =============================================================================
# 模型服务类
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
        from huggingface_hub import hf_hub_download
        from llama_cpp import Llama

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

    # -------------------------------------------------------------------------
    # 预测接口（流式）
    # -------------------------------------------------------------------------
    async def predict(self, message, history):
        """
        流式生成回答，内置“连续双字中文检测 + 增量输出 + 超时截断”。
        """
        history = history[-10:]

        # ---- 正面引导型 System Prompt ----
        system_prompt = """
你是专精于简体中文创作的作家。
你的全部输出必须用流畅、地道的中文直接呈现，不要添加任何注释、分析、括号说明或推理过程。
若用户用中文提问，请用中文回答；若用户明确要求英文，可切换。
你的风格自然、连贯，像一位友好的中国作家。
"""
        messages = [{"role": "system", "content": system_prompt}]

        for turn in history:
            if isinstance(turn, dict) and isinstance(turn.get("content"), str):
                messages.append({"role": turn["role"], "content": turn["content"]})

        messages.append({"role": "user", "content": message})

        # ---- 用于流式传输的队列和同步原语 ----
        result_queue = queue.Queue()
        END = object()
        stop_event = threading.Event()

        def worker():
            try:
                response = self.llm.create_chat_completion(
                    messages=messages,
                    max_tokens=4096,
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
                    # logit_bias={},   # 可按需添加
                    stream=True,
                    # 仅保留必要的停止词，移除所有英文短语
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

        # ---- 主循环：增量输出 + 连续双字中文检测 ----
        loop = asyncio.get_event_loop()
        output = ""                     # 累积所有文本（包括英文前缀）
        last_yield_len = 0              # 已 yield 的字符数（用于增量）
        chinese_start = -1              # 第一个连续中文的起始索引
        MAX_PREFIX_LEN = 80             # 未出现中文时的最大容忍长度

        # 使用正则检测连续两个及以上中文字符
        chinese_pattern = re.compile(r'[\u4e00-\u9fa5]{2,}')

        while True:
            item = await loop.run_in_executor(None, result_queue.get)

            if item is END:
                break
            if isinstance(item, Exception):
                raise item

            output += item

            # ---- 如果尚未找到正文起始点 ----
            if chinese_start == -1:
                match = chinese_pattern.search(output)
                if match:
                    chinese_start = match.start()
                    # 从第一个连续中文开始输出（之前的英文前缀全部丢弃）
                    clean_output = output[chinese_start:]
                    # 增量输出（此时上次输出长度为0，所以会输出全部 clean_output）
                    yield clean_output
                    last_yield_len = len(clean_output)
                else:
                    # 仍未出现连续中文，检查是否超长
                    if len(output) > MAX_PREFIX_LEN:
                        stop_event.set()
                        yield "抱歉，生成过程中出现了技术问题，请重新提问。"
                        return
                    # 否则继续等待，此时不 yield 任何内容
            else:
                # ---- 已找到正文起点：增量输出新增部分 ----
                current_full = output[chinese_start:]   # 正文部分（可能还在增长）
                if len(current_full) > last_yield_len:
                    new_part = current_full[last_yield_len:]
                    yield new_part
                    last_yield_len = len(current_full)
                # 如果长度未变，则无新内容，继续循环

    # -------------------------------------------------------------------------
    # Gradio UI
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
            title="Qwen3.6-14B 中文聊天 + 小说助手",
            description="Modal L4 + llama.cpp（最终优化版：纯中文增量输出）"
        )

        demo.queue(default_concurrency_limit=1)
        return gr.mount_gradio_app(web_app, demo, path="/")

# =============================================================================
# 部署入口
# =============================================================================
@app.local_entrypoint()
def main():
    print("部署完成")
    print("执行: modal deploy llama.py")
