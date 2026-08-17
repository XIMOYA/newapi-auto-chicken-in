"""通过 CDP 接管**已经开着的真实 Chrome**，只为取一个 Turnstile token。

为什么不能沿用 driver_patchright / driver_camoufox 那套 launch 流程：
实测 TaBiAI 的 Turnstile 会拒绝脚本新启的浏览器环境（不管 patchright 补得多干净，
也不管是否 headless），只有用户日常在用的那个 Chrome 才能出 token。所以这里
`connect_over_cdp` 挂到用户自己启动的实例上（chrome --remote-debugging-port=9222），
借它的真实环境完成质询，取完 token 就把标签页收掉，不动用户其它页面。

另外站点对 Turnstile 有频率限制：同一环境 20 分钟内反复 reset 是拿不到新 token 的，
所以取 token 必须串行 + 间隔，这部分节流由 runner 统一控制，本模块只管取一次。
"""

from __future__ import annotations

import time
from typing import Tuple

from .. import logger as log
from ..config import Account, TabiAIConfig
from .driver_base import DriverUnavailable

# 直接复用 driver_base 里那套「挂载官方 widget + 回调回填」的脚本，
# 保证两条链路对 token 的获取方式完全一致（不自造 token、不复用旧 token）。
_MOUNT_JS = """
async (siteKey) => {
  const id = '__newapi_tabiai_turnstile';
  const old = document.getElementById(id);
  if (old) old.remove();
  window.__newapi_turnstile_token = '';
  const box = document.createElement('div');
  box.id = id;
  box.style.cssText =
    'position:fixed;left:50%;top:24px;transform:translateX(-50%);z-index:2147483647;' +
    'padding:12px;background:#fff;border-radius:8px;box-shadow:0 4px 18px rgba(0,0,0,.25)';
  document.body.appendChild(box);
  const ready = () => window.turnstile && typeof window.turnstile.render === 'function';
  if (!ready()) {
    await new Promise((resolve, reject) => {
      const existing = document.querySelector(
        'script[src*="challenges.cloudflare.com/turnstile/v0/api.js"]');
      const script = existing || document.createElement('script');
      let settled = false;
      const done = (fn) => (v) => { if (!settled) { settled = true; fn(v); } };
      script.addEventListener('load', done(resolve));
      script.addEventListener('error', done(() => reject(new Error('turnstile api.js 加载失败'))));
      if (!existing) {
        script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
        script.async = true;
        document.head.appendChild(script);
      } else if (ready()) {
        done(resolve)(null);
      }
      setTimeout(done(resolve), 15000);
    });
  }
  if (!ready()) return false;
  window.turnstile.render(box, {
    sitekey: siteKey,
    callback: (token) => { window.__newapi_turnstile_token = String(token || ''); },
    'expired-callback': () => { window.__newapi_turnstile_token = ''; },
    'error-callback': () => { window.__newapi_turnstile_token = ''; },
  });
  return true;
}
"""

_READ_JS = "() => String(window.__newapi_turnstile_token || '')"


def _sync_playwright():
    """优先用 patchright（补过指纹），没装再退回原版 playwright。"""
    try:
        from patchright.sync_api import sync_playwright

        return sync_playwright
    except ImportError:
        pass
    try:
        from playwright.sync_api import sync_playwright

        return sync_playwright
    except ImportError as exc:
        raise DriverUnavailable(
            "取 Turnstile token 需要 patchright 或 playwright -> 执行: pip install patchright"
        ) from exc


def _site_key(page, account: Account) -> str:
    """从站点公开状态接口读 Turnstile site key，避免把 key 写死在配置里。"""
    script = """
    async (url) => {
      try {
        const resp = await fetch(url, {
          credentials: 'include',
          headers: { 'Accept': 'application/json, text/plain, */*' },
        });
        return await resp.text();
      } catch (e) { return ''; }
    }
    """
    try:
        raw = page.evaluate(script, account.base_url + "/api/status")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"读取 /api/status 失败: {exc}")
        return ""
    import json

    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return ""
    payload = data.get("data") if isinstance(data, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    for key in ("turnstile_site_key", "TurnstileSiteKey", "turnstile_sitekey"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def fetch_turnstile_token(tabiai: TabiAIConfig, account: Account) -> Tuple[str, str]:
    """接管真实 Chrome 取一个 Turnstile token；返回 (token, 失败原因)。

    只新开一个标签页并在结束时关掉，不碰用户已有的窗口和标签。
    """
    sync_playwright = _sync_playwright()
    manager = sync_playwright()
    playwright = manager.__enter__()
    browser = None
    page = None
    try:
        try:
            browser = playwright.chromium.connect_over_cdp(tabiai.cdp_url)
        except Exception as exc:  # noqa: BLE001
            return "", (
                f"连不上真实 Chrome（{tabiai.cdp_url}）：{type(exc).__name__}: {exc}；"
                "请先用 --remote-debugging-port=9222 启动 Chrome 并保持登录状态"
            )[:300]

        contexts = list(browser.contexts or [])
        context = contexts[0] if contexts else browser.new_context()
        page = context.new_page()
        page.set_default_timeout(max(10, tabiai.token_timeout) * 1000)
        # 必须在站点自己的源上挂 widget，Turnstile 会校验域名
        page.goto(account.base_url + "/console", wait_until="domcontentloaded")

        key = _site_key(page, account)
        if not key:
            return "", "站点 /api/status 没有返回 turnstile_site_key，无法挂载 widget"

        if not page.evaluate(_MOUNT_JS, key):
            return "", "Turnstile widget 挂载失败（api.js 未加载或被拦截）"

        deadline = time.time() + max(10, tabiai.token_timeout)
        log.info(f"已在真实 Chrome 中挂载 Turnstile，最多等待 {tabiai.token_timeout} 秒"
                 "（如出现交互质询请在该浏览器里点一下）")
        while time.time() < deadline:
            try:
                token = str(page.evaluate(_READ_JS) or "")
            except Exception as exc:  # noqa: BLE001
                return "", f"读取 token 失败: {type(exc).__name__}: {exc}"[:200]
            if token:
                log.debug(f"已获得 Turnstile token（长度 {len(token)}，内容不打印）")
                return token, ""
            time.sleep(0.5)
        return "", (
            f"等待 {tabiai.token_timeout} 秒仍未取得 Turnstile token；"
            "站点对同一环境有频率限制（实测约 20 分钟），稍后再试或换出口 IP"
        )
    finally:
        if page is not None and not tabiai.keep_page:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        # connect_over_cdp 的 close 只断开连接，不会关掉用户自己的 Chrome
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            manager.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

