import os
import re
import signal
import subprocess
import threading
import time
import socket
import modal

# =============================================================================
# SeaweedFS on Modal —— 服务端加密版（SSE-S3，主密钥来自 Modal Secret）
#
# 目标：只拿到 Modal Volume 的人看不到文件内容；只有通过本服务（S3 接口，
#       且 Secret 里的主密钥正确）才能读出明文。
#
# 做法：
#   1. 主密钥放 Modal Secret，容器启动时以环境变量 WEED_S3_SSE_KEY 注入，不写入 Volume。
#   2. 启动后给每个桶设置"默认加密 AES256"，客户端不带加密头也会被加密。
#   3. 启动后自检：上传不带加密头的测试对象，确认服务端标记为 AES256，否则拒绝服务。
#   4. 默认只对外暴露 S3 端口。可用 EXPOSE 临时切到 filer / master 查看数据，
#      但这两个页面没有登录，且 Filer 上传不经过 S3 层、可能绕过加密，见配置区说明。
#   5. 写成类：enter 里启动 weed，exit 里先让 weed 正常退出，再提交 Volume。
#
# 【第一次部署，我没能验证、需要你看构建和日志的地方】
#   A) 官方镜像 ghcr.io/seaweedfs/seaweedfs:4.55.0 在 Modal 构建的第 0 步就失败：
#      "parsing skopeo inspect config: skopeo inspect returned non-zero status"。
#      这发生在 add_python 之前，是 Modal 读取该镜像配置时出错，原因我不清楚，
#      所以 Alpine 能否配合 add_python 这个问题仍然没有答案。
#      现在默认用 IMAGE_SOURCE="debian"（下载官方预编译二进制）。
#   B) 官方镜像的入口脚本我没看到内容，这里用 .entrypoint([]) 清掉，避免它拦截 Modal 的启动命令。
#   C) weed server 的参数只用了官方文档出现过的：-dir -ip.bind -filer -s3
#      -volume.max -master.volumeSizeLimitMB，端口全部用默认值。
#   D) Modal 的 Web 函数单次 HTTP 请求有 150 秒上限，大文件上传/下载可能失败，
#      部署后请按文件末尾的测试步骤实测。
#   E) startup_timeout 参数需要较新的 modal 客户端（1.1.4+）。如果报
#      unexpected keyword argument 'startup_timeout'，升级 modal 或删掉那一行。
# =============================================================================

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "seaweedfs"
VOLUME_NAME = "seaweedfs-data"

# 镜像来源：
#   "official" -> 官方镜像 ghcr.io/seaweedfs/seaweedfs:<版本>（Alpine）
#   "debian"   -> Debian 基础镜像 + 下载官方预编译二进制（official 构建失败时用）
IMAGE_SOURCE = "debian"   # 官方镜像在 Modal 的 skopeo 检查步骤就失败了（见文件开头说明），改用 debian

# 固定版本，不用 latest（SSE 相关代码更新频繁，新版本可能改变行为）
SEAWEED_VERSION = "4.55.0"
OFFICIAL_IMAGE = f"ghcr.io/seaweedfs/seaweedfs:{SEAWEED_VERSION}"

# 仅 IMAGE_SOURCE="debian" 时使用。下载地址里的标签格式和资产名是我凭印象写的，
# 构建报错请到 GitHub Releases 核对。
DEBIAN_BASE_IMAGE = "debian:bookworm-slim"

# 二进制下载用的 Release 标签。
#   "latest"：先用它保证能构建成功。构建日志里 `weed version` 会打印实际版本号，
#             确认后再把这里改成那个版本对应的标签固定下来。
#   具体标签：我不确定 Release 标签是 "4.55.0" 还是 "4.55" 这种格式，
#             写错会 404，构建报错时请到 GitHub Releases 页面核对。
BINARY_TAG = "latest"
if BINARY_TAG == "latest":
    DOWNLOAD_URL = "https://github.com/seaweedfs/seaweedfs/releases/latest/download/linux_amd64.tar.gz"
else:
    DOWNLOAD_URL = (
        f"https://github.com/seaweedfs/seaweedfs/releases/download/{BINARY_TAG}/linux_amd64.tar.gz"
    )

# ---------- 对外暴露哪个服务（Modal 一个函数只能暴露一个端口） ----------
#   "s3"     -> S3 API（日常使用，有凭据认证 + 默认加密）          【默认，平时就用这个】
#   "filer"  -> Filer 网页/HTTP（浏览文件、检查数据）
#   "master" -> Master 网页（集群状态、volume 信息）
#
# 【filer / master 的风险，请看清楚再开】
#   - 这两个端口没有登录，知道地址的人都能访问。
#   - Filer 能直接上传和删除文件；我推断通过 Filer 上传不经过 S3 层、不会触发 SSE，
#     文件可能以明文写进 Volume（没实测）。所以用 filer 模式时只看、不要往里传文件。
#   - 用完请改回 "s3" 并重新部署，让公网地址恢复到只有 S3。
# EXPOSE = "s3"
EXPOSE = "filer"

# 开启 filer / master 前必须手动把它改成 True，防止不小心把无登录页面部署到公网。
ALLOW_UNAUTHENTICATED_UI = True

# SeaweedFS 各组件的默认端口（一般不用改）
S3_PORT = 8333
FILER_PORT = 8888
MASTER_PORT = 9333

PORT_MAP = {"s3": S3_PORT, "filer": FILER_PORT, "master": MASTER_PORT}
if EXPOSE not in PORT_MAP:
    raise ValueError(f"EXPOSE 只能是 {list(PORT_MAP)}，当前为 {EXPOSE!r}")
if EXPOSE != "s3" and not ALLOW_UNAUTHENTICATED_UI:
    raise ValueError(
        f"EXPOSE={EXPOSE!r} 会把没有登录的页面暴露到公网。"
        "确认要开启，请先把 ALLOW_UNAUTHENTICATED_UI 改成 True；平时请用 EXPOSE = 's3'。"
    )
WEB_PORT = PORT_MAP[EXPOSE]

# ---------- 启动时自动创建的 bucket（逗号分隔） ----------
# 桶名规则：3-63 位，只能用小写字母、数字、连字符
SEAWEED_BUCKETS = "assets,upload"

# 数据目录（挂载 Volume）。weed 的数据与元数据默认都放在 -dir 下面
DATA_DIR = "/data"
WEED_DIR = f"{DATA_DIR}/seaweed"

# 单个 volume 文件大小上限（MB）。小一点可以减少预占空间
VOLUME_SIZE_LIMIT_MB = 128

# 启动后是否做"加密自检"。True：不通过就拒绝启动（更安全）；
# False：只打印警告（若你的版本 head_object 不返回加密头导致误判，可临时关闭）
STRICT_ENCRYPTION_CHECK = True

MAX_CONTAINERS = 1          # 本地文件存储，必须单实例，避免多实例写冲突
MAX_CONCURRENT_INPUTS = 32
TIMEOUT_SECONDS = 3600
SCALEDOWN_WINDOW_SECONDS = 180
STARTUP_TIMEOUT_SECONDS = 300       # 容器启动（含建桶、自检）的时间上限
WEB_STARTUP_TIMEOUT_SECONDS = 120   # 等待对外端口可用的时间上限

CPU = 0.5
MEMORY_MB = 512             # 我估计的值，没实测；部署后看 Modal 面板里的实际占用再调

# Volume 自动提交间隔（秒）
VOLUME_COMMIT_INTERVAL_SECONDS = 30

# 退出时等待 weed 正常停止的秒数。Modal 在 SIGTERM 后给 30 秒，这里留出提交 Volume 的时间
EXIT_GRACE_SECONDS = 20

# Modal Secret 名称，里面需要三项：
#   SEAWEED_ACCESS_KEY   S3 Access Key
#   SEAWEED_SECRET_KEY   S3 Secret Key
#   SEAWEED_SSE_KEY      服务端加密主密钥（至少 32 个字符，建议 openssl rand -hex 32）
#                        ！丢了就无法解密已有数据，请另外安全备份！
#                        ！从第一次启动起就必须设置，不要先裸跑再补！
SECRET_NAME = "seaweedfs-secret"
# ====================================================


# ====================================================
# 1. 持久化数据卷
# ====================================================
vol = modal.Volume.from_name(
    VOLUME_NAME,
    create_if_missing=True,
)

# ====================================================
# 2. 镜像
# ====================================================
if IMAGE_SOURCE == "official":
    # 官方镜像：清掉它的 ENTRYPOINT，让 Modal 自己的启动命令正常运行
    image = (
        modal.Image.from_registry(OFFICIAL_IMAGE, add_python="3.12")
        .entrypoint([])
        .pip_install("boto3")
    )
elif IMAGE_SOURCE == "debian":
    image = (
        modal.Image.from_registry(DEBIAN_BASE_IMAGE, add_python="3.12")
        .apt_install("curl", "ca-certificates", "tzdata")
        .run_commands(
            f"curl -fsSL {DOWNLOAD_URL} -o /tmp/weed.tar.gz",
            "tar -xzf /tmp/weed.tar.gz -C /usr/local/bin",
            "chmod +x /usr/local/bin/weed",
            "rm /tmp/weed.tar.gz",
            "weed version",
        )
        .pip_install("boto3")
    )
else:
    raise ValueError(f"IMAGE_SOURCE 只能是 'official' 或 'debian'，当前为 {IMAGE_SOURCE!r}")

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 3. 辅助函数
# ====================================================
def wait_port(port: int, process: subprocess.Popen, timeout: float) -> bool:
    """等待端口可连接；weed 进程提前退出则抛错。"""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"weed 进程异常退出，退出代码：{code}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def parse_buckets(raw: str) -> list:
    """解析并校验桶名。"""
    names = [b.strip() for b in raw.split(",") if b.strip()]
    if not names:
        raise ValueError("SEAWEED_BUCKETS 不能为空")
    for name in names:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", name):
            raise ValueError(f"桶名不合法：{name!r}（3-63 位，仅小写字母/数字/连字符）")
    return names


def make_s3_client(access_key: str, secret_key: str):
    """连接容器内的 S3 网关（走 127.0.0.1，不经过公网）。"""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{S3_PORT}",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1},
        ),
    )


def setup_buckets_with_default_encryption(s3, buckets: list, process: subprocess.Popen):
    """创建桶，并给每个桶设置"默认加密 AES256（SSE-S3）"。

    默认加密只对设置之后的新上传生效，不会回头加密旧文件，
    所以本函数必须在对外服务、任何数据写入之前完成。
    """
    from botocore.exceptions import ClientError

    encryption_rule = {
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
    }

    for name in buckets:
        last_error = None
        for _ in range(30):   # S3 网关刚启动时可能还没完全就绪，重试
            if process.poll() is not None:
                raise RuntimeError("weed 进程在建桶期间退出")
            try:
                try:
                    s3.create_bucket(Bucket=name)
                    print(f"bucket 已创建：{name}", flush=True)
                except ClientError as error:
                    code = error.response.get("Error", {}).get("Code", "")
                    if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                        print(f"bucket 已存在：{name}", flush=True)
                    else:
                        raise
                # 每次启动都重新设置一遍，幂等
                s3.put_bucket_encryption(
                    Bucket=name,
                    ServerSideEncryptionConfiguration=encryption_rule,
                )
                print(f"bucket 默认加密已设置（AES256）：{name}", flush=True)
                last_error = None
                break
            except Exception as error:
                last_error = error
                time.sleep(2)
        if last_error is not None:
            raise RuntimeError(f"bucket {name} 初始化失败：{last_error}")


def verify_encryption(s3, bucket: str) -> bool:
    """加密自检：上传一个不带任何加密头的对象，看服务端是否仍标记为 AES256。

    这只能证明"默认加密被应用了"。Volume 里是不是真的没有明文，
    要按文件末尾的"人工验证步骤"下载 Volume 搜索明文标记。
    """
    key = ".encryption-selftest"
    s3.put_object(Bucket=bucket, Key=key, Body=b"SELFTEST-PLAINTEXT-MARKER-12345")
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        return head.get("ServerSideEncryption") == "AES256"
    finally:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as error:
            print(f"自检对象删除失败（可手动删除 {bucket}/{key}）：{error}", flush=True)


# ====================================================
# 4. 服务类：enter 启动 weed，web_server 暴露端口，exit 优雅退出
# ====================================================
@app.cls(
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes={DATA_DIR: vol},
    timeout=TIMEOUT_SECONDS,
    startup_timeout=STARTUP_TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    secrets=[
        modal.Secret.from_name(SECRET_NAME),
    ],
)
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS,
)
class SeaweedFS:

    @modal.enter()
    def start(self):
        """容器启动：检查 Secret -> 启动 weed -> 建桶并开启默认加密 -> 自检。"""
        print("======================================", flush=True)
        print(f"正在启动 SeaweedFS {SEAWEED_VERSION}（SSE-S3 加密）...", flush=True)
        print("======================================", flush=True)

        self.process = None
        self.stop_event = threading.Event()

        # ---------- 读取并检查 Secret ----------
        access_key = os.environ.get("SEAWEED_ACCESS_KEY", "").strip()
        secret_key = os.environ.get("SEAWEED_SECRET_KEY", "").strip()
        sse_key = os.environ.get("SEAWEED_SSE_KEY", "").strip()

        # 没有 S3 凭据，SeaweedFS 可能允许匿名访问，所以强制要求
        if not access_key or not secret_key:
            raise ValueError(
                "SEAWEED_ACCESS_KEY / SEAWEED_SECRET_KEY 未设置，请在 Modal Secret 中配置。"
            )
        # 没有主密钥就不能加密，宁可不启动，也不让数据明文落盘
        if len(sse_key) < 32:
            raise ValueError(
                "SEAWEED_SSE_KEY 未设置或少于 32 个字符。"
                "请用 `openssl rand -hex 32` 生成，写入 Modal Secret，并另外安全备份。"
            )

        buckets = parse_buckets(SEAWEED_BUCKETS)

        # ---------- 准备目录 ----------
        os.makedirs(WEED_DIR, exist_ok=True)

        # ---------- 配置环境变量 ----------
        env = os.environ.copy()
        env["TZ"] = os.environ.get("TZ", "Asia/Shanghai")

        # S3 管理员凭据：官方文档中 weed 通过这两个环境变量读取
        env["AWS_ACCESS_KEY_ID"] = access_key
        env["AWS_SECRET_ACCESS_KEY"] = secret_key

        # SSE-S3 主密钥（对应 security.toml 里的 [s3.sse] key）：
        #   - 任意字符串，由 HKDF 派生 256 位密钥
        #   - 只设置 KEY，不要同时设置 KEK（官方 PR 说明两者同时存在网关会拒绝启动）
        #   - 只存在于环境变量里，不写入 Volume
        env["WEED_S3_SSE_KEY"] = sse_key
        env.pop("WEED_S3_SSE_KEK", None)

        # 其余 SEAWEED_* 变量不传给 weed 进程
        for name in ("SEAWEED_ACCESS_KEY", "SEAWEED_SECRET_KEY", "SEAWEED_SSE_KEY"):
            env.pop(name, None)

        # 日志里只打印长度，绝不打印密钥内容
        print(f"Access Key 长度：{len(access_key)}，Secret Key 长度：{len(secret_key)}", flush=True)
        print(f"SSE 主密钥长度：{len(sse_key)}（来自 Secret，不落盘）", flush=True)
        print(f"BUCKETS：{buckets}", flush=True)
        print(f"数据目录：{WEED_DIR}", flush=True)
        print(f"对外暴露：{EXPOSE}（端口 {WEB_PORT}）", flush=True)
        if EXPOSE != "s3":
            print("警告：当前对外暴露的是无登录页面，只用来查看，用完请改回 EXPOSE = 's3'。", flush=True)
        print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)

        # ---------- 启动 weed server ----------
        # 刻意不开 -s3.encryptVolumeData：有 issue 显示它开启时桶默认加密不会生效，
        # 这里只用 SSE-S3。
        cmd = [
            "weed", "server",
            f"-dir={WEED_DIR}",
            "-ip.bind=0.0.0.0",
            f"-master.volumeSizeLimitMB={VOLUME_SIZE_LIMIT_MB}",
            "-volume.max=0",        # 0 = 按剩余磁盘自动决定
            "-filer",
            "-s3",
        ]
        self.process = subprocess.Popen(cmd, env=env, cwd=DATA_DIR)
        print(f"weed 进程已启动，PID：{self.process.pid}", flush=True)

        # ---------- 后台定期提交 Volume ----------
        threading.Thread(target=self._commit_loop, daemon=True).start()

        # ---------- 等待 S3 端口就绪 ----------
        if not wait_port(S3_PORT, self.process, STARTUP_TIMEOUT_SECONDS):
            self._stop_weed()
            raise RuntimeError(f"S3 启动超时（端口 {S3_PORT}），请检查 Modal 容器日志")
        print(f"S3 端口已就绪：{S3_PORT}", flush=True)

        # ---------- 建桶 + 设置默认加密 ----------
        s3 = make_s3_client(access_key, secret_key)
        try:
            setup_buckets_with_default_encryption(s3, buckets, self.process)

            # ---------- 加密自检 ----------
            if verify_encryption(s3, buckets[0]):
                print("加密自检通过：未带加密头的上传被标记为 AES256", flush=True)
            else:
                message = (
                    "加密自检未通过：未带加密头的上传没有被标记为 AES256。"
                    "可能是 SSE 密钥未生效、版本差异，或 head_object 不返回该头。"
                )
                if STRICT_ENCRYPTION_CHECK:
                    raise RuntimeError(message + "已拒绝对外服务。")
                print("警告：" + message, flush=True)
        except Exception:
            self._stop_weed()
            raise

        print("SeaweedFS 初始化完成", flush=True)

    @modal.web_server(WEB_PORT, startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS)
    def serve(self):
        """把容器内的 S3 端口暴露到公网。weed 已在 enter 里启动，这里什么都不用做。"""
        pass

    @modal.exit()
    def stop(self):
        """容器关闭：先让 weed 正常退出（刷新元数据），再提交 Volume。"""
        print("收到关闭信号，正在停止 weed...", flush=True)
        self._stop_weed()
        try:
            vol.commit()
            print("Volume 已提交", flush=True)
        except Exception as error:
            print(f"退出时 Volume 提交失败：{error}", flush=True)

    # ---------- 内部方法 ----------
    def _commit_loop(self):
        """后台线程：定期提交 Volume，直到收到停止事件。"""
        while not self.stop_event.wait(VOLUME_COMMIT_INTERVAL_SECONDS):
            try:
                vol.commit()
            except Exception as error:
                print(f"Volume 提交失败：{error}", flush=True)

    def _stop_weed(self):
        """给 weed 发 SIGTERM，等待它退出；超时才强杀。"""
        self.stop_event.set()
        process = getattr(self, "process", None)
        if process is None or process.poll() is not None:
            return
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=EXIT_GRACE_SECONDS)
            print("weed 已正常退出", flush=True)
        except subprocess.TimeoutExpired:
            print("weed 未在限定时间内退出，强制结束", flush=True)
            process.kill()
            process.wait()


# =============================================================================
# 部署步骤
# =============================================================================
#   1) 生成主密钥并创建 Secret（主密钥请另外备份，丢失则数据无法解密）：
#        openssl rand -hex 32
#        modal secret create seaweedfs-secret \
#            SEAWEED_ACCESS_KEY=你的AK \
#            SEAWEED_SECRET_KEY=你的SK \
#            SEAWEED_SSE_KEY=上一步生成的64位hex
#
#   2) 部署：
#        modal deploy seaweedfs.py
#      终端会打印 serve 方法的 URL，形如 https://<工作区>--seaweedfs-seaweedfs-serve.modal.run
#      （以终端实际输出为准）。第一次访问要冷启动，可能要等几十秒。
#
#   3) 看日志：
#        modal app logs seaweedfs
#      应依次看到：weed 进程已启动 -> S3 端口已就绪 -> bucket 已创建 ->
#      bucket 默认加密已设置 -> 加密自检通过 -> SeaweedFS 初始化完成。
#
# =============================================================================
# 部署后的测试（建议按顺序做）
# =============================================================================
#   先配置 aws cli（region 随意，开启 path-style）：
#     export AWS_ACCESS_KEY_ID=你的AK
#     export AWS_SECRET_ACCESS_KEY=你的SK
#     export AWS_DEFAULT_REGION=us-east-1
#     export EP=https://你的modal地址
#
#   T1 基本读写 + 加密标记（不带 --sse，验证默认加密是否生效）：
#     echo "SELFTEST-PLAINTEXT-MARKER hello" > t.txt
#     aws --endpoint-url $EP s3 cp t.txt s3://assets/t.txt
#     aws --endpoint-url $EP s3api head-object --bucket assets --key t.txt
#       -> 返回里应有 "ServerSideEncryption": "AES256"
#     aws --endpoint-url $EP s3 cp s3://assets/t.txt - 
#       -> 能读出明文内容
#
#   T2 Volume 里没有明文（最关键）：
#     等 30~60 秒让 Volume 提交，然后：
#     modal volume get seaweedfs-data /seaweed ./vol-dump
#     grep -rl "SELFTEST-PLAINTEXT-MARKER" ./vol-dump     # 应该一无所获
#     （搜得到说明没加密）
#
#   T3 大文件（验证 Modal 150 秒单请求限制的实际影响）：
#     dd if=/dev/urandom of=big.bin bs=1M count=300
#     time aws --endpoint-url $EP s3 cp big.bin s3://upload/big.bin
#     aws --endpoint-url $EP s3 cp s3://upload/big.bin big2.bin && cmp big.bin big2.bin
#     再用你实际要用的客户端（OpenList / rclone）测一次，慢速网络下更容易触发。
#
#   T4 重启后数据仍在：
#     等容器缩容（闲置 SCALEDOWN_WINDOW_SECONDS 秒），再读取 T1 的文件，应当仍然可读。
#     日志里应看到"weed 已正常退出"和"Volume 已提交"。
#
#   T5 密钥确实起作用：
#     把 Secret 里的 SEAWEED_SSE_KEY 临时改成另一个值并重新部署，读取旧文件应当失败；
#     验证完立刻改回原值，确认旧文件恢复可读。
#
# 使用注意：
#   - 不要用 `weed mount` 读取 SSE-S3 对象（有 issue 报告读到的字节不正确）。
#   - 不要通过 Filer(8888) 上传：它不经过 S3 层，不会触发 SSE。本脚本不暴露该端口。
#   - 文件名、目录结构、对象大小不会被加密。
#   - 有 S3 凭据的人读到的是自动解密后的明文；能读取 Modal Secret 或进入容器的人
#     也能拿到主密钥，本方案防的是"Volume 被拷走/被他人访问"。
# =============================================================================
