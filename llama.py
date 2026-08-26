# =============================================================================
# Qwen3-14B-GGUF Q5_K_XL
# Modal L4 + llama.cpp + Gradio 5.4
# Stable Version (Fixed)
# =============================================================================


import modal


# =============================================================================
# Model
# =============================================================================

MODEL_REPO = "unsloth/Qwen3-14B-GGUF"

MODEL_FILE = "Qwen3-14B-UD-Q5_K_XL.gguf"



# =============================================================================
# Image
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
        "huggingface_hub==0.25.2",
        "pydantic<3",
        "requests"
    )

    # FIX: use the cu124 wheel to match the cuda:12.4.1 base image.
    # Mismatched CUDA wheel/runtime versions can cause the model load
    # inside @modal.enter() to crash silently, which means the container
    # never comes up and Gradio never gets a chance to load.
    .pip_install(
        "llama-cpp-python",
        extra_index_url=
        "https://abetlen.github.io/llama-cpp-python/whl/cu124"
    )
)



# =============================================================================
# Volume
# =============================================================================


model_volume = modal.Volume.from_name(
    "qwen3-14b-cache",
    create_if_missing=True
)



# =============================================================================
# Download model
# =============================================================================


def download_model():

    from huggingface_hub import hf_hub_download


    print("Downloading model...")


    hf_hub_download(

        repo_id=MODEL_REPO,

        filename=MODEL_FILE,

        local_dir="/cache",

        resume_download=True

    )


    print("Model download finished")




image = image.run_function(

    download_model,

    volumes={
        "/cache": model_volume
    }

)



# =============================================================================
# App
# =============================================================================


app = modal.App(

    "qwen3-14b-gradio",

    image=image

)



# =============================================================================
# Model Service
# =============================================================================


# FIX: allow the container to handle several requests at once.
# Without this, Modal serializes every incoming request (HTML, JS/CSS,
# the Gradio websocket, etc.), so the page hangs on load even though
# nothing is actually broken.
@app.cls(

    gpu="L4",

    volumes={
        "/cache":model_volume
    },


    timeout=900,


    scaledown_window=300,


    max_containers=1

)
@modal.concurrent(max_inputs=10)
class Qwen3Service:



    @modal.enter()

    def load_model(self):

        from llama_cpp import Llama



        print("Loading Qwen3...")



        self.llm = Llama(

            model_path=
            f"/cache/{MODEL_FILE}",


            n_gpu_layers=-1,


            # L4 24GB
            n_ctx=16384,


            verbose=False

        )


        print("Qwen3 loaded")




    # =========================================================================
    # Chat
    # =========================================================================


    # FIX: plain generator instead of `async def` + blocking sync call.
    # llama-cpp's create_chat_completion() is fully synchronous, so wrapping
    # it in `async def` gains nothing and risks blocking the event loop
    # under concurrent requests. A normal generator is what Gradio expects
    # for streaming and is more predictable here.
    def chat(

        self,

        message,

        history

    ):


        messages = [

            {

                "role":"system",

                "content":
"""
你是一名专业中文小说作者。

要求：

1. 永远使用简体中文。
2. 不输出思考过程。
3. 不输出 <think> 标签。
4. 直接输出最终答案。
5. 支持长篇小说创作。
6. 保持人物和剧情连续。

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



        try:

            stream = self.llm.create_chat_completion(


                messages=messages,


                max_tokens=4096,


                temperature=0.7,


                top_p=0.8,


                top_k=20,


                min_p=0,


                repeat_penalty=1.1,


                stream=True

            )



            output=""



            for chunk in stream:


                delta = (

                    chunk["choices"][0]
                    ["delta"]

                )


                text = delta.get(

                    "content",

                    ""

                )


                if text:


                    output += text


                    # FIX: strip any stray <think>...</think> block just in
                    # case the template still emits an (empty or non-empty)
                    # think block per the README's note on enable_thinking.
                    cleaned = output

                    if "<think>" in cleaned:

                        cleaned = cleaned.split("</think>")[-1].lstrip("\n")


                    yield cleaned

        except Exception as e:

            yield f"生成时出错：{e}"





    # =========================================================================
    # Gradio
    # =========================================================================


    @modal.asgi_app()

    def ui(self):


        import gradio as gr

        from fastapi import FastAPI



        web_app = FastAPI()



        demo = gr.ChatInterface(


            fn=self.chat,


            type="messages",


            title=
            "Qwen3-14B 中文小说助手",


            description=
"""
Qwen3-14B-UD-Q5_K_XL

Modal L4 + llama.cpp

支持长篇小说创作
"""

        )



        demo.queue(

            default_concurrency_limit=5

        )



        return gr.mount_gradio_app(

            web_app,

            demo,

            path="/"

        )




# =============================================================================
# Deploy
# =============================================================================


@app.local_entrypoint()

def main():

    print(
        """
部署:

modal deploy llama.py
"""
    )
