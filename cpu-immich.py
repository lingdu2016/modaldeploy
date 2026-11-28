import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import modal

# ====================== 配置区（所有可调参数都在这里） ======================
APP_NAME = "immich"

# 两个 Volume（都不要删）：
#   immich-data  -> 照片/视频
#   immich-state -> Redis 队列快照 + Postgres 快照/备份
MEDIA_VOLUME_NAME = "immich-data"
STATE_VOLUME_NAME = "immich-state"

# Immich 版本，server 与 机器学习 App 必须写成同一个值。
# "v3" 会跟随 v3.x 最新版；想完全固定就写具体版本（如 "v3.0.2"）。
IMMICH_VERSION = "v3"
IMMICH_IMAGE = f"ghcr.io/immich-app/immich-server:{IMMICH_VERSION}"

# 构建期安装的 Postgres 主版本 和 VectorChord 版本
# （Immich 要求 pgvector >=0.7 <0.9，VectorChord >=0.3 <2.0）
PG_MAJOR = 14
VCHORD_VERSION = "0.4.3"

# 端口
SERVER_PORT = 2283
REDIS_PORT = 6379
PG_PORT = 5432

# ---------- 照片/视频存储 ----------
#   "volume" -> Modal Volume（适合几百 GB 以内）
#   "r2"     -> CloudBucketMount 挂载 R2/S3（需实测小文件性能）
STORAGE_MODE = "volume"

DATA_DIR = "/data"            # 媒体目录
STATE_DIR = "/state"          # 状态目录（Redis/Postgres 持久化文件）

# 仅 STORAGE_MODE = "r2" 时使用
R2_BUCKET_NAME = "immich-photos"
R2_ENDPOINT_URL = "https://<account_id>.r2.cloudflarestorage.com"
# 该 Secret 里需要：AWS_ACCESS_KEY_ID、AWS_SECRET_ACCESS_KEY
R2_SECRET_NAME = "immich-r2-secret"

# ---------- Redis ----------
REDIS_SAVE_SECONDS = 30       # 距上次快照 N 秒且有写入，就落盘一次

# ---------- Postgres ----------
# 数据库运行在容器本地磁盘（快、可靠），持久化靠两层：
#   1) 正常休眠时：停库后把整个数据目录打包存进 Volume（恢复最快）
#   2) 运行期间：每隔一段时间 pg_dump 一份逻辑备份（容器被强杀时的兜底）
PG_LOCAL_PARENT = "/var/lib/immich-pg"
PG_DATA_DIR = f"{PG_LOCAL_PARENT}/data"
PG_BIN = f"/usr/lib/postgresql/{PG_MAJOR}/bin"
PG_SHARED_BUFFERS = "256MB"
DUMP_FIRST_DELAY_SECONDS = 120     # 启动后多久做第一次备份
DUMP_INTERVAL_SECONDS = 900        # 之后每隔多久备份一次（15 分钟）

# ---------- 资源与并发 ----------
MIN_CONTAINERS = 0                 # 0 = 允许休眠；要常驻改成 1
MAX_CONTAINERS = 1                 # 必须单副本：Redis/Postgres 都在容器内
MAX_CONCURRENT_INPUTS = 100
TIMEOUT_SECONDS = 86400
SCALEDOWN_WINDOW_SECONDS = 300     # 空闲 5 分钟后休眠
WEB_STARTUP_TIMEOUT_SECONDS = 600  # 含数据库恢复 + Immich 启动，给足时间

# 休眠/关闭时，各进程保存数据的最长等待时间（秒）
EXIT_WAIT_IMMICH = 15
EXIT_WAIT_REDIS = 15
EXIT_WAIT_PG = 30

CPU = (0.5, 2.0)
MEMORY_MB  = (1536, 4096)

# Volume 自动提交间隔（秒）
VOLUME_COMMIT_INTERVAL_SECONDS = 30

TIMEZONE = "Asia/Shanghai"

# Modal Secret 名称。里面所有项都是可选的，但 Secret 本身必须存在，
# 至少放一项即可，例如：
#   IMMICH_MACHINE_LEARNING_URL   机器学习 App 的 Web 地址
#   DB_USERNAME / DB_PASSWORD / DB_DATABASE_NAME   不填则默认 postgres / postgres / immich
#     （数据库只监听容器内 127.0.0.1，外部无法访问；名称只能用字母数字下划线）
SECRET_NAME = "immich-secret"
# ====================================================

PG_STATE_DIR = f"{STATE_DIR}/pg"
REDIS_DIR = f"{STATE_DIR}/redis"
PG_TAR = f"{PG_STATE_DIR}/pgdata.tar"      # 正常休眠时的整体快照
PG_DUMP = f"{PG_STATE_DIR}/immich.dump"    # 运行期间的逻辑备份
PG_FLAG = f"{PG_STATE_DIR}/running.flag"   # 存在 = 上一次没有正常关闭

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


# ====================================================
# 1. 存储
# ====================================================
state_vol = modal.Volume.from_name(STATE_VOLUME_NAME, create_if_missing=True)
volumes_to_commit = [state_vol]
storage_mounts = {STATE_DIR: state_vol}

if STORAGE_MODE == "r2":
    storage_mounts[DATA_DIR] = modal.CloudBucketMount(
        R2_BUCKET_NAME,
        bucket_endpoint_url=R2_ENDPOINT_URL,
        secret=modal.Secret.from_name(R2_SECRET_NAME),
    )
else:
    media_vol = modal.Volume.from_name(MEDIA_VOLUME_NAME, create_if_missing=True)
    storage_mounts[DATA_DIR] = media_vol
    volumes_to_commit.append(media_vol)


# ====================================================
# 2. 镜像：immich-server + Postgres + pgvector + VectorChord + Valkey/Redis
# ====================================================
# 全部在构建期完成，之后由 Modal 缓存，冷启动不会重复安装。
_add_pgdg_repo = (
    "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
    "--no-install-recommends curl ca-certificates gnupg && "
    "(grep -rqs apt.postgresql.org /etc/apt/sources.list /etc/apt/sources.list.d || "
    "( . /etc/os-release && install -d /usr/share/postgresql-common/pgdg && "
    "curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc "
    "-o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc && "
    'echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] '
    'https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" '
    "> /etc/apt/sources.list.d/pgdg.list ))"
)

_install_postgres = (
    "mkdir -p /etc/postgresql-common && "
    "echo 'create_main_cluster = false' > /etc/postgresql-common/createcluster.conf && "
    "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
    f"--no-install-recommends postgresql-{PG_MAJOR} postgresql-{PG_MAJOR}-pgvector"
)

_install_vchord = (
    "ARCH=$(dpkg --print-architecture) && "
    "curl -fsSL -o /tmp/vchord.deb "
    f"https://github.com/tensorchord/VectorChord/releases/download/{VCHORD_VERSION}/"
    f"postgresql-{PG_MAJOR}-vchord_{VCHORD_VERSION}-1_$ARCH.deb && "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y /tmp/vchord.deb && "
    "rm -f /tmp/vchord.deb"
)

_install_redis = (
    "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends valkey-server"
    " || DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends redis-server"
)

image = (
    modal.Image.from_registry(IMMICH_IMAGE, add_python="3.12")
    .entrypoint([])
    .run_commands(
        _add_pgdg_repo,
        _install_postgres,
        _install_vchord,
        _install_redis,
        "rm -rf /var/lib/apt/lists/*",
    )
)

app = modal.App(
    name=APP_NAME,
    image=image,
)


# ====================================================
# 3. 通用辅助函数
# ====================================================
def commit_all_volumes():
    """把容器内写入的数据提交到所有 Volume。"""
    for volume in volumes_to_commit:
        try:
            volume.commit()
        except Exception as error:
            print(f"Volume 提交失败：{error}", flush=True)


def commit_volume_periodically(stopping):
    while not stopping.wait(VOLUME_COMMIT_INTERVAL_SECONDS):
        commit_all_volumes()


def wait_for_port(port, timeout, process, name):
    """等待端口可连接；进程提前退出或超时则抛异常。"""
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"{name} 启动失败，退出代码：{return_code}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                print(f"{name} 已就绪，监听端口：{port}", flush=True)
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"{name} 启动超时，请检查 Modal 容器日志")


def stop_process(process, name, sig=signal.SIGTERM, wait=15):
    """发送信号并等待进程自行退出，超时则强制结束。"""
    if process is None or process.poll() is not None:
        return
    print(f"正在停止 {name}...", flush=True)
    process.send_signal(sig)
    try:
        process.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        print(f"{name} 未在 {wait} 秒内退出，强制结束", flush=True)
        process.kill()


def watchdog(processes, stopping):
    """运行期间任何子进程意外退出，就让容器退出，由 Modal 拉起新容器。
    正常关闭（stopping 已置位）时不触发。"""
    while not stopping.wait(2):
        for name, process in processes.items():
            code = process.poll()
            if code is not None:
                print(f"{name} 意外退出（代码 {code}），容器即将重启", flush=True)
                os._exit(1)


# ====================================================
# 4. Postgres 辅助函数
# ====================================================
PG_ENV = {
    "HOME": PG_LOCAL_PARENT,
    "PATH": os.environ.get("PATH", "/usr/bin:/bin") + f":{PG_BIN}",
    "LANG": "C.UTF-8",
}


def pg_client(binary, user, *args, dbname="postgres", check=True, capture=False):
    """以 root 身份运行 psql / pg_dump / pg_restore 等客户端（走本机 TCP，trust 认证）。"""
    cmd = [f"{PG_BIN}/{binary}", "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", user]
    if binary in ("psql", "pg_dump"):
        cmd += ["-d", dbname]
    cmd += list(args)
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, env=PG_ENV)


def pg_sql(user, sql, dbname="postgres"):
    result = pg_client("psql", user, "-v", "ON_ERROR_STOP=1", "-tAc", sql,
                       dbname=dbname, capture=True)
    return result.stdout.strip()


def wait_for_postgres(timeout, process, user):
    """pg_isready 返回 0 才算真正可以接受连接（崩溃恢复期间会拒绝）。"""
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"Postgres 启动失败，退出代码：{return_code}")
        ready = subprocess.run(
            [f"{PG_BIN}/pg_isready", "-h", "127.0.0.1", "-p", str(PG_PORT), "-U", user],
            capture_output=True, env=PG_ENV,
        )
        if ready.returncode == 0:
            print("Postgres 已就绪", flush=True)
            return
        time.sleep(1)
    raise RuntimeError("Postgres 启动超时，请检查 Modal 容器日志")


def start_postgres_process(user):
    process = subprocess.Popen(
        [
            f"{PG_BIN}/postgres",
            "-D", PG_DATA_DIR,
            "-c", "listen_addresses=127.0.0.1",
            "-c", f"port={PG_PORT}",
            "-c", "unix_socket_directories=/tmp",
            "-c", "shared_preload_libraries=vchord.so",
            "-c", f"shared_buffers={PG_SHARED_BUFFERS}",
            "-c", "logging_collector=off",
        ],
        user="postgres", group="postgres", extra_groups=[],
        cwd="/tmp", env=PG_ENV,
    )
    wait_for_postgres(180, process, user)
    return process


def bring_up_postgres(mode, user, db):
    """按 mode 准备本地数据目录并启动 Postgres。
    mode: "tar"（解压整体快照）/ "dump"（新建集群后恢复逻辑备份）/ "init"（全新数据库）"""
    shutil.rmtree(PG_LOCAL_PARENT, ignore_errors=True)
    os.makedirs(PG_LOCAL_PARENT, exist_ok=True)
    shutil.chown(PG_LOCAL_PARENT, "postgres", "postgres")

    if mode == "tar":
        subprocess.run(["tar", "-xf", PG_TAR, "-C", PG_LOCAL_PARENT], check=True)
        subprocess.run(["chown", "-R", "postgres:postgres", PG_DATA_DIR], check=True)
        os.chmod(PG_DATA_DIR, 0o700)
    else:
        subprocess.run(
            [f"{PG_BIN}/initdb", "-D", PG_DATA_DIR, "-U", user, "--auth=trust",
             "--encoding=UTF8", "--locale=C.UTF-8", "--data-checksums"],
            user="postgres", group="postgres", extra_groups=[],
            cwd="/tmp", env=PG_ENV, check=True,
        )

    process = start_postgres_process(user)
    try:
        if mode == "dump":
            print("正在从逻辑备份恢复数据库...", flush=True)
            result = pg_client("pg_restore", user, "--create", "-d", "postgres", PG_DUMP, check=False)
            if result.returncode != 0:
                print(f"pg_restore 返回 {result.returncode}（可能只是警告），继续检查数据库是否存在", flush=True)

        exists = pg_sql(user, f"SELECT 1 FROM pg_database WHERE datname='{db}'")
        if exists != "1":
            pg_sql(user, f'CREATE DATABASE "{db}"')
            print(f"已创建数据库 {db}", flush=True)
    except Exception:
        stop_process(process, "Postgres", sig=signal.SIGINT, wait=EXIT_WAIT_PG)
        raise
    return process


def prepare_postgres(user, db):
    """决定恢复来源并启动 Postgres。
    - 上次没正常关闭（flag 存在）：优先用较新的逻辑备份
    - 上次正常关闭：优先用整体快照（最快）
    任何一种失败，就退到下一种，最后才是全新数据库。"""
    os.makedirs(PG_STATE_DIR, exist_ok=True)
    for leftover in (PG_TAR + ".tmp", PG_DUMP + ".tmp"):
        if os.path.exists(leftover):
            os.remove(leftover)

    has_tar = os.path.exists(PG_TAR)
    has_dump = os.path.exists(PG_DUMP)
    dirty = os.path.exists(PG_FLAG)

    candidates = []
    order = ["dump", "tar"] if dirty else ["tar", "dump"]
    for mode in order:
        if (mode == "tar" and has_tar) or (mode == "dump" and has_dump):
            candidates.append(mode)
    candidates.append("init")

    print(
        f"数据库恢复来源候选：{candidates}"
        f"（上次{'未正常关闭' if dirty else '正常关闭或首次启动'}）",
        flush=True,
    )

    for mode in candidates:
        try:
            print(f"尝试方式：{mode}", flush=True)
            started = time.monotonic()
            process = bring_up_postgres(mode, user, db)
            print(f"数据库就绪，方式：{mode}，耗时 {time.monotonic() - started:.1f} 秒", flush=True)
            return process
        except Exception as error:
            print(f"方式 {mode} 失败：{error}", flush=True)
    raise RuntimeError("所有数据库恢复方式都失败了，请检查日志和 /state/pg 下的文件")


def dump_database(user, db):
    """生成逻辑备份：先写临时文件，成功后原子替换，再提交 Volume。"""
    tmp = PG_DUMP + ".tmp"
    try:
        pg_client("pg_dump", user, "-Fc", "--create", "-f", tmp, dbname=db)
        os.replace(tmp, PG_DUMP)
        commit_all_volumes()
        print("数据库逻辑备份完成", flush=True)
    except Exception as error:
        print(f"数据库逻辑备份失败：{error}", flush=True)
        if os.path.exists(tmp):
            os.remove(tmp)


def dump_periodically(stopping, user, db):
    delay = DUMP_FIRST_DELAY_SECONDS
    while not stopping.wait(delay):
        dump_database(user, db)
        delay = DUMP_INTERVAL_SECONDS


def save_postgres_snapshot():
    """Postgres 已完全停止后：打包数据目录、清除 running 标记、提交 Volume。"""
    tmp = PG_TAR + ".tmp"
    started = time.monotonic()
    subprocess.run(["tar", "-cf", tmp, "-C", PG_LOCAL_PARENT, "data"], check=True)
    os.replace(tmp, PG_TAR)
    if os.path.exists(PG_FLAG):
        os.remove(PG_FLAG)
    commit_all_volumes()
    print(f"数据库整体快照已保存，耗时 {time.monotonic() - started:.1f} 秒", flush=True)


def find_start_script():
    """定位 Immich 的启动脚本。v3 在 /usr/src/app/server/bin，旧版本在 /usr/src/app/bin。"""
    candidates = [
        "/usr/src/app/server/bin/start.sh",
        "/usr/src/app/bin/start.sh",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    found = shutil.which("start.sh")
    if found:
        return found
    raise RuntimeError(
        "找不到 Immich 的 start.sh，已尝试：" + "、".join(candidates)
        + "。请在镜像里执行 find / -name start.sh 确认实际路径。"
    )


# ====================================================
# 5. 启动 Postgres + Valkey/Redis + immich-server
# ====================================================
@app.cls(
    min_containers=MIN_CONTAINERS,
    max_containers=MAX_CONTAINERS,
    cpu=CPU,
    memory=MEMORY_MB,
    volumes=storage_mounts,
    timeout=TIMEOUT_SECONDS,
    scaledown_window=SCALEDOWN_WINDOW_SECONDS,
    secrets=[
        modal.Secret.from_name(SECRET_NAME),
    ],
)
@modal.concurrent(
    max_inputs=MAX_CONCURRENT_INPUTS,
)
class Immich:
    @modal.web_server(
        SERVER_PORT,
        startup_timeout=WEB_STARTUP_TIMEOUT_SECONDS,
    )
    def ui(self):
        """在同一个容器里依次启动 Postgres、Redis(Valkey)、Immich Server。"""
        print("======================================", flush=True)
        print("正在启动 Immich（server + redis + postgres）...", flush=True)
        print("======================================", flush=True)

        self.stopping = threading.Event()
        self.pg_process = None
        self.redis_process = None
        self.immich_process = None
        self.pg_ready = False

        # ---------- 读取数据库配置（均有默认值） ----------
        db_user = os.environ.get("DB_USERNAME", "").strip() or "postgres"
        db_password = os.environ.get("DB_PASSWORD", "").strip() or "postgres"
        db_name = os.environ.get("DB_DATABASE_NAME", "").strip() or "immich"
        for label, value in (("DB_USERNAME", db_user), ("DB_DATABASE_NAME", db_name)):
            if not NAME_PATTERN.match(value):
                raise ValueError(f"{label} 只能包含字母、数字和下划线，当前值：{value!r}")
        self.db_user = db_user
        self.db_name = db_name

        # ---------- 准备目录 ----------
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(REDIS_DIR, exist_ok=True)
        os.makedirs(PG_STATE_DIR, exist_ok=True)

        # ---------- 1/3 启动 Postgres ----------
        self.pg_process = prepare_postgres(db_user, db_name)
        self.pg_ready = True

        # 立刻写入「运行中」标记并提交：之后如果被强杀，下次启动就知道要用逻辑备份
        with open(PG_FLAG, "w") as flag_file:
            flag_file.write(str(time.time()))
        commit_all_volumes()

        # ---------- 2/3 启动 Redis(Valkey)：仅本机监听，快照写入 Volume ----------
        redis_bin = shutil.which("valkey-server") or shutil.which("redis-server")
        if not redis_bin:
            raise RuntimeError("镜像里没有找到 valkey-server / redis-server")

        dump_file = os.path.join(REDIS_DIR, "dump.rdb")
        print(
            f"Redis 快照：{'发现已有快照，将恢复队列' if os.path.exists(dump_file) else '无快照，全新启动'}",
            flush=True,
        )

        self.redis_process = subprocess.Popen(
            [
                redis_bin,
                "--bind", "127.0.0.1",
                "--port", str(REDIS_PORT),
                "--dir", REDIS_DIR,
                "--dbfilename", "dump.rdb",
                "--save", str(REDIS_SAVE_SECONDS), "1",
                "--appendonly", "no",
            ]
        )
        print(f"Redis 已启动：{redis_bin}，PID：{self.redis_process.pid}", flush=True)
        wait_for_port(REDIS_PORT, 30, self.redis_process, "Redis")

        # ---------- 3/3 启动 immich-server ----------
        env = os.environ.copy()
        env["TZ"] = os.environ.get("TZ", TIMEZONE)
        env["DB_HOSTNAME"] = "127.0.0.1"
        env["DB_PORT"] = str(PG_PORT)
        env["DB_USERNAME"] = db_user
        env["DB_PASSWORD"] = db_password
        env["DB_DATABASE_NAME"] = db_name
        env.pop("DB_URL", None)        # 避免残留配置覆盖上面的连接参数
        env.pop("DB_SSL_MODE", None)   # 本机连接不走 SSL
        env["REDIS_HOSTNAME"] = "127.0.0.1"
        env["REDIS_PORT"] = str(REDIS_PORT)
        env["IMMICH_MEDIA_LOCATION"] = DATA_DIR
        env["IMMICH_PORT"] = str(SERVER_PORT)
        env["PATH"] = env.get("PATH", "") + ":/usr/src/app/server/bin:/usr/src/app/bin"

        print(f"数据库：127.0.0.1:{PG_PORT}/{db_name}（用户 {db_user}）", flush=True)
        print(f"机器学习地址：{env.get('IMMICH_MACHINE_LEARNING_URL', '(未设置，请在管理后台配置)')}", flush=True)
        print(f"存储模式：{STORAGE_MODE}，媒体目录：{DATA_DIR}", flush=True)
        print(f"CPU：{CPU} / 内存：{MEMORY_MB} MB", flush=True)

        start_script = find_start_script()
        print(f"Immich 启动脚本：{start_script}", flush=True)
        self.immich_process = subprocess.Popen(
            ["/bin/bash", start_script],
            env=env,
            cwd="/usr/src/app",
        )
        print(f"Immich 进程已启动，PID：{self.immich_process.pid}", flush=True)

        # ---------- 后台线程 ----------
        threading.Thread(
            target=commit_volume_periodically, args=(self.stopping,), daemon=True
        ).start()
        threading.Thread(
            target=dump_periodically, args=(self.stopping, db_user, db_name), daemon=True
        ).start()
        threading.Thread(
            target=watchdog,
            args=(
                {
                    "Postgres": self.pg_process,
                    "Redis": self.redis_process,
                    "Immich": self.immich_process,
                },
                self.stopping,
            ),
            daemon=True,
        ).start()

        # ---------- 等待 Immich 开始监听 ----------
        wait_for_port(SERVER_PORT, WEB_STARTUP_TIMEOUT_SECONDS, self.immich_process, "Immich")

    @modal.exit()
    def stop(self):
        """容器休眠/关闭：先停 Immich → 再停 Redis（保存快照）→ 再停 Postgres → 打包数据库 → 提交 Volume。"""
        stopping = getattr(self, "stopping", None)
        if stopping is not None:
            stopping.set()

        stop_process(getattr(self, "immich_process", None), "Immich", wait=EXIT_WAIT_IMMICH)
        stop_process(getattr(self, "redis_process", None), "Redis", wait=EXIT_WAIT_REDIS)

        pg_process = getattr(self, "pg_process", None)
        # SIGINT = fast shutdown：断开连接、写检查点后干净退出（SIGTERM 会一直等客户端断开）
        stop_process(pg_process, "Postgres", sig=signal.SIGINT, wait=EXIT_WAIT_PG)

        pg_clean = (
            getattr(self, "pg_ready", False)
            and pg_process is not None
            and pg_process.returncode == 0
        )
        if pg_clean:
            try:
                save_postgres_snapshot()
            except Exception as error:
                # 失败时 running.flag 仍在，下次启动会改用逻辑备份
                print(f"保存数据库整体快照失败：{error}", flush=True)
        else:
            print("Postgres 未能干净退出，不保存整体快照，下次启动将使用逻辑备份", flush=True)

        commit_all_volumes()
        print("容器退出。", flush=True)
