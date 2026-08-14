"""BrowserDriver 抽象层。

Camoufox 和 Patchright 都暴露 Playwright 的同步 API，所以所有共享逻辑
（导航、状态判定、cookie 收割、截图、页内 fetch）都放在基类里，
子类只负责把一个持久化 BrowserContext 交出来。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from .. import logger as log
from ..config import Account, Config
from . import detect

# Turnstile 组件常见容器/iframe 选择器
TURNSTILE_SELECTORS = (
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[title*='Cloudflare']",
    "iframe[title*='challenge']",
    ".cf-turnstile",
    "#cf-turnstile",
    "div[id^='cf-chl-widget']",
)
# 站点自带图形验证码
CAPTCHA_IMG_SELECTORS = (
    "img[src*='captcha']",
    "img[alt*='验证码']",
    "canvas[id*='captcha']",
    "#captcha img",
)
CAPTCHA_INPUT_SELECTORS = (
    "input[name*='captcha']",
    "input[placeholder*='验证码']",
    "input[id*='captcha']",
)


@dataclass
class PageState:
    url: str = ""
    title: str = ""
    html: str = ""
    challenge: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.challenge is None

    def brief(self) -> str:
        label = detect.CHALLENGE_LABEL.get(self.challenge or "", "正常页面")
        return f"{label} | title={self.title[:40]!r}"


class DriverUnavailable(RuntimeError):
    """驱动依赖缺失或浏览器未下载，附带可执行的修复提示。"""


class BrowserDriver:
    """公共实现。子类只需实现 _launch() 返回一个持久化 BrowserContext。"""

    name = "base"

    def __init__(self, cfg: Config, account: Account, options: Any = None):
        self.cfg = cfg
        self.account = account
        self.options = options
        self.context = None
        self.page = None
        self._closers: list = []

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def _launch(self):
        raise NotImplementedError

    def open(self) -> "BrowserDriver":
        self.account.profile_dir.mkdir(parents=True, exist_ok=True)
        self.context = self._launch()
        try:
            self.context.set_default_timeout(self.cfg.browser.timeout * 1000)
        except Exception:  # noqa: BLE001
            pass
        pages = list(getattr(self.context, "pages", []) or [])
        self.page = pages[0] if pages else self.context.new_page()
        return self

    def close(self) -> None:
        for closer in reversed(self._closers):
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不该影响签到结果
                log.debug(f"关闭浏览器资源时出错: {exc}")
        self._closers.clear()
        self.context = None
        self.page = None

    def __enter__(self) -> "BrowserDriver":
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    def _proxy(self) -> Optional[dict]:
        from ..utils import parse_proxy

        return parse_proxy(self.account.proxy)

    # ------------------------------------------------------------------ #
    # 导航与状态
    # ------------------------------------------------------------------ #

    def set_extra_http_headers(self, headers: Optional[dict] = None) -> bool:
        """给浏览器上下文设置站点 API 所需的公共请求头。"""
        if not headers:
            return True
        try:
            self.context.set_extra_http_headers(dict(headers))
            return True
        except Exception as exc:  # noqa: BLE001 - 某些驱动/假对象可能不支持
            log.debug(f"设置浏览器公共请求头失败: {exc}")
            return False

    def seed_auth_state(self, user_id: Optional[int]) -> bool:
        """在下一次导航前写入 GoRouter 前端路由守卫需要的 localStorage。"""
        if not user_id:
            return False
        try:
            value = int(user_id)
        except (TypeError, ValueError):
            return False
        script = f"""
        (() => {{
          try {{
            const id = {value};
            localStorage.setItem('uid', String(id));
            localStorage.setItem('user', JSON.stringify({{id}}));
          }} catch (_) {{}}
        }})();
        """
        try:
            self.page.add_init_script(script)
            return True
        except Exception as exc:  # noqa: BLE001 - 某些驱动/假对象可能不支持
            log.debug(f"设置浏览器 localStorage 登录态失败: {exc}")
            return False

    def goto(self, url: str, timeout: Optional[int] = None) -> PageState:
        ms = int((timeout or self.cfg.browser.timeout) * 1000)
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=ms)
        except Exception as exc:  # noqa: BLE001 - 质询页常常触发超时，但页面已在
            log.debug(f"goto 异常（继续判定页面状态）: {exc}")
        return self.state()

    def state(self) -> PageState:
        try:
            url = self.page.url
            title = self.page.title()
            html = self.page.content()
        except Exception as exc:  # noqa: BLE001 - 质询页刷新期间读不到，视为仍在质询
            log.debug(f"读取页面状态失败: {exc}")
            return PageState(challenge=detect.MANAGED_CHALLENGE)
        return PageState(url=url, title=title, html=html,
                         challenge=detect.page_challenge_type(html, title, url))

    # 轮询期间用的轻量探针：整页 HTML 留在浏览器里扫，只把命中结果传回来。
    # 业务页面（SPA）的 content() 动辄几 MB，每秒拉一次纯属浪费。
    _PROBE_SCRIPT = """
    (markers) => {
      const root = document.documentElement;
      const html = ((root && root.outerHTML) || '').toLowerCase();
      const has = (list) => {
        for (let i = 0; i < list.length; i++) {
          if (html.indexOf(list[i]) >= 0) return true;
        }
        return false;
      };
      return {
        url: location.href,
        title: document.title || '',
        challenge: has(markers.challenge),
        js: has(markers.js),
        turnstile: has(markers.turnstile),
        waf: has(markers.waf),
        login: has(markers.login),
        password: html.indexOf('type="password"') >= 0
          || html.indexOf("type='password'") >= 0,
      };
    }
    """

    def quick_state(self) -> PageState:
        """轻量状态判定。驱动不支持时自动退回 state()（整页 HTML）。"""
        try:
            probe = self.page.evaluate(self._PROBE_SCRIPT, detect.probe_markers())
            challenge = detect.classify_probe(probe)
        except Exception as exc:  # noqa: BLE001 - 探针不可用就退回完整判定，不能影响结果
            log.debug(f"轻量状态探针不可用，退回整页判定: {type(exc).__name__}: {exc}")
            return self.state()
        return PageState(url=str(probe.get("url") or ""), title=str(probe.get("title") or ""),
                         html="", challenge=challenge)

    def wait_until_passed(self, timeout: Optional[int] = None, poll: float = 1.0) -> PageState:
        """轮询等待质询自解，而不是固定 sleep。

        轮询间隔按 1.5 倍递增（上限 3s）：刚开始密集探测以便尽早发现自解，
        长时间没动静时降频，把 60s 内的几十次整页探测压到十几次。
        """
        limit = timeout if timeout is not None else self.cfg.browser.timeout
        deadline = time.time() + max(1.0, float(limit))
        state = self.quick_state()
        last = state.challenge
        interval = max(0.05, float(poll))
        while True:
            if state.passed or state.challenge in (detect.WAF_BLOCK, detect.LOGIN_REQUIRED):
                return state
            now_ts = time.time()
            if now_ts >= deadline:
                return state
            time.sleep(min(interval, max(0.05, deadline - now_ts)))
            interval = min(3.0, interval * 1.5)
            state = self.quick_state()
            if state.challenge != last:
                log.debug(f"页面状态变化: {state.brief()}")
                last = state.challenge

    # ------------------------------------------------------------------ #
    # 会话信息收割
    # ------------------------------------------------------------------ #

    def user_agent(self) -> str:
        try:
            return self.page.evaluate("() => navigator.userAgent") or ""
        except Exception:  # noqa: BLE001
            return ""

    def accept_language(self) -> str:
        """按浏览器真实语言列表拼 Accept-Language，和 UA 一起构成 cf_clearance 的上下文。"""
        try:
            langs = self.page.evaluate(
                "() => (navigator.languages || []).join(',')"
            )
        except Exception:  # noqa: BLE001
            langs = ""
        parts = [p.strip() for p in str(langs or "").split(",") if p.strip()]
        if not parts:
            return self.cfg.browser.locale
        out = [parts[0]]
        for idx, part in enumerate(parts[1:4], start=1):
            out.append(f"{part};q={max(0.1, 1 - idx * 0.1):.1f}")
        return ",".join(out)

    def cookies(self) -> list:
        try:
            return list(self.context.cookies() or [])
        except Exception:  # noqa: BLE001
            return []

    def cookie_dict(self) -> dict:
        """只取本站域下的 cookie，避免把无关域的 cookie 混进请求头。"""
        from urllib.parse import urlparse

        host = (urlparse(self.account.base_url).hostname or "").lower()
        out: dict = {}
        for item in self.cookies():
            name = item.get("name")
            if not name:
                continue
            domain = (item.get("domain") or "").lstrip(".").lower()
            if domain and host and not (host == domain or host.endswith("." + domain)):
                continue
            out[name] = item.get("value") or ""
        return out

    def turnstile_token(self) -> str:
        """读取页面上真实 Turnstile widget 生成的 token，不生成、不持久化、不复用旧 token。"""
        script = """
        () => {
          try {
            if (window.__newapi_turnstile_token) {
              return window.__newapi_turnstile_token;
            }
          } catch (_) {}
          const inputs = Array.from(
            document.querySelectorAll('input[name="cf-turnstile-response"]')
          );
          for (const input of inputs) {
            if (input.value) return input.value;
          }
          try {
            if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
              const value = window.turnstile.getResponse();
              if (value) return value;
            }
          } catch (_) {}
          return '';
        }
        """
        try:
            value = self.page.evaluate(script)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"读取 Turnstile token 失败: {exc}")
            return ""
        return str(value or "").strip()

    def mount_turnstile(self, site_key: str) -> bool:
        """在当前业务页挂载官方 Turnstile widget，不自行生成 token。"""
        key = str(site_key or "").strip()
        if not key:
            return False
        script = """
        async (siteKey) => {
          try {
            const id = '__newapi_checkin_turnstile';
            const old = document.getElementById(id);
            if (old) old.remove();
            const box = document.createElement('div');
            box.id = id;
            box.style.position = 'fixed';
            box.style.right = '20px';
            box.style.bottom = '20px';
            box.style.zIndex = '2147483647';
            box.style.background = '#fff';
            box.style.padding = '8px';
            box.style.borderRadius = '6px';
            document.body.appendChild(box);

            const ready = () => window.turnstile &&
              typeof window.turnstile.render === 'function';
            if (!ready()) {
              await new Promise((resolve, reject) => {
                const existing = document.querySelector(
                  'script[src*="challenges.cloudflare.com/turnstile/v0/api.js"]'
                );
                const script = existing || document.createElement('script');
                let settled = false;
                const done = (fn) => (value) => {
                  if (settled) return;
                  settled = true;
                  fn(value);
                };
                script.addEventListener('load', done(resolve), {once: true});
                script.addEventListener('error', done(reject), {once: true});
                if (!existing) {
                  script.src =
                    'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
                  script.async = true;
                  script.defer = true;
                  document.head.appendChild(script);
                }
                setTimeout(() => {
                  if (ready()) done(resolve)();
                  else done(reject)(new Error('Turnstile script timeout'));
                }, 15000);
              });
            }
            if (!ready()) return false;
            window.__newapi_turnstile_token = '';
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = 'cf-turnstile-response';
            box.appendChild(input);
            const widget = window.turnstile.render(box, {
              sitekey: siteKey,
              callback: (token) => {
                window.__newapi_turnstile_token = String(token || '');
                input.value = window.__newapi_turnstile_token;
              },
              'expired-callback': () => {
                window.__newapi_turnstile_token = '';
                input.value = '';
              },
              'error-callback': () => {
                window.__newapi_turnstile_token = '';
                input.value = '';
              },
            });
            return widget !== undefined && widget !== null;
          } catch (_) {
            return false;
          }
        }
        """
        try:
            result = self.page.evaluate(script, key)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"挂载 Turnstile widget 失败: {exc}")
            return False
        return bool(result)

    def wait_for_turnstile_token(self, timeout: Optional[int] = None,
                                 poll: float = 0.5) -> str:
        """等待当前页面上的官方 widget 完成并回填 token。"""
        limit = timeout if timeout is not None else self.cfg.browser.timeout
        deadline = time.time() + max(0.0, float(limit))
        while True:
            token = self.turnstile_token()
            if token:
                log.debug(f"已获得 Turnstile token（长度 {len(token)}，内容不打印）")
                return token
            if time.time() >= deadline:
                return ""
            time.sleep(max(0.1, poll))

    # ------------------------------------------------------------------ #
    # 截图与定位
    # ------------------------------------------------------------------ #

    def viewport(self) -> Tuple[int, int]:
        try:
            size = self.page.viewport_size
            if size:
                return int(size["width"]), int(size["height"])
        except Exception:  # noqa: BLE001
            pass
        try:
            width, height = self.page.evaluate("() => [window.innerWidth, window.innerHeight]")
            if width and height:
                return int(width), int(height)
        except Exception:  # noqa: BLE001
            pass
        return int(self.cfg.browser.window[0]), int(self.cfg.browser.window[1])

    def screenshot(self, clip: Optional[dict] = None, full_page: bool = False) -> bytes:
        try:
            # scale="css" 让截图按 CSS 像素输出，避免高 DPR 下图片过大浪费 token
            return self.page.screenshot(clip=clip, full_page=full_page, type="png", scale="css")
        except Exception as exc:  # noqa: BLE001
            log.debug(f"截图失败: {exc}")
            return b""

    def inject_cookies(self, cookie_header: str) -> int:
        """把配置里的登录 cookie 注入浏览器上下文。

        S4 页内直发依赖这一步：浏览器 profile 本身并没有登录态，
        不注入的话页内 fetch 会直接 401。
        """
        from urllib.parse import urlparse

        from ..client import parse_cookie_header

        parsed = urlparse(self.account.base_url)
        host = parsed.hostname
        if not host:
            return 0
        secure = parsed.scheme == "https"
        items = [
            {"name": name, "value": value, "domain": host, "path": "/",
             "secure": secure, "sameSite": "Lax"}
            for name, value in parse_cookie_header(cookie_header).items()
        ]
        if not items:
            return 0
        try:
            self.context.add_cookies(items)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"注入 cookie 失败: {exc}")
            return 0
        names = ", ".join(sorted(item["name"] for item in items))
        log.debug(f"已向浏览器注入 {len(items)} 条 cookie（名称: {names}）")
        return len(items)

    def find_element_box(self, selectors) -> Optional[dict]:
        """返回第一个可见且有实际尺寸的元素包围盒。"""
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if locator.count() == 0:
                    continue
                box = locator.bounding_box()
            except Exception:  # noqa: BLE001 - 选择器不合法/元素消失都跳过
                continue
            if box and box.get("width", 0) > 4 and box.get("height", 0) > 4:
                log.debug(f"命中选择器 {selector} -> {box}")
                return box
        return None

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #

    def click_at(self, x: float, y: float) -> bool:
        try:
            # Camoufox 开了 humanize 后浏览器层已经做了人性化轨迹，不再叠加
            if self.cfg.browser.humanize and self.name != "camoufox":
                from ..ai.humanize import move_mouse_human

                move_mouse_human(self.page, x, y)
            else:
                self.page.mouse.move(x, y)
            time.sleep(0.12)
            self.page.mouse.click(x, y)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug(f"点击 ({x:.0f},{y:.0f}) 失败: {exc}")
            return False

    def click_turnstile(self) -> bool:
        """不依赖 AI 的 Turnstile 点击：复选框固定在组件左侧、垂直居中。"""
        box = self.find_element_box(TURNSTILE_SELECTORS)
        if not box:
            return False
        x = box["x"] + min(30.0, box["width"] / 2)
        y = box["y"] + box["height"] / 2
        log.debug(f"点击 Turnstile 复选框 @ ({x:.0f},{y:.0f})")
        return self.click_at(x, y)

    def fill_captcha(self, text: str) -> bool:
        for selector in CAPTCHA_INPUT_SELECTORS:
            try:
                locator = self.page.locator(selector).first
                if locator.count() == 0:
                    continue
                locator.click()
                locator.fill("")
                locator.type(text, delay=90)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    # ------------------------------------------------------------------ #
    # S4：在页面上下文里直发请求（天然携带完整指纹）
    # ------------------------------------------------------------------ #

    _FETCH_SCRIPT = """
    async ({url, method, headers, body}) => {
      try {
        const resp = await fetch(url, {
          method: method,
          headers: headers || {},
          body: body || undefined,
          credentials: 'include',
          redirect: 'follow',
        });
        const text = await resp.text();
        const hdrs = {};
        resp.headers.forEach((v, k) => { hdrs[k] = v; });
        return {ok: true, status: resp.status, body: text, headers: hdrs};
      } catch (e) {
        return {ok: false, status: 0, body: String(e), headers: {}};
      }
    }
    """

    def fetch_in_page(self, url: str, method: str = "GET",
                      headers: Optional[dict] = None, body: Optional[str] = None) -> dict:
        try:
            result = self.page.evaluate(
                self._FETCH_SCRIPT,
                {"url": url, "method": method, "headers": headers or {}, "body": body},
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": 0, "body": f"页内 fetch 执行失败: {exc}"}
        return result if isinstance(result, dict) else {"ok": False, "status": 0, "body": ""}

    # ------------------------------------------------------------------ #
    # 排障产物：无头 VPS 上唯一的现场证据
    # ------------------------------------------------------------------ #

    def dump_artifacts(self, target_dir: Path, tag: str = "fail") -> Optional[Path]:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            png = self.screenshot(full_page=True)
            if png:
                (target_dir / f"{tag}.png").write_bytes(png)
            try:
                html = self.page.content()
            except Exception:  # noqa: BLE001
                html = ""
            if html:
                (target_dir / f"{tag}.html").write_text(html, encoding="utf-8")
            return target_dir
        except Exception as exc:  # noqa: BLE001
            log.debug(f"落盘排障产物失败: {exc}")
            return None
