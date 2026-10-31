import modal
import io

app = modal.App("edge-tts-web")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("edge-tts", "fastapi", "python-multipart")
)

VOICES = {
    "晓晓（女声·推荐）": "zh-CN-XiaoxiaoNeural",
    "云希（男声·推荐）": "zh-CN-YunxiNeural",
    "晓伊（女声）": "zh-CN-XiaoyiNeural",
    "云健（男声）": "zh-CN-YunjianNeural",
    "晓辰（女声）": "zh-CN-XiaochenNeural",
    "晓涵（女声）": "zh-CN-XiaohanNeural",
    "晓梦（女声）": "zh-CN-XiaomengNeural",
    "晓秋（女声）": "zh-CN-XiaoqiuNeural",
    "晓睿（女声）": "zh-CN-XiaoruiNeural",
    "晓双（女声）": "zh-CN-XiaoshuangNeural",
    "云扬（男声）": "zh-CN-YunyangNeural",
    "云夏（男声）": "zh-CN-YunxiaNeural",
    "云野（男声）": "zh-CN-YunyeNeural",
    "English - Aria（女）": "en-US-AriaNeural",
    "English - Guy（男）": "en-US-GuyNeural",
    "English - Jenny（女）": "en-US-JennyNeural",
    "English - Christopher（男）": "en-US-ChristopherNeural",
}

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

def create_web_app():
    from fastapi import FastAPI, Form
    from fastapi.responses import HTMLResponse, StreamingResponse

    web_app = FastAPI(title="edge-tts API")

    async def generate_audio(text: str, voice: str, rate: str, pitch: str, volume: str):
        import edge_tts

        text = text.strip()
        if not text:
            return None, "文字不能为空"
        if len(text) > 5000:
            return None, "文字过长，请控制在 5000 字以内"

        rate_str = f"{int(rate):+d}%"
        pitch_str = f"{int(pitch):+d}Hz"
        volume_str = f"{int(volume):+d}%"

        try:
            communicate = edge_tts.Communicate(
                text,
                voice or DEFAULT_VOICE,
                rate=rate_str,
                pitch=pitch_str,
                volume=volume_str,
            )
            audio_data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]

            if not audio_data:
                return None, "生成失败，未返回音频数据"
            return audio_data, None
        except Exception as e:
            return None, f"生成出错: {str(e)}"

    # ========== 网页 ==========
    @web_app.get("/", response_class=HTMLResponse)
    async def index():
        voice_options = "".join(
            [f'<option value="{v}">{k}</option>' for k, v in VOICES.items()]
        )
        return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>文本转语音 · edge-tts</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f7fa; color: #1a1a1a; line-height: 1.6; padding: 24px 16px;
        }}
        .container {{
            max-width: 720px; margin: 0 auto; background: #fff; border-radius: 16px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.06); padding: 32px;
        }}
        h1 {{ font-size: 1.75rem; font-weight: 700; margin-bottom: 8px; }}
        .subtitle {{ color: #666; font-size: 0.95rem; margin-bottom: 28px; }}
        label {{ display: block; font-weight: 600; font-size: 0.9rem; margin-bottom: 8px; }}
        textarea {{
            width: 100%; height: 160px; padding: 14px; font-size: 16px;
            border: 1px solid #ddd; border-radius: 10px; resize: vertical;
        }}
        textarea:focus {{ outline: none; border-color: #2563eb; box-shadow: 0 0 0 3px rgba(37,99,235,0.12); }}
        .row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 20px; }}
        @media (max-width: 600px) {{ .row {{ grid-template-columns: 1fr; }} }}
        select, input[type="range"] {{
            width: 100%; padding: 10px 12px; font-size: 15px;
            border: 1px solid #ddd; border-radius: 8px; background: #fff;
        }}
        .range-value {{ font-size: 0.85rem; color: #666; margin-top: 4px; }}
        button {{
            width: 100%; margin-top: 24px; padding: 14px; font-size: 16px; font-weight: 600;
            color: #fff; background: #2563eb; border: none; border-radius: 10px; cursor: pointer;
        }}
        button:hover {{ background: #1d4ed8; }}
        button:disabled {{ background: #93c5fd; cursor: not-allowed; }}
        .result {{ margin-top: 28px; padding-top: 24px; border-top: 1px solid #eee; display: none; }}
        .result.show {{ display: block; }}
        audio {{ width: 100%; margin-bottom: 12px; }}
        .download-btn {{
            display: inline-block; padding: 10px 18px; background: #10b981; color: #fff;
            text-decoration: none; border-radius: 8px; font-weight: 500; font-size: 14px;
        }}
        .error {{ margin-top: 16px; padding: 12px; background: #fef2f2; color: #b91c1c; border-radius: 8px; display: none; }}
        .error.show {{ display: block; }}
        .loading {{ text-align: center; color: #666; margin-top: 16px; display: none; }}
        .loading.show {{ display: block; }}
        .api-tip {{ margin-top: 24px; padding: 12px; background: #f0f9ff; border-radius: 8px; font-size: 13px; color: #0369a1; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>文本转语音</h1>
        <p class="subtitle">基于微软 edge-tts · 免费 · 支持网页 + API 调用</p>

        <form id="ttsForm">
            <label for="text">输入文字</label>
            <textarea id="text" name="text" placeholder="在这里输入要转换的文字..." required></textarea>

            <div class="row">
                <div>
                    <label for="voice">选择声音</label>
                    <select id="voice" name="voice">{voice_options}</select>
                </div>
                <div>
                    <label for="rate">语速 <span id="rateValue">+0%</span></label>
                    <input type="range" id="rate" name="rate" min="-50" max="50" value="0" step="5">
                    <div class="range-value">-50% ~ +50%</div>
                </div>
            </div>

            <div class="row" style="margin-top: 16px;">
                <div>
                    <label for="pitch">音调 <span id="pitchValue">+0Hz</span></label>
                    <input type="range" id="pitch" name="pitch" min="-50" max="50" value="0" step="5">
                </div>
                <div>
                    <label for="volume">音量 <span id="volumeValue">+0%</span></label>
                    <input type="range" id="volume" name="volume" min="-50" max="50" value="0" step="5">
                </div>
            </div>

            <button type="submit" id="submitBtn">生成语音</button>
        </form>

        <div class="loading" id="loading">正在生成，请稍候...</div>
        <div class="error" id="error"></div>
        <div class="result" id="result">
            <audio id="audio" controls></audio><br>
            <a id="download" class="download-btn" download="tts.mp3">下载 MP3</a>
        </div>

        <div class="api-tip">
            <strong>API 调用：</strong> POST /api/tts<br>
            参数：text（必填）、voice、rate、pitch、volume<br>
            返回：audio/mpeg（MP3）
        </div>
    </div>

    <script>
        const rateSlider = document.getElementById('rate');
        const pitchSlider = document.getElementById('pitch');
        const volumeSlider = document.getElementById('volume');
        rateSlider.oninput = () => document.getElementById('rateValue').textContent = (rateSlider.value >= 0 ? '+' : '') + rateSlider.value + '%';
        pitchSlider.oninput = () => document.getElementById('pitchValue').textContent = (pitchSlider.value >= 0 ? '+' : '') + pitchSlider.value + 'Hz';
        volumeSlider.oninput = () => document.getElementById('volumeValue').textContent = (volumeSlider.value >= 0 ? '+' : '') + volumeSlider.value + '%';

        document.getElementById('ttsForm').addEventListener('submit', async (e) => {{
            e.preventDefault();
            const btn = document.getElementById('submitBtn');
            const loading = document.getElementById('loading');
            const errorBox = document.getElementById('error');
            const result = document.getElementById('result');
            const audio = document.getElementById('audio');
            const download = document.getElementById('download');

            btn.disabled = true;
            loading.classList.add('show');
            errorBox.classList.remove('show');
            result.classList.remove('show');

            const formData = new FormData();
            formData.append('text', document.getElementById('text').value);
            formData.append('voice', document.getElementById('voice').value);
            formData.append('rate', rateSlider.value);
            formData.append('pitch', pitchSlider.value);
            formData.append('volume', volumeSlider.value);

            try {{
                const res = await fetch('/api/tts', {{ method: 'POST', body: formData }});
                if (!res.ok) throw new Error(await res.text() || '生成失败');
                const blob = await res.blob();
                const url = URL.createObjectURL(blob);
                audio.src = url;
                download.href = url;
                result.classList.add('show');
                audio.play();
            }} catch (err) {{
                errorBox.textContent = err.message || '生成失败，请稍后重试';
                errorBox.classList.add('show');
            }} finally {{
                btn.disabled = false;
                loading.classList.remove('show');
            }}
        }});
    </script>
</body>
</html>
"""

    # ========== API ==========
    @web_app.post("/api/tts")
    async def api_tts(
        text: str = Form(...),
        voice: str = Form(DEFAULT_VOICE),
        rate: str = Form("0"),
        pitch: str = Form("0"),
        volume: str = Form("0"),
    ):
        audio_data, err = await generate_audio(text, voice, rate, pitch, volume)
        if err:
            return HTMLResponse(err, status_code=400)

        return StreamingResponse(
            io.BytesIO(audio_data),
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": "inline; filename=tts.mp3",
                "Cache-Control": "no-cache",
            },
        )

    # 兼容旧网页路径
    @web_app.post("/tts")
    async def tts_compat(
        text: str = Form(...),
        voice: str = Form(DEFAULT_VOICE),
        rate: str = Form("0"),
        pitch: str = Form("0"),
        volume: str = Form("0"),
    ):
        return await api_tts(text, voice, rate, pitch, volume)

    return web_app

@app.function(
    image=image,
    timeout=120,
    scaledown_window=30,
)
@modal.asgi_app()
def fastapi_app():
    return create_web_app()
