# llama-Qwen35-27B-WebNovelWriter-Q4-gguf.py
# 部署命令: modal deploy llama-Qwen35-27B-WebNovelWriter-Q4-gguf.py

import modal
import time
from typing import List, Optional, Union, Dict, Any

# =============================================================================
# 1. 基础配置与模型参数
# =============================================================================
VOLUME_NAME = "qwen3.5-27b-cache"
MODEL_FILE = "Qwen3.5-27B-WebNovel-Writer-zh-Q4_K_M.gguf"
MODEL_ALIAS = "qwen3.5-27b-webnovel"

# 挂载持久化存储卷
vol = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)

# =============================================================================
# 2. 镜像构建 (直接安装预编译 CUDA 轮子，无需手动编译)
# =============================================================================
image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-devel-ubuntu22.04", add_python="3.11"
    )
    .env({
        # 强制底层向量计算使用通用的 AVX2/F16C 指令集，禁用易导致 Exit Code 132 的 AVX-512
        "GGML_AVX512": "OFF",
        "GGML_AVX2": "ON",
    })
    .pip_install(
        # 使用官方预编译的 CUDA 12 现成轮子，秒级安装
        "llama-cpp-python",
        extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cu124",
    )
    .pip_install(
        "fastapi==0.115.0",
        "uvicorn==0.30.6",
        "pydantic==2.9.2"
    )
)

app = modal.App(name="qwen3.5-27b-gguf-openai-api", image=image)

# =============================================================================
# 3. Modal GPU 服务类
# =============================================================================
@app.cls(
    gpu="L4",                   # 绑定 24GB 显存的 L4 GPU
    volumes={"/cache": vol},    # 挂载包含 GGUF 模型文件的存储卷
    scaledown_window=300,       # 5 分钟无请求自动休眠
    max_containers=1            # 单容器限制
)
class ModelService:
    @modal.enter()
    def load_model(self):
        """容器启动时加载模型至 GPU 显存"""
        from llama_cpp import Llama
        
        print("🚀 正在加载 llama.cpp 引擎 (开启 Q8_0 KV Cache 优化)...")
        self.llm = Llama(
            model_path=f"/cache/{MODEL_FILE}",
            n_gpu_layers=-1,        # 27B 全部 Offload 到 GPU 显存
            n_ctx=32768,            # 支撑 L4 上下文极速扩容至 32k
            type_k=8,               # KV Cache K 8-bit 量化 (GGML_TYPE_Q8_0)
            type_v=8,               # KV Cache V 8-bit 量化 (GGML_TYPE_Q8_0)
            offload_kqv=True,       # KV Cache 彻底推入 GPU 显存
            verbose=False,
            n_batch=512
        )
        print("🎉 模型加载完成，当前可用最大上下文：32768")

    @modal.web_endpoint(method="POST", path="/v1/chat/completions")
    def chat_completions(self, request_data: Dict[str, Any]):
        """OpenAI /v1/chat/completions 兼容标准接口 (采用纯 Dict 解析，避免顶层 Pydantic 依赖)"""
        from fastapi.responses import JSONResponse, StreamingResponse
        import json

        # 解析请求参数
        messages = request_data.get("messages", [])
        max_tokens = request_data.get("max_tokens", 4096)
        temperature = request_data.get("temperature", 0.8)
        top_p = request_data.get("top_p", 0.95)
        repeat_penalty = request_data.get("repeat_penalty", 1.08)
        stream = request_data.get("stream", False)

        # 转换为 llama-cpp 接受的 messages 格式
        formatted_messages = [{"role": msg.get("role"), "content": msg.get("content")} for msg in messages]

        # 1. 流式响应处理 (Streaming)
        if stream:
            def stream_generator():
                start_time = time.time()
                first_token_time = None
                generated_tokens = 0

                response_stream = self.llm.create_chat_completion(
                    messages=formatted_messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    repeat_penalty=repeat_penalty,
                    stream=True
                )

                for chunk in response_stream:
                    if first_token_time is None:
                        first_token_time = time.time()
                    
                    delta = chunk['choices'][0]['delta']
                    if 'content' in delta:
                        generated_tokens += 1
                        
                    data_str = json.dumps(chunk, ensure_ascii=False)
                    yield f"data: {data_str}\n\n"

                total_time = time.time() - start_time
                ttft = (first_token_time - start_time) if first_token_time else 0
                tps = generated_tokens / total_time if total_time > 0 else 0
                print(f"⏱️ 首字延迟: {ttft:.2f}s | 生成 {generated_tokens} tokens | 耗时 {total_time:.2f}s | 速度: {tps:.2f} tokens/s")

                yield "data: [DONE]\n\n"

            return StreamingResponse(stream_generator(), media_type="text/event-stream")

        # 2. 非流式响应处理 (Non-Streaming)
        start_time = time.time()
        response = self.llm.create_chat_completion(
            messages=formatted_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            repeat_penalty=repeat_penalty,
            stream=False
        )
        total_time = time.time() - start_time
        usage = response.get("usage", {})
        completion_tokens = usage.get("completion_tokens", 0)
        tps = completion_tokens / total_time if total_time > 0 else 0
        print(f"⏱️ [非流式] 生成 {completion_tokens} tokens | 耗时 {total_time:.2f}s | 速度: {tps:.2f} tokens/s")

        return JSONResponse(content=response)
