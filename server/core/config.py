"""运行配置：数据目录、密钥、端口。"""
from __future__ import annotations

import contextlib
import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

logger = logging.getLogger(__name__)

# 老版 0.1.4 用的变量名；改名成 AOHUANG_DATA_DIR 后仍要认它，
# 否则老用户升级后 data_dir 变了，表现就是「库里的东西全没了」。
LEGACY_DATA_DIR_ENV = "TK_AUTOMATION_DATA_DIR"
DATA_DIR_ENV = "AOHUANG_DATA_DIR"


def _default_data_dir() -> Path:
    """数据目录：AOHUANG_DATA_DIR 优先，其次老变量，最后落默认位置。"""
    base = (os.environ.get(DATA_DIR_ENV) or "").strip()
    if base:
        return Path(base)
    legacy = (os.environ.get(LEGACY_DATA_DIR_ENV) or "").strip()
    if legacy:
        return Path(legacy)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(local) / "aohuang-desktop"


_legacy_warned = False


def _warn_legacy_env() -> None:
    """只在「旧变量被采纳」时提醒一次，且要等日志系统就绪以后再提醒。

    `Settings.data_dir` 的默认值在类定义时就算好了（模块导入阶段），那会儿
    configure_logging() 还没跑，warning 只会进 stderr 而被打包后的 Electron 丢掉。
    ensure_dirs() 在 lifespan 里紧跟 configure_logging() 执行，借它落盘最稳。
    """
    global _legacy_warned
    if _legacy_warned:
        return
    if (os.environ.get(DATA_DIR_ENV) or "").strip():
        return
    legacy = (os.environ.get(LEGACY_DATA_DIR_ENV) or "").strip()
    if not legacy:
        return
    _legacy_warned = True
    logger.warning(
        "检测到旧环境变量 %s：已改用 %s，当前仍兼容旧变量名。"
        "请把变量名改为 %s —— 数据目录仍然是 %s，数据不会丢。",
        LEGACY_DATA_DIR_ENV, DATA_DIR_ENV, DATA_DIR_ENV, legacy,
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AOHUANG_", env_file=".env", extra="ignore")

    data_dir: Path = _default_data_dir()
    # Optional SQLAlchemy URL. An empty value preserves the per-user SQLite database.
    database_url: str = ""
    api_port: int = 8812
    api_port_max: int = 8822
    # 妙手上游。注意是 openapi-erp，不是 openapi ——
    # 0.1.4 老版 src/miaoshou_client.py 里写的是 openapi-erp.91miaoshou.com，
    # 之前误写成 openapi.91miaoshou.com 导致 TLS 握手失败（SSL connect error），
    # 表现就是「以前能连采集箱，现在连不上」。
    miaoshou_base: str = "https://openapi-erp.91miaoshou.com"
    # 图片翻译代理（空则用官方端点直连）
    image_proxy: str = ""
    image_endpoint: str = "https://image.codesonline.dev/v1/images/edits"
    image_task_endpoint: str = "https://image.codesonline.dev/v1/images/tasks"
    # 图片服务密钥；也可写在 data_dir/image-key.txt
    image_key: str = ""
    # 共享服务器模式（多人共用同一份仓库）。
    # True 时后端监听 0.0.0.0，别人才能连上；单独开这个开关会在启动日志里
    # 打一条显眼的告示（见 main.py 的 shared_mode_warning）。
    # 注意它**不是**权限开关：没有任何鉴权逻辑读它，它只影响监听地址和
    # 自动更新（共享模式下不做自动安装，否则一个人升级会把所有人的服务重启）。
    shared_mode: bool = False
    # 显式指定监听地址，优先于 shared_mode。
    # 为什么两个都要：shared_mode 是「给人用的语义开关」（开/关），
    # bind_host 是「给部署用的具体值」（只想绑某个内网网卡时用得上）。
    # 只留 shared_mode 就没法绑到 192.168.x.x，而只留 bind_host 又要求
    # 用户去写 IP 才能实现「多人共用」这个最普通的需求。
    bind_host: str = ""
    # 开发模式：打开 /api/docs 与 /openapi.json
    dev: bool = False
    request_timeout: float = 30.0
    # 请求体上限（图片 base64 走 JSON 时放宽）
    max_body_bytes: int = 40 * 1024 * 1024

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def sqlalchemy_url(self) -> URL:
        """Return the configured database URL, defaulting to the local SQLite file."""
        configured = self.database_url.strip()
        if not configured:
            return URL.create("sqlite", database=str(self.db_path))

        url = make_url(configured)
        if url.drivername in {"postgres", "postgresql"}:
            url = url.set(drivername="postgresql+psycopg")
        if url.get_backend_name() not in {"sqlite", "postgresql"}:
            raise ValueError("AOHUANG_DATABASE_URL must use SQLite or PostgreSQL")
        return url

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def image_results_dir(self) -> Path:
        return self.data_dir / "image-results"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def image_key_path(self) -> Path:
        return self.data_dir / "image-key.txt"

    def ensure_dirs(self) -> None:
        self._make(self.data_dir)
        self._make(self.images_dir)
        self._make(self.image_results_dir)
        self._make(self.logs_dir)
        # logs_dir 刚建好、configure_logging 已在 lifespan 里跑过，这里提醒才会落盘
        _warn_legacy_env()

    @property
    def listen_host(self) -> str:
        """后端实际监听地址。三个来源，优先级从高到低：

          1. `AOHUANG_BIND_HOST` —— 部署时想绑某个具体网卡（如 192.168.1.5）；
          2. `AOHUANG_SHARED_MODE=1` —— 多人共用，绑所有网卡；
          3. 默认 —— 只绑回环，别人连不上，等于「每人在自己机器上装一份」。

        这个顺序不能反过来：显式指定的地址永远比一个布尔开关更具体，
        开关开着但用户指了网卡，就该听用户的。
        放在 config 里而不是 main.py 里，是因为 run_server.py（打包入口）
        和 main.py（源码入口）必须算出同一个地址，否则「源码能连、打包连不上」。
        """
        explicit = (self.bind_host or "").strip()
        if explicit:
            return explicit
        return "0.0.0.0" if self.shared_mode else "127.0.0.1"

    @staticmethod
    def _make(path: Path) -> None:
        """建目录；失败时给出可读提示，而不是把 PermissionError 抛给启动器。

        打包后若数据目录不可写（杀软拦截、只读卷、被策略限制），
        原始 traceback 里只有 WinError 5，用户和日志都看不出该怎么办。
        """
        try:
            path.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise RuntimeError(
                f"数据目录无法写入：{path}\n"
                f"原因：{exc}\n"
                "处理办法：把数据目录换到有写权限的位置（设置 AOHUANG_DATA_DIR 环境变量"
                "指向例如 D:\\aohuang-data），或给该目录授予当前用户写权限后重试。"
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"数据目录创建失败：{path}\n原因：{exc}\n"
                "处理办法：检查磁盘空间、路径是否被同名文件占用，或改用 AOHUANG_DATA_DIR "
                "指向其它位置。"
            ) from exc

    def local_secret(self) -> str:
        """本机 API 密钥：首次启动生成，写入文件（600）。"""
        self.ensure_dirs()
        if self.secret_path.is_file():
            value = self.secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(32)
        self.secret_path.write_text(value, encoding="utf-8")
        with contextlib.suppress(OSError):
            # 非 POSIX 文件系统（如某些网络盘）不支持 chmod，权限位落不下不影响使用
            os.chmod(self.secret_path, 0o600)
        return value

    def image_api_key(self) -> str:
        """图片服务密钥：环境变量 > 数据目录 image-key.txt > 老版遗留位置。

        老版把密钥放在 LOCALAPPDATA/tk-product-automation 下，
        按模式叫 codesonline-key.txt（直连）或 image-license.txt（代理许可）。
        升级到这个版本的机器上那两个文件还在——不读就等于让用户重新填一遍密钥，
        而界面上没有任何地方能填。所以按老版同样的顺序回退读取。
        """
        if self.image_key.strip():
            return self.image_key.strip()
        try:
            text = self.image_key_path.read_text(encoding="utf-8-sig").strip()
            if text:
                return text
        except OSError:
            pass
        return self._legacy_image_key()

    def legacy_image_key_dir(self) -> Path:
        """老版 0.1.4 的密钥目录（LOCALAPPDATA/tk-product-automation）。

        公开方法而不是 _legacy_dir：/images/verify 要在「未配置」的提示里把
        这个路径原样写给人看，跨模块摸私有方法既脏又容易在重构时静默失联。
        """
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "tk-product-automation"

    def _legacy_image_key(self) -> str:
        """老版 0.1.4 的密钥文件（LOCALAPPDATA/tk-product-automation）。"""
        base = self.legacy_image_key_dir()
        proxy = (self.image_proxy or "").strip()
        # 与老版 image_translate.credential_path 一致的取名规则：
        # 配了代理读许可码，没配读服务商密钥。
        names = ("image-license.txt",) if proxy else ("codesonline-key.txt", "image-license.txt")
        for name in names:
            try:
                text = (base / name).read_text(encoding="utf-8-sig").strip()
            except OSError:
                continue
            if len(text) >= 8 and not any(ch.isspace() for ch in text):
                return text
        return ""

    def edits_endpoint(self) -> str:
        proxy = (self.image_proxy or "").strip().rstrip("/")
        if proxy:
            return f"{proxy}/v1/images/edits"
        return self.image_endpoint

    def image_key_origin(self) -> dict:
        """反查「当前生效的图片密钥是从哪儿读到的」，给 /images/verify 用。

        刻意不复制 image_api_key() 的查找顺序：这里先拿到最终生效的值，再逐个
        位置去比对，命中哪个就是哪个。两份顺序各写一遍迟早会漂移，那种漂移的
        表现是「界面说密钥来自 A，实际用的是 B」，比不告诉用户还糟。

        返回 {"source": 人话来源, "path": 文件路径或空, "mode": direct|proxy|""}。
        """
        key = self.image_api_key()
        mode = "proxy" if (self.image_proxy or "").strip() else "direct"
        if not key:
            return {"source": "", "path": "", "mode": ""}
        if self.image_key.strip() == key:
            return {"source": "环境变量 AOHUANG_IMAGE_KEY", "path": "", "mode": mode}
        if self._read_text(self.image_key_path) == key:
            return {"source": f"数据目录 {self.image_key_path.name}",
                    "path": str(self.image_key_path), "mode": mode}
        for name in ("image-license.txt", "codesonline-key.txt"):
            candidate = self.legacy_image_key_dir() / name
            if self._read_text(candidate) == key:
                return {"source": f"老版遗留文件 {name}", "path": str(candidate),
                        "mode": mode}
        # 值对得上却没有可指认的位置（比如文件在读完后又变了），如实说不确定
        return {"source": "未知来源", "path": "", "mode": mode}

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            return ""

    def task_endpoint(self) -> str:
        proxy = (self.image_proxy or "").strip().rstrip("/")
        if proxy:
            return f"{proxy}/v1/images/tasks"
        return self.image_task_endpoint


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
