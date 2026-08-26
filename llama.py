# =============================================================================
# Qwen3-14B-GGUF Q5_K_XL
# Modal L4 + llama.cpp + Gradio
# Fixed version according to Qwen3 GGUF README
# =============================================================================

import os
import modal


# ============================================================
# Model
# ============================================================

MODEL_REPO = "unsloth/Qwen3-14B-GGUF"
MODEL_FILE = "Qwen3-14B-UD-Q5_K_XL.gguf"



# ============================================================
# Image
# ============================================================

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
        "huggingface_hub",
        "requests"
    )
    .pip_install(
        "llama-cpp-python",
        extra_index_url=
        "https://abetlen.github.io/llama-cpp-python/whl/cu121"
    )
)



# ============================================================
# Volume
# ============================================================

vol = modal.Volume.from_name(
    "qwen3-14b-cache",
    create_if_missing=True
)



# ============================================================
# Download
# ============================================================

def download_model():

    from huggingface_hub import hf_hub_download

    path = hf_hub_download(
        repo_id=MODEL_REPO,
        filename=MODEL_FILE,
        local_dir="/cache",
        resume_download=True
    )

    print("MODEL:", path)



image = image.run_function(
    download_model,
    volumes={
        "/cache":vol
    }
)



app = modal.App(
    "qwen3-14b-gradio",
    image=image
)



# ============================================================
# Service
# ============================================================


@app.cls(
    gpu="L4",
    volumes={
        "/cache":vol
    },
    timeout=900,
    scaledown_window=300,
    max_containers=1
)


class ModelService:



    @modal.enter()
    def load(self):

        from llama_cpp import Llama


        self.llm = Llama(

            model_path=
            f"/cache/{MODEL_FILE}",

            # L4 full GPU
            n_gpu_layers=-1,


            # Qwen3 recommended
            n_ctx=16384,


            verbose=False

        )


        print(
            "Qwen3 loaded"
        )




    async def chat(
        self,
        message,
        history
    ):


        messages=[

            {
                "role":"system",
                "content":
                """
你是一名中文小说作者。

规则：

- 使用简体中文。
- 不输出思考过程。
- 不输出<think>标签。
- 直接给最终答案。
- 支持长篇小说创作。
- 保持人物、世界观、剧情连续。

/no_think
"""
            }

        ]


        # Gradio messages format

        for item in history:

            if isinstance(item,dict):

                messages.append(
                    {
                        "role":
                        item["role"],

                        "content":
                        item["content"]
                    }
                )


        messages.append(

            {
                "role":"user",

                "content":
                message + "\n/no_think"

            }

        )



        stream = self.llm.create_chat_completion(

            messages=messages,


            max_tokens=4096,


            temperature=0.7,


            top_p=0.8,


            top_k=20,


            repeat_penalty=1.1,


            stream=True

        )



        output=""



        for chunk in stream:


            delta = (
                chunk
                ["choices"]
                [0]
                ["delta"]
            )


            text = delta.get(
                "content",
                ""
            )


            if text:

                output += text

                yield output





    # ========================================================
    # UI
    # ========================================================


    @modal.asgi_app()

    def app_ui(self):

        import gradio as gr
        from fastapi import FastAPI



        web=FastAPI()



        demo = gr.ChatInterface(

            fn=self.chat,


            type="messages",


            title=
            "Qwen3-14B 中文小说助手",


            description=
            """
unsloth/Qwen3-14B-GGUF
Q5_K_XL
Modal L4 + llama.cpp
            """

        )



        demo.queue(
            default_concurrency_limit=5
        )


        return gr.mount_gradio_app(
            web,
            demo,
            path="/"
        )





# ============================================================
# Deploy
# ============================================================


@app.local_entrypoint()

def main():

    print(
        "Deploy with: modal deploy app.py"
    )
