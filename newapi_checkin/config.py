"""配置加载：dataclass 模型 + 环境变量覆盖 + 校验 + 旧 visit_config.json 迁移。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .secure_config import ConfigEncryptionError, config_key_from_environment, decrypt_file
from .proxy_pool import ProxyPoolConfig
from .notify import EmailNotifyConfig


def runtime_root() -> Path:
    """返回可执行程序旁边的运行根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = runtime_root()
DATA_DIR = ROOT / "data"
PROFILES_DIR = DATA_DIR / "profiles"
SHOTS_DIR = DATA_DIR / "shots"
LOGS_DIR = DATA_DIR / "logs"
SESSIONS_FILE = DATA_DIR / "sessions.json"
CONFIG_FILE = ROOT / "config.json"
EXAMPLE_FILE = ROOT / "config.example.json"
LEGACY_FILE = ROOT / "visit_config.json"


def configure_runtime_environment() -> None:
    """让冻结发布包中的原生扩展和 Chromium 优先使用包内动态库。"""
    if not sys.platform.startswith("linux"):
        return
    lib_dir = ROOT / "runtime" / "lib"
    if not lib_dir.is_dir():
        return
    current = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [str(lib_dir)] + [item for item in current.split(":") if item]
    os.environ["LD_LIBRARY_PATH"] = ":".join(dict.fromkeys(parts))


configure_runtime_environment()

# 不同 New API fork 的签到路径不一致，按顺序探测
CHECKIN_PATH_CANDIDATES = (
    "/api/user/checkin",
    "/api/user/check_in",
    "/api/user/sign_in",
)

SELF_PATH = "/api/user/self"
# 站点公开信息，不需要鉴权。这里只关心 quota_per_unit —— 额度是内部整数单位，
# 换算成站点展示的 $ 要除以它（TaBiAI 是 500000 quota = $1，别的 fork 未必一样）
STATUS_PATH = "/api/status"
# 站点没告诉我们换算率时的兜底值。New API 上游默认就是这个数
DEFAULT_QUOTA_PER_UNIT = 500000

LOGIN_METHOD_NEWAPI_COOKIE = "newapi_cookie"
# TaBiAI（New API 分支）：凭据是 new_api_refresh cookie，先 refresh 换短期 access token，
# 业务接口只认 Bearer；签到还需要真实浏览器提供的 Turnstile token。
LOGIN_METHOD_TABIAI = "tabiai"
LOGIN_METHODS = (LOGIN_METHOD_NEWAPI_COOKIE, LOGIN_METHOD_TABIAI)

# 已废弃的 GitHub OAuth 登录方式，仅用于识别旧配置并自动迁移到 tabiai
LEGACY_LOGIN_METHOD_GITHUB_COOKIE = "github_cookie"

# TaBiAI 端点（依据 docs/签到原理.md 实测）
TABIAI_REFRESH_PATH = "/api/user/auth/refresh"
TABIAI_CHECKIN_PATH = "/api/user/checkin"
TABIAI_REFRESH_COOKIE_NAME = "new_api_refresh"

# login_method 的历史写法归一化表。配置文件与环境变量两条入口共用，
# 避免只在一处认旧值、另一处静默退回默认值。
LOGIN_METHOD_ALIASES = {
    "newapi": LOGIN_METHOD_NEWAPI_COOKIE,
    "cookie": LOGIN_METHOD_NEWAPI_COOKIE,
    "newapi-cookie": LOGIN_METHOD_NEWAPI_COOKIE,
    LOGIN_METHOD_NEWAPI_COOKIE: LOGIN_METHOD_NEWAPI_COOKIE,
    # GitHub OAuth 已不是登录方式：旧值平滑映射到 tabiai，不报错、不丢配置
    LEGACY_LOGIN_METHOD_GITHUB_COOKIE: LOGIN_METHOD_TABIAI,
    "github": LOGIN_METHOD_TABIAI,
    "github-cookie": LOGIN_METHOD_TABIAI,
    "github_user_session": LOGIN_METHOD_TABIAI,
    "tabi": LOGIN_METHOD_TABIAI,
    "tabiai": LOGIN_METHOD_TABIAI,
    "tabitoken": LOGIN_METHOD_TABIAI,
    "tabi-ai": LOGIN_METHOD_TABIAI,
}



class ConfigError(Exception):
    """配置文件缺失或不合法。"""


def slugify(name: str) -> str:
    """把账号名转成可用作目录名的 ascii slug（中文名也能稳定映射）。"""
    ascii_part = re.sub(r"[^0-9A-Za-z._-]+", "-", name).strip("-._")
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{ascii_part or 'acct'}-{digest}"


def _env_key(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "_", name).upper().strip("_")


@dataclass
class AIConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout: int = 60
    max_retries: int = 2

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_key and self.model)

    def _endpoint(self, suffix: str) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith(suffix):
            return base
        if re.search(r"/v\d+$", base):
            return base + suffix
        return base + "/v1" + suffix

    @property
    def chat_url(self) -> str:
        return self._endpoint("/chat/completions")

    @property
    def models_url(self) -> str:
        return self._endpoint("/models")


@dataclass
class BrowserConfig:
    driver: str = "camoufox"          # camoufox | patchright
    headless: Any = "virtual"          # "virtual"(Xvfb) | True | False
    humanize: bool = True
    timeout: int = 60                  # 单次过盾等待上限（秒）
    keep_artifacts_on_fail: bool = True
    locale: str = "zh-CN"
    window: tuple = (1280, 800)
    executable_path: Optional[str] = None  # 外置/内置 Chromium 可执行文件

    @property
    def is_headful(self) -> bool:
        return self.headless is False


@dataclass
class HttpConfig:
    impersonate: str = "chrome"
    timeout: int = 20
    verify: bool = True


@dataclass
class Defaults:
    retry: int = 2
    interval_seconds: tuple = (3, 8)


@dataclass
class SecurityConfig:
    encryption_enabled: bool = False
    config_key: str = ""
    encrypted_file: str = "data/config.encrypted.json"

    @classmethod
    def from_raw(cls, raw: Any) -> "SecurityConfig":
        raw = raw if isinstance(raw, dict) else {}
        encrypted_file = str(raw.get("encrypted_file") or cls.encrypted_file).strip()
        if not encrypted_file:
            encrypted_file = cls.encrypted_file
        return cls(
            encryption_enabled=_as_bool(raw.get("encryption_enabled"), False),
            config_key=str(raw.get("config_key") or "").strip(),
            encrypted_file=encrypted_file,
        )

    def to_dict(self) -> dict:
        return {
            "encryption_enabled": self.encryption_enabled,
            "config_key": self.config_key,
            "encrypted_file": self.encrypted_file,
        }


@dataclass
class ConfigSyncConfig:
    """远程配置 API 同步设置。"""

    enabled: bool = False
    url: str = ""
    method: str = "GET"
    token: str = ""
    token_header: str = "Authorization"
    token_prefix: str = "Bearer"
    headers: dict = field(default_factory=dict)
    body: Any = None
    response_field: str = ""
    timeout: int = 20
    auto_before_checkin: bool = True
    # TaBiAI 凭据轮转后的回写端点；留空则按 url 同源推导 /api/accounts/{name}/refresh-cookie
    writeback_url: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "ConfigSyncConfig":
        raw = raw if isinstance(raw, dict) else {}
        method = str(raw.get("method") or "GET").strip().upper()
        if method not in {"GET", "POST"}:
            method = "GET"
        headers_raw = raw.get("headers")
        headers = {
            str(key).strip(): str(value)
            for key, value in (headers_raw.items() if isinstance(headers_raw, dict) else [])
            if str(key).strip()
        }
        body = raw.get("body")
        if not isinstance(body, (dict, list, str, int, float, bool, type(None))):
            body = None
        timeout = max(5, min(300, _as_int(raw.get("timeout"), 20)))
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            url=str(raw.get("url") or "").strip(),
            method=method,
            token=str(raw.get("token") or "").strip(),
            token_header=str(raw.get("token_header") or "Authorization").strip() or "Authorization",
            token_prefix=str(raw.get("token_prefix") or "Bearer").strip(),
            headers=headers,
            body=body,
            response_field=str(raw.get("response_field") or "").strip(),
            timeout=timeout,
            auto_before_checkin=_as_bool(raw.get("auto_before_checkin"), True),
            writeback_url=str(raw.get("writeback_url") or "").strip(),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "url": self.url,
            "method": self.method,
            "token": self.token,
            "token_header": self.token_header,
            "token_prefix": self.token_prefix,
            "headers": dict(self.headers),
            "body": self.body,
            "response_field": self.response_field,
            "timeout": self.timeout,
            "auto_before_checkin": self.auto_before_checkin,
            "writeback_url": self.writeback_url,
        }


@dataclass
class Account:
    name: str
    url: str
    cookie: str = ""
    login_method: str = LOGIN_METHOD_NEWAPI_COOKIE
    # 旧 GitHub OAuth 时代的字段：签到链路已不再使用，仅为让老配置能原样加载而保留
    github_user_session: str = ""
    github_client_id: str = ""
    user_id: Optional[int] = None
    proxy: Optional[str] = None
    checkin_path: Optional[str] = None
    browser_path: str = "/dashboard"
    enabled: bool = True

    @property
    def uses_tabiai(self) -> bool:
        return self.login_method == LOGIN_METHOD_TABIAI

    @property
    def credential_label(self) -> str:
        return "TaBiAI 凭据" if self.uses_tabiai else "站点 Cookie"

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def profile_dir(self) -> Path:
        return PROFILES_DIR / self.slug

    def api(self, path: str) -> str:
        return self.base_url + path

    @property
    def browser_url(self) -> str:
        return self.base_url + self.browser_path

    @property
    def checkin_candidates(self) -> tuple:
        if self.checkin_path:
            return (self.checkin_path,)
        return CHECKIN_PATH_CANDIDATES


@dataclass
class TabiAIConfig:
    """TaBiAI 签到专属设置：Turnstile token 只能从真实浏览器里取。

    实测站点对 Turnstile 有频率限制：同一环境 20 分钟内反复 reset 拿不到新 token，
    所以多账号必须串行 + token_interval_minutes 间隔，不能并发抢。
    """

    enabled: bool = False
    # 接管已开着的 Chrome（--remote-debugging-port），而不是 launch 一个新的：
    # 全新启动的浏览器环境过不了 Turnstile，实测只有真实用户浏览器能出 token
    cdp_url: str = "http://127.0.0.1:9222"
    token_timeout: int = 120
    token_interval_minutes: int = 21
    keep_page: bool = False

    @classmethod
    def from_raw(cls, raw: Any) -> "TabiAIConfig":
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            enabled=_as_bool(raw.get("enabled"), False),
            cdp_url=str(raw.get("cdp_url") or "http://127.0.0.1:9222").strip()
            or "http://127.0.0.1:9222",
            token_timeout=max(10, min(600, _as_int(raw.get("token_timeout"), 120))),
            token_interval_minutes=max(0, min(120, _as_int(raw.get("token_interval_minutes"), 21))),
            keep_page=_as_bool(raw.get("keep_page"), False),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "cdp_url": self.cdp_url,
            "token_timeout": self.token_timeout,
            "token_interval_minutes": self.token_interval_minutes,
            "keep_page": self.keep_page,
        }


@dataclass
class NotifyConfig:
    email: EmailNotifyConfig = field(default_factory=EmailNotifyConfig)

    @classmethod
    def from_raw(cls, raw: Any) -> "NotifyConfig":
        raw = raw if isinstance(raw, dict) else {}
        return cls(email=EmailNotifyConfig.from_raw(raw.get("email")))

    def to_dict(self) -> dict:
        return {"email": self.email.to_dict()}


@dataclass
class Config:
    ai: AIConfig = field(default_factory=AIConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    defaults: Defaults = field(default_factory=Defaults)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    config_sync: ConfigSyncConfig = field(default_factory=ConfigSyncConfig)
    proxy_pool: ProxyPoolConfig = field(default_factory=ProxyPoolConfig)
    tabiai: TabiAIConfig = field(default_factory=TabiAIConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    accounts: list = field(default_factory=list)
    source: Optional[Path] = None
    migrated_from: Optional[Path] = None

    def select(self, names: Optional[list] = None) -> list:
        """按 --account 过滤；未指定时返回所有 enabled 账号。"""
        if not names:
            return [a for a in self.accounts if a.enabled]
        wanted = {n.strip() for n in names if n.strip()}
        picked = [a for a in self.accounts if a.name in wanted]
        missing = wanted - {a.name for a in picked}
        if missing:
            available = ", ".join(a.name for a in self.accounts) or "<空>"
            raise ConfigError(
                f"找不到账号: {', '.join(sorted(missing))}；可用账号: {available}"
            )
        return picked


# --------------------------------------------------------------------------- #
# 解析辅助
# --------------------------------------------------------------------------- #

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_pair(value: Any, default: tuple) -> tuple:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            lo, hi = float(value[0]), float(value[1])
            return (min(lo, hi), max(lo, hi))
        except (TypeError, ValueError):
            return default
    return default


def parse_headless(value: Any, default: Any = "virtual") -> Any:
    """headless 支持 "virtual"(自动 Xvfb) / true / false 三态。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "virtual":
        return "virtual"
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #


def _build_accounts(raw_list: Any, problems: list) -> list:
    accounts: list = []
    if not isinstance(raw_list, list):
        problems.append("accounts 必须是数组")
        return accounts
    seen: dict = {}
    for idx, item in enumerate(raw_list, start=1):
        if not isinstance(item, dict):
            problems.append(f"accounts[{idx}] 必须是对象")
            continue
        name = str(item.get("name") or f"Task_{idx}").strip()
        if name in seen:
            name = f"{name}#{idx}"
        seen[name] = True
        url = str(item.get("url") or "").strip()
        if not url:
            problems.append(f"账号 {name}: 缺少 url")
            continue
        if not url.startswith(("http://", "https://")):
            problems.append(f"账号 {name}: url 必须以 http:// 或 https:// 开头（当前 {url}）")
            continue
        raw_uid = item.get("user_id", item.get("userId"))
        user_id = None
        if raw_uid not in (None, "", 0):
            try:
                user_id = int(raw_uid)
            except (TypeError, ValueError):
                problems.append(f"账号 {name}: user_id 不是整数（{raw_uid!r}）")

        cookie = str(item.get("cookie") or "").strip()
        github_user_session = str(
            item.get("github_user_session")
            or item.get("user_session")
            or item.get("github_cookie")
            or ""
        ).strip()
        raw_method = item.get("login_method", item.get("login_type", item.get("auth_type")))
        method_text = str(raw_method or "").strip().lower()
        if not method_text:
            # 默认仍是站点 Cookie；只有明显是旧 GitHub OAuth 格式的账号（只有 user_session
            # 没有 cookie）才推断为 TaBiAI —— 它们本就是 TaBiAI 站点，迁移后需要签发一次凭据。
            method_text = (
                LOGIN_METHOD_TABIAI
                if github_user_session and not cookie
                else LOGIN_METHOD_NEWAPI_COOKIE
            )
        aliases = LOGIN_METHOD_ALIASES
        method_text = aliases.get(method_text, method_text)
        if method_text not in LOGIN_METHODS:
            problems.append(
                f"账号 {name}: login_method 只能是 {', '.join(LOGIN_METHODS)}（当前 {method_text}）"
            )
            method_text = LOGIN_METHOD_NEWAPI_COOKIE

        client_id = str(
            item.get("github_client_id") or item.get("client_id") or ""
        ).strip()
        checkin_path = item.get("checkin_path") or item.get("checkinPath") or None
        if checkin_path and not str(checkin_path).startswith("/"):
            checkin_path = "/" + str(checkin_path).lstrip("/")
        browser_path = item.get("browser_path") or item.get("browserPath") or "/dashboard"
        browser_path = str(browser_path).strip() or "/dashboard"
        if browser_path.startswith(("http://", "https://")):
            from urllib.parse import urlsplit

            parsed_browser = urlsplit(browser_path)
            browser_path = parsed_browser.path or "/"
            if parsed_browser.query:
                browser_path += "?" + parsed_browser.query
        elif not browser_path.startswith("/"):
            browser_path = "/" + browser_path.lstrip("/")
        accounts.append(
            Account(
                name=name,
                url=url,
                cookie=cookie,
                login_method=method_text,
                github_user_session=github_user_session,
                github_client_id=client_id,
                user_id=user_id,
                proxy=(str(item.get("proxy")).strip() or None) if item.get("proxy") else None,
                checkin_path=checkin_path,
                browser_path=browser_path,
                enabled=_as_bool(item.get("enabled"), True),
            )
        )
    return accounts


def _apply_env(cfg: Config) -> list:
    """环境变量覆盖敏感字段，便于在 VPS 上不落盘明文。"""
    notes: list = []
    ai_map = {
        "CHECKIN_AI_BASE_URL": "base_url",
        "CHECKIN_AI_API_KEY": "api_key",
        "CHECKIN_AI_MODEL": "model",
    }
    for env_name, attr in ai_map.items():
        value = os.environ.get(env_name)
        if value:
            setattr(cfg.ai, attr, value.strip())
            notes.append(f"{env_name} -> ai.{attr}")
    if os.environ.get("CHECKIN_AI_ENABLED") is not None:
        cfg.ai.enabled = _as_bool(os.environ["CHECKIN_AI_ENABLED"], cfg.ai.enabled)
        notes.append("CHECKIN_AI_ENABLED -> ai.enabled")

    for acct in cfg.accounts:
        key = _env_key(acct.name)
        for suffix, attr in (
            ("COOKIE", "cookie"),
            ("GITHUB_USER_SESSION", "github_user_session"),
            ("GITHUB_CLIENT_ID", "github_client_id"),
            ("PROXY", "proxy"),
        ):
            env_name = f"CHECKIN_ACCOUNT_{key}_{suffix}"
            value = os.environ.get(env_name)
            if value:
                setattr(acct, attr, value.strip())
                notes.append(f"{env_name} -> {acct.name}.{attr}")
        method_env = f"CHECKIN_ACCOUNT_{key}_LOGIN_METHOD"
        method_value = os.environ.get(method_env)
        if method_value:
            method = method_value.strip().lower()
            method = LOGIN_METHOD_ALIASES.get(method, method)
            if method in LOGIN_METHODS:
                acct.login_method = method
                notes.append(f"{method_env} -> {acct.name}.login_method")
            else:
                notes.append(f"{method_env} 无效，忽略: {method}")
    return notes


def build_config(raw: dict, source: Optional[Path] = None) -> Config:
    problems: list = []
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是对象（旧的纯数组格式请见迁移说明）")

    security = SecurityConfig.from_raw(raw.get("security"))

    ai_raw = raw.get("ai") or {}
    ai = AIConfig(
        enabled=_as_bool(ai_raw.get("enabled"), False),
        base_url=str(ai_raw.get("base_url") or "").strip(),
        api_key=str(ai_raw.get("api_key") or "").strip(),
        model=str(ai_raw.get("model") or "gpt-4o-mini").strip(),
        timeout=_as_int(ai_raw.get("timeout"), 60),
        max_retries=_as_int(ai_raw.get("max_retries"), 2),
    )

    br_raw = raw.get("browser") or {}
    driver = str(br_raw.get("driver") or "camoufox").strip().lower()
    if driver not in ("camoufox", "patchright"):
        problems.append(f"browser.driver 只能是 camoufox 或 patchright（当前 {driver}）")
        driver = "camoufox"
    window = _as_pair(br_raw.get("window"), (1280, 800))
    executable_path = br_raw.get("executable_path", br_raw.get("executablePath"))
    executable_path = str(executable_path).strip() if executable_path else None
    browser = BrowserConfig(
        driver=driver,
        headless=parse_headless(br_raw.get("headless"), "virtual"),
        humanize=_as_bool(br_raw.get("humanize"), True),
        timeout=_as_int(br_raw.get("timeout"), 60),
        keep_artifacts_on_fail=_as_bool(br_raw.get("keep_artifacts_on_fail"), True),
        locale=str(br_raw.get("locale") or "zh-CN").strip(),
        window=(int(window[0]), int(window[1])),
        executable_path=executable_path,
    )

    http_raw = raw.get("http") or {}
    http = HttpConfig(
        impersonate=str(http_raw.get("impersonate") or "chrome").strip(),
        timeout=_as_int(http_raw.get("timeout"), 20),
        verify=_as_bool(http_raw.get("verify"), True),
    )

    def_raw = raw.get("defaults") or {}
    interval = _as_pair(def_raw.get("interval_seconds"), (3.0, 8.0))
    defaults = Defaults(
        retry=max(0, _as_int(def_raw.get("retry"), 2)),
        interval_seconds=interval,
    )
    config_sync = ConfigSyncConfig.from_raw(raw.get("config_sync"))
    proxy_pool = ProxyPoolConfig.from_raw(raw.get("proxy_pool"))
    tabiai = TabiAIConfig.from_raw(raw.get("tabiai"))
    notify = NotifyConfig.from_raw(raw.get("notify"))

    accounts = _build_accounts(raw.get("accounts"), problems)
    if not accounts and not problems:
        problems.append("accounts 为空，至少配置一个账号")

    cfg = Config(
        ai=ai, browser=browser, http=http, defaults=defaults,
        security=security, config_sync=config_sync, proxy_pool=proxy_pool,
        tabiai=tabiai, notify=notify, accounts=accounts, source=source,
    )
    _apply_env(cfg)

    if cfg.ai.enabled and not cfg.ai.ready:
        problems.append("ai.enabled 为 true 但 base_url / api_key / model 不完整")

    if problems:
        raise ConfigError("配置有问题:\n  - " + "\n  - ".join(problems))
    return cfg


# --------------------------------------------------------------------------- #
# 旧格式迁移
# --------------------------------------------------------------------------- #


def migrate_legacy(raw_list: list) -> dict:
    """把旧 visit_config.json 的纯数组结构转换为新结构。"""
    accounts = []
    for idx, item in enumerate(raw_list, start=1):
        if not isinstance(item, dict):
            continue
        accounts.append(
            {
                "name": item.get("name") or f"Task_{idx}",
                "url": item.get("url") or "",
                "cookie": item.get("cookie") or "",
                "login_method": LOGIN_METHOD_NEWAPI_COOKIE,
                "github_user_session": "",
                "github_client_id": "",
                "user_id": item.get("userId") or item.get("user_id"),
                "proxy": item.get("proxy"),
                "checkin_path": None,
                "enabled": True,
            }
        )
    template: dict = {}
    if EXAMPLE_FILE.exists():
        try:
            template = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            template = {}
    result = {
        "ai": {"enabled": False, "base_url": "", "api_key": "", "model": "gpt-4o-mini",
               "timeout": 60, "max_retries": 2},
        "browser": template.get("browser") or {},
        "http": template.get("http") or {},
        "defaults": template.get("defaults") or {},
        "security": template.get("security") or {},
        "accounts": accounts,
    }
    return result


def _read_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"读取 {path.name} 失败: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} 不是合法 JSON: 第 {exc.lineno} 行 {exc.msg}") from exc


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROFILES_DIR, SHOTS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def resolve_encrypted_config_path(target: Path, security: SecurityConfig) -> Path:
    encrypted = Path(security.encrypted_file)
    return encrypted if encrypted.is_absolute() else target.parent / encrypted


def read_config_document(path: Optional[Path] = None) -> tuple[dict, dict, SecurityConfig]:
    """返回 (bootstrap 明文、有效 raw、security)。"""
    target = path or Path(os.environ.get("CHECKIN_CONFIG") or CONFIG_FILE)
    if not target.exists():
        raise ConfigError(f"找不到配置文件 {target}")
    bootstrap = _read_json(target)
    if isinstance(bootstrap, list):
        bootstrap = migrate_legacy(bootstrap)
    if not isinstance(bootstrap, dict):
        raise ConfigError("配置根节点必须是对象")
    security = SecurityConfig.from_raw(bootstrap.get("security"))
    if not security.encryption_enabled:
        return bootstrap, bootstrap, security

    key = config_key_from_environment(security.config_key)
    if not key:
        raise ConfigError("已启用配置加密，但 security.config_key 为空")
    encrypted_path = resolve_encrypted_config_path(target, security)
    if not encrypted_path.exists():
        raise ConfigError(f"已启用配置加密，但找不到密文文件: {encrypted_path}")
    try:
        effective = decrypt_file(encrypted_path, key)
    except ConfigEncryptionError as exc:
        raise ConfigError(str(exc)) from exc
    if not isinstance(effective, dict):
        raise ConfigError("解密后的配置根节点必须是对象")
    effective = dict(effective)
    effective["security"] = security.to_dict()
    return bootstrap, effective, security


def load_config(path: Optional[Path] = None) -> Config:
    """加载配置；启用 security.encryption_enabled 时自动解密。"""
    ensure_dirs()
    target = path or Path(os.environ.get("CHECKIN_CONFIG") or CONFIG_FILE)

    if target.exists():
        _bootstrap, raw, _security = read_config_document(target)
        return build_config(raw, source=target)

    if LEGACY_FILE.exists():
        legacy_raw = _read_json(LEGACY_FILE)
        if not isinstance(legacy_raw, list):
            raise ConfigError(f"{LEGACY_FILE.name} 应为纯数组格式")
        converted = migrate_legacy(legacy_raw)
        CONFIG_FILE.write_text(
            json.dumps(converted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        cfg = build_config(converted, source=CONFIG_FILE)
        cfg.migrated_from = LEGACY_FILE  # type: ignore[attr-defined]
        return cfg
    raise ConfigError(
        f"找不到配置文件 {target}。\n"
        f"  请复制模板后填写: cp {EXAMPLE_FILE.name} {CONFIG_FILE.name}"
    )
