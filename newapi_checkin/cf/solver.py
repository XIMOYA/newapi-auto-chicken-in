"""过盾编排：S2 等自解 -> S3 AI 介入 -> S4 页内直发 -> S5 人工兜底。

一个关键前提：浏览器 profile 本身没有登录态，所以进站前先把配置里的登录 cookie
注入浏览器上下文；这样过盾后可以直接在页面里完成签到（S4），
不依赖「cookie 能否迁移回 curl_cffi」这件不确定的事。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .. import client as api
from .. import logger as log
from ..ai import prompts
from ..config import (
    GITHUB_PROTOCOL_TABI,
    LOGIN_METHOD_GITHUB_COOKIE,
    SELF_PATH,
    SHOTS_DIR,
    Account,
    Config,
)
from ..utils import now
from . import detect
from .driver_base import (
    CAPTCHA_IMG_SELECTORS,
    TURNSTILE_SELECTORS,
    BrowserDriver,
    DriverUnavailable,
    PageState,
)
from .session_store import CFSession, cookie_expiry

MANUAL_TIMEOUT = 300          # S5 人工兜底最长等待
AI_ROUNDS = 3                 # S3 最多来回几轮
SHORT_WAIT = 30               # 每次交互后等待质询自解的上限
AI_ASSIST_BUDGET = 150        # S3 整段（含所有轮次与 AI 请求）的总时长上限
AI_CALL_RESERVE = 5           # 剩余时间不足这么多秒时不再发起新的 AI 请求


@dataclass
class SolveOutcome:
    ok: bool = False
    strategy: str = "S2"
    cf: Optional[CFSession] = None
    api_result: Optional[api.ApiResult] = None
    detail: str = ""
    terminal: bool = False        # True 表示重试也没用（例如 WAF 硬封禁）
    result_kind: Optional[str] = None  # 失败原因的业务类型，避免统一映射成 WAF


def _make_driver(cfg: Config, account: Account, options) -> BrowserDriver:
    if cfg.browser.driver == "patchright":
        from .driver_patchright import PatchrightDriver

        return PatchrightDriver(cfg, account, options)
    from .driver_camoufox import CamoufoxDriver

    return CamoufoxDriver(cfg, account, options)


def solve(*, cfg: Config, account: Account, exit_ip: Optional[str], options,
          ai=None) -> SolveOutcome:
    """对外唯一入口。任何异常都转成 SolveOutcome，不向上抛。"""
    try:
        driver = _make_driver(cfg, account, options)
    except DriverUnavailable as exc:
        return SolveOutcome(False, "S2", detail=str(exc))

    try:
        with driver:
            return _run(driver, cfg, account, exit_ip, options, ai)
    except DriverUnavailable as exc:
        return SolveOutcome(False, "S2", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - 过盾失败不能让整轮崩掉
        if log.is_verbose():
            import traceback

            log.debug(traceback.format_exc())
        return SolveOutcome(False, "S2", detail=f"过盾过程异常: {type(exc).__name__}: {exc}")


def _run(driver: BrowserDriver, cfg: Config, account: Account, exit_ip: Optional[str],
         options, ai) -> SolveOutcome:
    is_github = account.login_method == LOGIN_METHOD_GITHUB_COOKIE
    log.info(f"启动 {driver.name} 过盾（profile: {account.profile_dir.name}）")
    # NewAPI 模式注入站点登录 Cookie；GitHub 模式只需要站点 CF 上下文，
    # GitHub user_session 由 OAuth HTTP 客户端按 github.com 域名单独发送。
    driver.inject_cookies(account.cookie)
    if not is_github and account.user_id:
        if driver.seed_auth_state(account.user_id):
            log.debug(f"浏览器入口已预置 localStorage 登录态 user_id={account.user_id}")
        if driver.set_extra_http_headers({"New-Api-User": str(account.user_id)}):
            log.debug(f"浏览器入口已设置 New-Api-User={account.user_id}")

    if is_github and account.github_protocol == GITHUB_PROTOCOL_TABI:
        # TaBi 的 state 接口只接受 POST，浏览器先进入公开登录页完成过盾。
        browser_url = account.api("/sign-in")
    else:
        browser_url = (
            account.api("/api/oauth/state?mode=login")
            if is_github else account.browser_url
        )
    state = driver.goto(browser_url)
    log.debug(f"浏览器入口: {browser_url}")
    log.debug(f"首屏状态: {state.brief()}")
    strategy = "S2"

    # NewAPI 模式必须先确认站点登录态；GitHub 模式进入公开 OAuth state
    # 地址即可开始处理站点 Cloudflare，不能把未完成 OAuth 的页面误判为失效。
    if state.challenge == detect.LOGIN_REQUIRED and not is_github:
        artifact = _dump(driver, cfg, account, "login-required")
        return SolveOutcome(
            False,
            "S2",
            detail=_with_artifact(
                "浏览器被重定向到登录页：当前 cookie 不是有效的登录会话，"
                "请复制完整登录 cookie（不要只复制 cf_clearance）",
                artifact,
            ),
            terminal=True,
            result_kind=api.LOGIN_REQUIRED,
        )
    if state.challenge == detect.LOGIN_REQUIRED and is_github:
        log.debug("GitHub 模式忽略站点登录页判定：OAuth Cookie 将在 GitHub authorize 阶段校验")
        state = PageState(url=state.url, title=state.title, challenge=None)

    # ---------------- S2：等质询自解 ----------------
    if not state.passed:
        if state.challenge == detect.WAF_BLOCK:
            artifact = _dump(driver, cfg, account, "waf")
            return SolveOutcome(False, "S2", detail=_with_artifact(
                "WAF 硬封禁，脚本无法绕过，需要换出口 IP", artifact),
                terminal=True, result_kind=api.WAF_BLOCKED)
        if state.challenge == detect.TURNSTILE:
            # 先用几何法点一下，能省掉一次 AI 调用
            if driver.click_turnstile():
                log.debug("已用 DOM 几何定位点击 Turnstile 复选框")
        state = driver.wait_until_passed()
        log.debug(f"S2 等待结束: {state.brief()}")

    # ---------------- S3：AI 辅助 ----------------
    if not state.passed and state.challenge != detect.WAF_BLOCK and ai is not None:
        log.info("质询未自动通过，转入 S3 由 AI 判断页面状态")
        strategy = "S3"
        state = _ai_assist(driver, cfg, ai, state, account)

    # ---------------- S5：人工兜底 ----------------
    if not state.passed and options is not None and getattr(options, "manual", False):
        state = _manual(driver)
        if state.passed:
            strategy = "S5"

    # DOM 判定不是最终裁判：Cloudflare 会把 JS 检测脚本注入到已经通过的正常页面，
    # 站点自己的文案也可能撞上封禁关键词。放弃之前先用页内 API 请求实测一次。
    if not state.passed and state.challenge != detect.LOGIN_REQUIRED:
        if _page_session_alive(driver, account):
            log.warn("DOM 仍被判为质询页，但页内 API 请求成功，按已过盾继续")
            state = PageState(url=state.url, title=state.title, challenge=None)

    if not state.passed:
        artifact = _dump(driver, cfg, account, "cf-fail")
        label = detect.CHALLENGE_LABEL.get(state.challenge or "", "质询页")
        return SolveOutcome(False, strategy, detail=_with_artifact(f"仍停在{label}", artifact),
                            terminal=state.challenge == detect.WAF_BLOCK)

    # ---------------- 收割会话 ----------------
    cf = _harvest(driver, account, exit_ip)
    log.debug(f"收割 cookie {len(cf.cookies)} 条"
              + ("（含 cf_clearance）" if "cf_clearance" in cf.cookies else "（无 cf_clearance）"))

    if is_github:
        # GitHub Cookie 的签到动作绑定 OAuth 回调，不能在未登录的站点页面
        # 直接 POST NewAPI checkin；把 CF session 交回 runner 的 OAuth 客户端。
        return SolveOutcome(True, strategy, cf=cf,
                            detail="站点过盾完成，交回 GitHub OAuth 登录链路")

    # ---------------- S4：直接在页面里完成签到 ----------------
    result = _checkin_in_page(driver, account)
    if result is not None and result.kind == api.TURNSTILE_REQUIRED:
        # New API 的 Turnstile 中间件要求把当前 widget 生成的 token 放到
        # ?turnstile=...；token 是短时、一次性的，只在当前浏览器上下文立即使用。
        token = driver.turnstile_token()
        if not token:
            # 页面上可能已有 Turnstile 组件（业务页或质询残留），先尝试直接点击
            clicked = driver.click_turnstile()
            if not clicked and ai is not None:
                clicked = _click_turnstile_with_ai(driver, ai)
            if clicked:
                log.debug("检测到站点要求 Turnstile，已尝试点击当前页面 widget")
            token = driver.turnstile_token()
        if not token:
            # 业务页面上通常没有 Turnstile（签到接口才要求），从站点状态接口读
            # 公开 site key 后挂载官方 widget，再由交互循环主动应对质询。
            site_key = _turnstile_site_key(driver, account)
            if site_key and driver.mount_turnstile(site_key):
                log.debug("已在当前页面挂载官方 Turnstile widget，开始交互等待 token")

        wait = MANUAL_TIMEOUT if getattr(options, "manual", False) else cfg.browser.timeout
        if not token:
            token = _wait_turnstile_token_interactive(driver, ai, wait)
        if token:
            result = _checkin_in_page(driver, account, token)
        else:
            if getattr(options, "manual", False):
                detail = (
                    "站点要求 Turnstile token，但人工模式等待后页面仍没有生成有效 token；"
                    "请确认远程浏览器中的验证已完成，且当前出口 IP 未变化"
                )
            else:
                detail = (
                    "站点要求 Turnstile token，但无图形模式下等待后没有生成有效 token；"
                    "Turnstile 判定当前环境（数据中心 IP）不可信，进入交互质询且未能自动通过；"
                    "可尝试给该账号配置住宅代理，或联系站点管理员关闭该要求"
                )
            return SolveOutcome(False, "S4", cf=cf, api_result=result, detail=detail,
                                result_kind=api.TURNSTILE_REQUIRED)

    if result is not None and result.kind in (api.SUCCESS, api.ALREADY_DONE):
        return SolveOutcome(True, "S4", cf=cf, api_result=result,
                            detail=f"过盾于 {strategy}，签到在浏览器上下文内完成")
    if result is not None and result.kind != api.NETWORK_ERROR:
        return SolveOutcome(False, "S4", cf=cf, api_result=result,
                            detail=result.message or "浏览器内签到失败")

    # 页内直发不可用时，把 cookie 交回快路径由上层重发
    return SolveOutcome(True, strategy, cf=cf, detail="已过盾，交回 HTTP 快路径重发")


# --------------------------------------------------------------------------- #
# S3：AI 辅助
# --------------------------------------------------------------------------- #


def _page_session_alive(driver: BrowserDriver, account: Optional[Account]) -> bool:
    """用页内 fetch 实测一次 /api/user/self，作为「到底过没过盾」的最终裁判。

    DOM 关键词判定天生会误报：CF 开了 Bot Fight 后会把 JS 检测脚本注入到已经
    通过的业务页面里。一旦误报，S3 就会陷入「AI 说过了 / DOM 说没过」的死循环，
    一直等到超时。真正能定论的只有「站点 API 是否正常返回业务数据」。
    """
    if account is None:
        return False
    raw = driver.fetch_in_page(
        account.base_url + SELF_PATH, "GET",
        {"Accept": "application/json, text/plain, */*"},
    )
    if not raw.get("ok"):
        return False
    status, body, resp_headers, data = _parse_raw(raw)
    if detect.analyze(status, resp_headers, body).blocked:
        return False
    result = api.classify_self(status, data, body[:160])
    if not result.ok or not result.user_id:
        return False
    if account.user_id is None:
        account.user_id = result.user_id
        log.debug(f"页内实测确认已过盾，user_id={result.user_id}")
    return True


def _ai_assist(driver: BrowserDriver, cfg: Config, ai, state: PageState,
               account: Optional[Account] = None) -> PageState:
    wait = min(SHORT_WAIT, cfg.browser.timeout)
    # S3 每轮都要截图 + 调模型，没有总预算时「3 轮 × 重试 × 60s」可以跑到十分钟，
    # 而且完全绕过 browser.timeout。这里给整段 S3 一个绝对截止时间。
    deadline = time.monotonic() + AI_ASSIST_BUDGET
    for round_no in range(1, AI_ROUNDS + 1):
        if time.monotonic() >= deadline:
            log.warn(f"S3 已用满 {AI_ASSIST_BUDGET}s 预算，停止 AI 辅助")
            break
        shot = driver.screenshot()
        if not shot:
            log.debug("截图为空，S3 无法继续")
            break
        verdict = ai.classify_page(shot)
        log.info(f"AI 第 {round_no} 轮判定: {verdict}")

        if verdict.state == prompts.PASSED:
            fresh = driver.state()
            if fresh.passed:
                return fresh
            # AI 与 DOM 打架时，让站点 API 来裁决，而不是无限期地相信 DOM
            if _page_session_alive(driver, account):
                log.warn("AI 判定已过盾且页内 API 请求成功，以实测结果为准")
                return PageState(url=fresh.url, title=fresh.title, challenge=None)
            log.debug("AI 认为已过盾但 DOM 仍是质询页且页内 API 不通，继续等待")
            state = driver.wait_until_passed(timeout=wait)
            if state.passed:
                return state
            continue

        if verdict.state == prompts.TURNSTILE_CHECKBOX:
            if _click_turnstile_with_ai(driver, ai):
                state = driver.wait_until_passed(timeout=wait)
                if state.passed:
                    return state
            continue

        if verdict.state == prompts.IMAGE_CAPTCHA:
            if _solve_image_captcha(driver, ai):
                state = driver.wait_until_passed(timeout=wait)
                if state.passed:
                    return state
            continue

        if verdict.state == prompts.GRID_CAPTCHA:
            if _solve_grid_captcha(driver, ai):
                state = driver.wait_until_passed(timeout=wait)
                if state.passed:
                    return state
            continue

        if verdict.state in (prompts.RATE_LIMITED, prompts.LOGIN_REQUIRED, prompts.ERROR_PAGE):
            log.warn(f"AI 判定为 {verdict.state}，这类情况 AI 处理不了，结束 S3")
            break

        # cf_waiting / unknown：继续等
        state = driver.wait_until_passed(timeout=wait)
        if state.passed:
            return state

    return driver.state()


def _click_turnstile_with_ai(driver: BrowserDriver, ai) -> bool:
    """优先截 iframe 局部图定位——局部图的坐标精度远高于整页图。"""
    box = driver.find_element_box(TURNSTILE_SELECTORS)
    if box:
        clip = {
            "x": max(0.0, box["x"] - 10),
            "y": max(0.0, box["y"] - 10),
            "width": box["width"] + 20,
            "height": box["height"] + 20,
        }
        shot = driver.screenshot(clip=clip)
        point = ai.locate(shot, int(clip["width"]), int(clip["height"]),
                          prompts.TARGET_TURNSTILE) if shot else None
        if point:
            x = clip["x"] + point[0] * clip["width"]
            y = clip["y"] + point[1] * clip["height"]
            log.info(f"AI 定位复选框 -> ({x:.0f}, {y:.0f})")
            return driver.click_at(x, y)
        log.debug("AI 未能定位，退回几何法点击")
        return driver.click_turnstile()

    width, height = driver.viewport()
    shot = driver.screenshot()
    point = ai.locate(shot, width, height, prompts.TARGET_TURNSTILE) if shot else None
    if not point:
        return False
    x, y = point[0] * width, point[1] * height
    log.info(f"AI 整页定位复选框 -> ({x:.0f}, {y:.0f})")
    return driver.click_at(x, y)


def _solve_image_captcha(driver: BrowserDriver, ai) -> bool:
    """站点自带的字符验证码：裁剪图片 -> AI OCR -> 填入 -> 回车提交。"""
    box = driver.find_element_box(CAPTCHA_IMG_SELECTORS)
    if not box:
        log.debug("页面上找不到验证码图片元素")
        return False
    shot = driver.screenshot(clip={
        "x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"],
    })
    if not shot:
        return False
    text = ai.ocr(shot)
    if not text:
        log.debug("AI 未能识别验证码")
        return False
    log.info(f"AI 识别验证码: {text}")
    if not driver.fill_captcha(text):
        log.debug("找不到验证码输入框")
        return False
    try:
        driver.page.keyboard.press("Enter")
    except Exception as exc:  # noqa: BLE001
        log.debug(f"提交验证码失败: {exc}")
        return False
    return True


def _solve_grid_captcha(driver: BrowserDriver, ai) -> bool:
    """点选式验证码：让 AI 给出多个点，依次点击后回车。"""
    width, height = driver.viewport()
    shot = driver.screenshot()
    if not shot:
        return False
    points = ai.locate_grid(shot, width, height, "题目要求点选的所有图块")
    if not points:
        log.debug("AI 未能给出可点击的图块")
        return False
    log.info(f"AI 给出 {len(points)} 个点选目标")
    clicked = 0
    for nx, ny in points[:9]:
        if driver.click_at(nx * width, ny * height):
            clicked += 1
    if not clicked:
        return False
    try:
        driver.page.keyboard.press("Enter")
    except Exception:  # noqa: BLE001
        pass
    return True


# --------------------------------------------------------------------------- #
# S5：人工兜底
# --------------------------------------------------------------------------- #


def _manual(driver: BrowserDriver) -> PageState:
    log.warn("进入人工兜底模式：请在弹出的浏览器窗口里手动完成验证")
    log.warn(f"最多等待 {MANUAL_TIMEOUT}s，通过后脚本会自动继续，并把会话存进 profile")
    return driver.wait_until_passed(timeout=MANUAL_TIMEOUT, poll=2.0)


# --------------------------------------------------------------------------- #
# S4：页内直发签到
# --------------------------------------------------------------------------- #


def _parse_raw(raw: dict):
    status = int(raw.get("status") or 0)
    body = str(raw.get("body") or "")
    headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    data = None
    try:
        parsed = json.loads(body)
        data = parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        data = None
    return status, body, headers, data


def _with_turnstile(path: str, token: Optional[str]) -> str:
    """给签到路径附加当前页面刚生成的 Turnstile token。"""
    if not token:
        return path
    parts = urlsplit(path)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.lower() != "turnstile"]
    query.append(("turnstile", token))
    return urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


def _wait_turnstile_token_interactive(driver: BrowserDriver, ai, timeout: int) -> str:
    """交互式等待 Turnstile token，而不是干等。

    Turnstile 在无头浏览器 + 数据中心 IP 下常进入交互式质询（勾选框/图片点选），
    只调用 wait_for_turnstile_token 干等永远拿不到 token。这里循环地：
      1. 轮询 token（官方 widget 自解时会回填）；
      2. 节流点击复选框（幂等：已通过时点击无害，未通过时是必经第一步）；
      3. 截图让 AI 判断是否出现图片点选质询并代为点选。

    截止时间在「发起 AI 请求之前」也要检查一次：一次视觉调用可能占掉几十秒，
    只在循环顶部检查会让实际耗时远远超出传入的 timeout。
    """
    deadline = time.monotonic() + max(1.0, float(timeout))
    last_click = 0.0
    grid_attempts = 0
    while True:
        token = driver.turnstile_token()
        if token:
            log.debug(f"已获得 Turnstile token（长度 {len(token)}，内容不打印）")
            return token
        if time.monotonic() >= deadline:
            return ""

        # 1) 节流点击复选框（每 2.5s 最多一次）
        now_ts = time.monotonic()
        if now_ts - last_click >= 2.5 and driver.find_element_box(TURNSTILE_SELECTORS):
            if driver.click_turnstile():
                last_click = now_ts
                log.debug("已点击 Turnstile 复选框，等待自解")
                time.sleep(1.2)
                token = driver.turnstile_token()
                if token:
                    return token

        # 2) AI 应对图片点选质询（最多 3 轮，避免烧太多视觉 token）
        if (ai is not None and grid_attempts < 3
                and deadline - time.monotonic() > AI_CALL_RESERVE):
            width, height = driver.viewport()
            shot = driver.screenshot()
            if shot:
                points = ai.locate_grid(shot, width, height, "题目要求点选的所有图块")
                if points:
                    grid_attempts += 1
                    clicked = 0
                    for nx, ny in points[:9]:
                        if driver.click_at(nx * width, ny * height):
                            clicked += 1
                    if clicked:
                        log.info(f"AI 点选了 {clicked} 个图块，继续等待 token")
                        time.sleep(2.0)
                        token = driver.turnstile_token()
                        if token:
                            return token
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        time.sleep(min(1.0, remaining))
    return ""


def _turnstile_site_key(driver: BrowserDriver, account: Account) -> Optional[str]:
    """从站点状态接口读取公开 Turnstile site key。"""
    raw = driver.fetch_in_page(
        account.base_url + "/api/status",
        "GET",
        {"Accept": "application/json, text/plain, */*"},
    )
    if not raw.get("ok"):
        log.debug(f"读取站点 Turnstile 配置失败: {str(raw.get('body'))[:160]}")
        return None
    status, body, resp_headers, data = _parse_raw(raw)
    verdict = detect.analyze(status, resp_headers, body)
    if verdict.blocked or not isinstance(data, dict):
        return None
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    key = payload.get("turnstile_site_key") or payload.get("TurnstileSiteKey")
    key = str(key or "").strip()
    if not key:
        log.debug("站点状态未返回 Turnstile site key")
        return None
    return key


def _checkin_in_page(driver: BrowserDriver, account: Account,
                     turnstile_token: Optional[str] = None) -> Optional[api.ApiResult]:
    base = account.base_url
    headers = {"Accept": "application/json, text/plain, */*"}
    user_id = account.user_id

    if not user_id:
        raw = driver.fetch_in_page(base + SELF_PATH, "GET", headers)
        if not raw.get("ok"):
            log.debug(f"页内 /api/user/self 失败: {str(raw.get('body'))[:160]}")
            return api.ApiResult(api.NETWORK_ERROR, message=str(raw.get("body"))[:160])
        status, body, resp_headers, data = _parse_raw(raw)
        verdict = detect.analyze(status, resp_headers, body)
        if verdict.blocked:
            return api.ApiResult(api.CF_BLOCKED, message=verdict.describe(), status=status,
                                 verdict=verdict, signals=list(verdict.signals))
        result = api.classify_self(status, data, body[:160])
        if not result.ok or not result.user_id:
            return result
        user_id = result.user_id
        account.user_id = user_id
        log.debug(f"页内获取 user_id={user_id}")

    headers["New-Api-User"] = str(user_id)
    last: Optional[api.ApiResult] = None
    for path in account.checkin_candidates:
        request_path = _with_turnstile(path, turnstile_token)
        raw = driver.fetch_in_page(base + request_path, "POST", headers)
        if not raw.get("ok"):
            last = api.ApiResult(api.NETWORK_ERROR, message=str(raw.get("body"))[:160], path=path)
            continue
        status, body, resp_headers, data = _parse_raw(raw)
        verdict = detect.analyze(status, resp_headers, body)
        if verdict.blocked:
            return api.ApiResult(api.CF_BLOCKED, message=verdict.describe(), status=status,
                                 path=path, verdict=verdict, signals=list(verdict.signals))
        if status in (404, 405, 501):
            last = api.ApiResult(api.FAILED, message=f"HTTP {status}（路径不存在）",
                                 status=status, path=path)
            continue
        result = api.classify_checkin(status, data, body[:160], path)
        result.user_id = user_id
        if result.kind == api.UNKNOWN and status >= 400:
            last = result
            continue
        return result
    return last


# --------------------------------------------------------------------------- #
# 会话收割与排障产物
# --------------------------------------------------------------------------- #


def _harvest(driver: BrowserDriver, account: Account, exit_ip: Optional[str]) -> CFSession:
    """cf_clearance 绑定 IP + UA，所以这三样必须作为一组一起存。"""
    return CFSession(
        cookies=driver.cookie_dict(),
        user_agent=driver.user_agent(),
        accept_language=driver.accept_language(),
        exit_ip=exit_ip,
        proxy=account.proxy,
        expires_at=cookie_expiry(driver.cookies()),
        saved_at=now(),
    )


def _dump(driver: BrowserDriver, cfg: Config, account: Account, tag: str) -> Optional[str]:
    """无头 VPS 上唯一的现场证据：截图 + HTML 快照。"""
    if not cfg.browser.keep_artifacts_on_fail:
        return None
    target = SHOTS_DIR / f"{account.slug}_{datetime.now():%Y%m%d-%H%M%S}"
    path = driver.dump_artifacts(target, tag)
    if path is not None:
        log.info(f"现场证据已保存: {path}")
        return str(path)
    return None


def _with_artifact(detail: str, artifact: Optional[str]) -> str:
    return f"{detail}；现场证据: {artifact}" if artifact else detail
