"""
newapi_checkin/xray.py
xray 管理器：把 VLESS 节点变成本地 socks5 入口，供既有的 HTTP/浏览器消费点直接用。

为什么需要它：curl_cffi 和 Playwright 都只吃 http/socks5 代理，VLESS 得先有个本地
转换层。一个 xray 进程带 N 个入站（每个节点一个本地端口），不是一条一个进程 ——
预检并发能到几十上百条，那个量级的进程起不来。

配置结构与 server/xray_config.go 保持同一套（那边对照 xray 官方形态核对过）：
    inbounds[]      {listen:"127.0.0.1", port, protocol:"socks", tag:"in-N"}
    outbounds[]     {protocol:"vless", settings.vnext[...], streamSettings{...}, tag:"out-N"}
    routing.rules[] {type:"field", inboundTag:["in-N"], outboundTag:"out-N"}
两边漂了会出现「平台测得通、客户端连不上」这种最难查的不一致。

最要紧的一条约定：**节点标识与本地入口是两个不同的键**。
成功率反馈、黑名单、共用计数一律按节点原始 vless:// URI 记账；只有真正发请求时
才换成 socks5://127.0.0.1:port。混用会让所有反馈记到 127.0.0.1 上，
平台侧的优选排序当场失效。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import logger as log

# 只支持这两种传输安全：其余（none/ws/grpc/xhttp）的字段名没有可靠依据，
# 猜着生成会得到「xray 能启动但连不上」的配置
SUPPORTED_SECURITY = ("reality", "tls")
SUPPORTED_NETWORK = ("tcp",)

# 本地入站只听回环。听 0.0.0.0 等于把明文 socks 代理白送给同网段任何人
LOCAL_HOST = "127.0.0.1"

# 默认起始端口。挑高位避开常用服务，冲突时 _pick_free_port 会往后找
DEFAULT_START_PORT = 22800

# 进程启动后等端口就绪的上限。xray 冷启动通常 200ms 内，给 15s 容忍慢机器
STARTUP_TIMEOUT_SEC = 15


class XrayUnavailable(RuntimeError):
    """找不到可用的 xray 二进制，附带可执行的修复提示。"""


class XrayStartError(RuntimeError):
    """xray 启动失败（配置被拒 / 端口占满 / 进程当场退出）。"""


@dataclass
class VlessNode:
    """一条 VLESS 节点。

    raw 逐字保留原始 URI —— 它是平台侧 proxies 表的 addr 与 proxy_feedback 的主键，
    反馈必须按它回填，不能用本地端口。
    """

    raw: str
    uuid: str
    host: str
    port: int
    tag: str = ""
    params: dict = field(default_factory=dict)

    def param(self, key: str, default: str = "") -> str:
        """取单个查询参数（参数名大小写敏感：headerType 之类是驼峰）。"""
        values = self.params.get(key) or []
        return (values[0] if values else "") or default

    @property
    def network(self) -> str:
        """传输方式。分享链接省略 type 时各家客户端都按 tcp 处理。"""
        return (self.param("type") or "tcp").lower()

    @property
    def security(self) -> str:
        """传输层安全。别和 encryption 搞混：VLESS 的 encryption 恒为 none。"""
        return (self.param("security") or "none").lower()

    @property
    def server_name(self) -> str:
        """握手用的域名：sni → host 参数 → 节点 host。

        回落节点 host 是有意的：纯 tls 节点常不写 sni，此时 SNI 就该是连接地址本身。
        """
        for key in ("sni", "host"):
            value = self.param(key).strip()
            if value:
                return value
        return self.host


def parse_vless_uri(uri: str) -> VlessNode | None:
    """解析一条 vless:// 链接；缺了接入必需的三样（uuid/host/port）返回 None。

    不在这里判断「支持不支持」—— 那是 node_supported 的事。解析与能力判定分开，
    才能在跳过某个节点时说清到底是链接坏了还是传输方式没实现。
    """
    text = (uri or "").strip()
    if not text.lower().startswith("vless://"):
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    uuid = unquote(parsed.username or "").strip()
    host = (parsed.hostname or "").strip()
    if not uuid or not host or not parsed.port:
        return None
    return VlessNode(
        raw=text,
        uuid=uuid,
        host=host,
        port=int(parsed.port),
        tag=unquote(parsed.fragment or ""),
        params=parse_qs(parsed.query or ""),
    )


def node_supported(node: VlessNode) -> tuple[bool, str]:
    """能不能给这个节点生成有效配置。返回 (可用, 不可用原因)。

    原因要带回去记日志：静默跳过会让人以为节点数对不上是 bug。
    """
    if node.security not in SUPPORTED_SECURITY:
        return False, f'security="{node.security}" 暂不支持（只支持 reality / tls）'
    if node.network not in SUPPORTED_NETWORK:
        return False, f'传输方式 type="{node.network}" 暂不支持（只支持 tcp）'
    if node.security == "reality" and not node.param("pbk"):
        return False, "reality 节点缺少 pbk（publicKey），无法握手"
    return True, ""


def node_label(node: VlessNode) -> str:
    """日志里用的节点标识。**绝不能带 uuid** —— 那是接入凭据。"""
    name = node.tag.strip() or f"{node.host}:{node.port}"
    return f"vless://***@{node.host}:{node.port}#{name}" if node.tag else f"vless://***@{node.host}:{node.port}"


def proxy_label(addr: str) -> str:
    """任意代理地址的日志安全写法。

    vless:// 必须打码（uuid 是接入凭据），http/socks5 原样返回 —— 那类地址在日志里
    本来就是可见的，打码反而让排查时对不上号。解析不出来的 vless 也不能原样吐出去。
    """
    if not addr:
        return ""
    if not addr.lower().startswith("vless://"):
        return addr
    node = parse_vless_uri(addr)
    return node_label(node) if node else "vless://<无法解析>"


@dataclass
class Binding:
    """节点与它的本地入口的对应关系。两个键刻意分开，见模块开头的约定。"""

    node_addr: str      # 原始 vless:// URI，记账用
    local_proxy: str    # socks5://127.0.0.1:port，发请求用
    inbound_tag: str


def _outbound_for(node: VlessNode, tag: str) -> dict:
    """造一条 vless 出站。"""
    user = {"id": node.uuid, "encryption": "none"}
    # flow 只在节点显式给了才带：xtls-rprx-vision 之类填错会直接握手失败，
    # 给一个「常见默认值」比不给更危险
    flow = node.param("flow").strip()
    if flow:
        user["flow"] = flow

    stream: dict = {"network": node.network, "security": node.security}
    if node.security == "reality":
        stream["realitySettings"] = {
            "fingerprint": node.param("fp") or "chrome",
            "serverName": node.server_name,
            "publicKey": node.param("pbk"),
            "shortId": node.param("sid"),
            "spiderX": node.param("spx"),
        }
    elif node.security == "tls":
        tls: dict = {"serverName": node.server_name}
        if node.param("fp"):
            tls["fingerprint"] = node.param("fp")
        if node.param("alpn"):
            tls["alpn"] = node.param("alpn").split(",")
        stream["tlsSettings"] = tls

    return {
        "protocol": "vless",
        "settings": {"vnext": [{"address": node.host, "port": node.port, "users": [user]}]},
        "streamSettings": stream,
        "tag": tag,
    }


def build_config(nodes: list[VlessNode], ports: list[int]) -> tuple[dict, list[Binding], list[str]]:
    """生成 xray 配置与节点→本地入口映射。

    ports 由调用方预先探好（每个可用节点一个），因为「端口能不能听」只有真去 bind
    才知道，不能在这层假设连号可用。ports 不够时多出来的节点按跳过处理。

    全部节点都不支持时返回空 inbounds —— 调用方据此判定「压根不用起 xray」，
    这比抛错好处理。
    """
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [],
        "outbounds": [],
        "routing": {"rules": []},
    }
    bindings: list[Binding] = []
    skipped: list[str] = []

    for node in nodes:
        ok, why = node_supported(node)
        if not ok:
            skipped.append(f"{node_label(node)}: {why}")
            continue
        if len(bindings) >= len(ports):
            skipped.append(f"{node_label(node)}: 没有可用的本地端口了")
            continue

        index = len(bindings)
        port = ports[index]
        in_tag, out_tag = f"in-{index}", f"out-{index}"
        config["inbounds"].append({
            "listen": LOCAL_HOST, "port": port, "protocol": "socks", "tag": in_tag,
        })
        config["outbounds"].append(_outbound_for(node, out_tag))
        config["routing"]["rules"].append({
            "type": "field", "inboundTag": [in_tag], "outboundTag": out_tag,
        })
        bindings.append(Binding(
            node_addr=node.raw,
            local_proxy=f"socks5://{LOCAL_HOST}:{port}",
            inbound_tag=in_tag,
        ))
    return config, bindings, skipped


def find_binary(configured: str = "") -> str:
    """定位 xray 可执行文件。找不到抛 XrayUnavailable 并给出修复提示。

    查找顺序：配置指定 → PATH → 项目 bin/ 目录（Actions 下载后放这里）。
    """
    candidates: list[str] = []
    if configured.strip():
        candidates.append(configured.strip())
    for name in ("xray", "xray.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    root = Path(__file__).resolve().parent.parent
    for name in ("xray", "xray.exe"):
        candidates.append(str(root / "bin" / name))

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise XrayUnavailable(
        "找不到 xray 可执行文件。请任选其一：\n"
        "  1) 配置 proxy_pool.xray_path 指向二进制\n"
        "  2) 把 xray 放进 PATH\n"
        f"  3) 放到 {root / 'bin'} 目录下\n"
        "GitHub Actions 里由 workflow 自动下载（见 .github/workflows）"
    )


def _pick_free_port(start: int, taken: set[int]) -> int | None:
    """从 start 往上找一个能真正 bind 的端口。

    真去 bind 而不是只看 taken：同机器上别的进程（甚至上一轮没退干净的 xray）
    可能正占着，只查自己的记录会让 xray 启动时才炸，且错误信息很难懂。
    """
    for port in range(start, start + 500):
        if port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((LOCAL_HOST, port))
            except OSError:
                continue
        return port
    return None


def _wait_port_ready(port: int, deadline: float) -> bool:
    """等某个本地端口开始接受连接。"""
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex((LOCAL_HOST, port)) == 0:
                return True
        time.sleep(0.2)
    return False


class XrayManager:
    """管一个 xray 进程的生命周期，对外只暴露「节点 → 本地代理地址」。

    用法（务必用 with，否则进程会漏）：
        with XrayManager(nodes, xray_path=cfg.xray_path) as mgr:
            proxy = mgr.proxy_for(node_addr)   # socks5://127.0.0.1:xxxxx

    没有可用节点时 start() 不起进程、proxy_for 一律返回空串 —— 调用方据此回落
    到原有的 http/socks5 代理，而不是拿一个不能用的地址去死等超时。
    """

    def __init__(self, nodes: list[VlessNode], xray_path: str = "",
                 start_port: int = DEFAULT_START_PORT):
        self.nodes = nodes
        self.xray_path = xray_path
        self.start_port = start_port
        self.bindings: list[Binding] = []
        self.skipped: list[str] = []
        self._by_node: dict[str, str] = {}
        self._proc: subprocess.Popen | None = None
        self._workdir: str = ""

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """探端口 → 生成配置 → 起进程 → 等入站就绪。"""
        if not self.nodes:
            return
        taken: set[int] = set()
        ports: list[int] = []
        for _ in self.nodes:
            port = _pick_free_port(self.start_port, taken)
            if port is None:
                break
            taken.add(port)
            ports.append(port)

        config, self.bindings, self.skipped = build_config(self.nodes, ports)
        for reason in self.skipped:
            log.warn(f"[xray] 跳过节点 {reason}")
        if not self.bindings:
            log.warn("[xray] 没有可用的 VLESS 节点，不启动 xray")
            return

        binary = find_binary(self.xray_path)
        self._workdir = tempfile.mkdtemp(prefix="newapi-xray-")
        config_path = os.path.join(self._workdir, "config.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)

        # 日志重定向到文件而不是 PIPE：不读 PIPE 会在缓冲区满时把 xray 卡死
        self._log_path = os.path.join(self._workdir, "xray.log")
        log_fh = open(self._log_path, "w", encoding="utf-8")
        creationflags = 0
        if sys.platform == "win32":
            # 不给子进程弹控制台窗口（与 daemon.py 的做法一致）
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                [binary, "run", "-c", config_path],
                stdout=log_fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=creationflags,
            )
        except OSError as exc:
            log_fh.close()
            self._cleanup_workdir()
            raise XrayStartError(f"启动 xray 失败: {type(exc).__name__}: {exc}") from exc
        finally:
            log_fh.close()

        deadline = time.time() + STARTUP_TIMEOUT_SEC
        first_port = int(self.bindings[0].local_proxy.rsplit(":", 1)[1])
        if not _wait_port_ready(first_port, deadline):
            detail = self._tail_log()
            self.stop()
            raise XrayStartError(
                f"xray 启动后 {STARTUP_TIMEOUT_SEC}s 内入站未就绪。日志尾部：\n{detail}"
            )
        self._by_node = {b.node_addr: b.local_proxy for b in self.bindings}
        log.info(f"[xray] 已启动，{len(self.bindings)} 个节点各自监听一个本地端口")

    def stop(self) -> None:
        """停进程并清临时目录。重复调用安全。"""
        proc = self._proc
        self._proc = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # 5 秒还不退就强杀：临时目录要删，留着进程会占住配置文件
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    log.warn("[xray] 进程未能退出，临时目录可能残留")
        self._cleanup_workdir()
        self._by_node = {}

    def _cleanup_workdir(self) -> None:
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = ""

    def _tail_log(self, lines: int = 12) -> str:
        """取 xray 日志尾部，用于把启动失败的真实原因带回错误信息里。"""
        path = getattr(self, "_log_path", "")
        if not path or not os.path.isfile(path):
            return "（没有日志）"
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:]).strip() or "（日志为空）"
        except OSError:
            return "（日志读不出来）"

    def __enter__(self) -> "XrayManager":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ---------------------------------------------------------------- 查询

    def proxy_for(self, node_addr: str) -> str:
        """节点原始 URI → 本地代理地址。没有对应入口时返回空串。"""
        return self._by_node.get((node_addr or "").strip(), "")

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def local_proxies(self) -> list[str]:
        """全部本地入口地址，顺序与节点优选顺序一致。"""
        return [b.local_proxy for b in self.bindings]
