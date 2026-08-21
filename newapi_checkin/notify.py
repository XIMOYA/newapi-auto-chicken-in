"""邮件通知：签到结果 HTML 邮件（每日无论成败都发）。

设计要点：
  - 纯标准库 smtplib + email.mime，无第三方依赖，桌面 / GitHub Actions 通用
  - 支持 SSL(465) 与 STARTTLS(587)；默认阿里邮箱 smtp.aliyun.com:465
  - 发送失败只 WARN 不中断（与 AI / 代理池同一「可降级」哲学）
  - 模板全部内联样式 + table 布局，QQ / 163 / Gmail / Outlook 手机端均正常显示
  - 北京时间展示（不依赖系统时区，手动 +8h 转换，Windows 也稳）
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from . import logger as log

OK_STATUSES = {"success", "already_done"}

# 状态 -> (徽章文字, 前景色, 背景色)
_STATUS_STYLE = {
    "success": ("签到成功", "#059669", "#ecfdf5", "#059669"),
    "already_done": ("今日已签", "#059669", "#ecfdf5", "#059669"),
    "skipped": ("已跳过", "#64748b", "#f1f5f9", "#64748b"),
    "failed": ("签到失败", "#dc2626", "#fef2f2", "#dc2626"),
    "auth_failed": ("认证失败", "#dc2626", "#fef2f2", "#dc2626"),
    "login_required": ("浏览器未登录", "#dc2626", "#fef2f2", "#dc2626"),
    "cf_blocked": ("被盾拦截", "#dc2626", "#fef2f2", "#dc2626"),
    "waf_block": ("WAF 封禁", "#dc2626", "#fef2f2", "#dc2626"),
    "turnstile_required": ("需要 Turnstile", "#b45309", "#fffbeb", "#b45309"),
    "network_error": ("网络异常", "#dc2626", "#fef2f2", "#dc2626"),
    "config_error": ("配置错误", "#dc2626", "#fef2f2", "#dc2626"),
    "unknown": ("结果未知", "#b45309", "#fffbeb", "#b45309"),
}

_DATE_FORMAT = "%Y-%m-%d"


@dataclass
class EmailNotifyConfig:
    enabled: bool = False
    smtp_host: str = "smtp.aliyun.com"
    smtp_port: int = 465
    use_ssl: bool = True
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: list = field(default_factory=list)
    subject_prefix: str = "NewAPI 签到日报"
    timeout: int = 20

    @classmethod
    def from_raw(cls, raw: Optional[dict]) -> "EmailNotifyConfig":
        raw = raw if isinstance(raw, dict) else {}
        to_addrs = raw.get("to_addrs")
        if isinstance(to_addrs, str):
            to_addrs = [item.strip() for item in to_addrs.split(",") if item.strip()]
        elif isinstance(to_addrs, (list, tuple)):
            to_addrs = [str(item).strip() for item in to_addrs if str(item).strip()]
        else:
            to_addrs = []
        return cls(
            enabled=bool(raw.get("enabled", False)),
            smtp_host=str(raw.get("smtp_host") or "smtp.aliyun.com").strip(),
            smtp_port=_as_int(raw.get("smtp_port"), 465),
            use_ssl=raw.get("use_ssl", True) if isinstance(raw.get("use_ssl"), bool)
            else str(raw.get("use_ssl", "true")).strip().lower() not in {"0", "false", "no", "off"},
            username=str(raw.get("username") or "").strip(),
            password=str(raw.get("password") or ""),
            from_addr=str(raw.get("from_addr") or "").strip(),
            to_addrs=to_addrs,
            subject_prefix=str(raw.get("subject_prefix") or "NewAPI 签到日报").strip(),
            timeout=max(5, min(120, _as_int(raw.get("timeout"), 20))),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "use_ssl": self.use_ssl,
            "username": self.username,
            "password": self.password,
            "from_addr": self.from_addr,
            "to_addrs": list(self.to_addrs),
            "subject_prefix": self.subject_prefix,
            "timeout": self.timeout,
        }

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled and self.smtp_host and self.username
            and self.password and self.from_addr and self.to_addrs
        )


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def beijing_now() -> datetime:
    """返回北京时间（UTC+8），不依赖系统时区数据库。"""
    return datetime.utcnow() + timedelta(hours=8)


def _esc(text) -> str:
    """HTML 转义，防止账号名 / 报错信息里的 <>& 破坏模板。"""
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_subject(prefix: str, date_str: str, failed_count: int,
                  dry_run: bool = False) -> str:
    base = f"{prefix} {date_str}"
    if dry_run:
        base += "（连通性检查）"
    if failed_count > 0:
        base = f"[失败] {base}"
    return base


# --------------------------------------------------------------------------- #
# HTML 模板
# --------------------------------------------------------------------------- #
# 设计语言：Executive Dashboard（高管仪表盘）
#   - 品牌主色 深蓝 #1E3A8A / 副色 #3B82F6，语义色 成功绿 / 失败红 / 警告橙
#   - KPI 大数字 + 小标签，数字用等宽 tabular-nums，避免宽度跳动
#   - 全内联样式 + table 布局（QQ/163/Gmail/Outlook 均安全）
#   - 状态徽章 = 浅底深字胶囊 + 语义色圆点（不只靠颜色区分，见 WCAG color-not-only）

def _badge(status: str) -> str:
    label, fg, bg, dot = _STATUS_STYLE.get(status, _STATUS_STYLE["unknown"])
    return (
        f'<span style="display:inline-block;padding:4px 12px;border-radius:999px;'
        f'font-size:12px;font-weight:600;color:{fg};background:{bg};'
        f'white-space:nowrap;line-height:1.4;">'
        f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;'
        f'background:{fg};margin-right:6px;vertical-align:1px;"></span>'
        f'{label}</span>'
    )


def _section_title(text: str, accent: str = "#1E3A8A") -> str:
    return (
        f'<div style="margin:26px 0 12px;font-size:14px;font-weight:700;color:#1F2937;'
        f'letter-spacing:.3px;">'
        f'<span style="display:inline-block;width:4px;height:14px;border-radius:2px;'
        f'background:{accent};vertical-align:-2px;margin-right:8px;"></span>'
        f'{_esc(text)}</div>'
    )


def _quota_site_block(site, group: Optional[str]) -> str:
    """渲染一个站点的额度小节：标题行 + 每个模型的单价与可发次数。"""
    head_right = f"合计 ${site.total_usd:.2f}"
    if site.accounts:
        head_right += f" · {site.known}/{site.accounts} 个账号已知余额"
    warn = ""
    if not site.complete:
        # 说清楚这不是全部：有账号没查到余额，总额和次数都是保守下限
        warn = (
            "<div style='padding:8px 16px;font-size:11px;color:#92400e;"
            "background:#fffbeb;'>有账号没查到余额，下面的合计与次数是<b>下限</b>，"
            "实际可用量更高</div>"
        )

    rows = site.rows(group)
    if not rows:
        body = ("<div style='padding:10px 16px;font-size:12px;color:#94a3b8;'>"
                "定价表暂不可用，只汇总了余额</div>")
    else:
        cells = []
        for idx, (name, unit, calls) in enumerate(rows):
            stripe = "background:#ffffff;" if idx % 2 == 0 else "background:#fcfdfe;"
            cells.append(
                "<tr>"
                f"<td style='padding:8px 16px;font-size:12px;color:#334155;"
                f"border-top:1px solid #f1f5f9;{stripe}'>{_esc(name)}</td>"
                f"<td style='padding:8px 16px;font-size:12px;color:#64748b;text-align:right;"
                f"font-variant-numeric:tabular-nums;border-top:1px solid #f1f5f9;{stripe}'>"
                f"{_esc(unit)}</td>"
                f"<td style='padding:8px 16px;font-size:13px;color:#1f2937;font-weight:600;"
                f"text-align:right;font-variant-numeric:tabular-nums;"
                f"border-top:1px solid #f1f5f9;{stripe}'>{_esc(calls)}</td>"
                "</tr>"
            )
        body = ("<table role='presentation' style='width:100%;border-collapse:collapse;'>"
                + "".join(cells) + "</table>")

    return (
        "<div style='margin:12px 0;border:1px solid #e2e8f0;border-radius:8px;"
        "overflow:hidden;'>"
        "<div style='padding:10px 16px;background:#f1f5f9;font-size:12px;"
        "font-weight:700;color:#1e3a8a;'>"
        f"{_esc(site.label)}"
        "<span style='float:right;font-weight:600;color:#334155;"
        f"font-variant-numeric:tabular-nums;'>{_esc(head_right)}</span></div>"
        + warn + body + "</div>"
    )


def build_quota_overview(site_quotas: list, group: Optional[str] = None) -> str:
    """邮件底部的额度总览：按站点分组，各站小计 + 每个模型能发多少次，末尾全局合计。

    次数只对「按次计费」的模型成立（New API 的 quota_type=1，model_price 就是每次
    单价）。按 token 计费的模型没有「一次」的价格，那种只标计费方式，不编数字。
    """
    if not site_quotas:
        return ""
    blocks = [_quota_site_block(s, group) for s in site_quotas]
    grand = sum(s.total_usd for s in site_quotas)
    all_complete = all(s.complete for s in site_quotas)
    total_line = (
        "<div style='margin:14px 0 4px;padding:12px 16px;background:#1e3a8a;"
        "border-radius:8px;color:#ffffff;font-size:13px;font-weight:700;'>"
        "全部站点合计"
        "<span style='float:right;font-variant-numeric:tabular-nums;'>"
        f"${grand:.2f}{'' if all_complete else ' 起'}</span></div>"
    )
    note = (
        "<div style='margin:6px 0 0;font-size:11px;color:#94a3b8;line-height:1.6;'>"
        "单价与可发次数取自各站点 /api/pricing 的实时数据（每轮重新拉取），"
        "按默认分组价格计算；只统计模型名含 opus 的模型。</div>"
    )
    return _section_title("额度总览（按站点）") + "".join(blocks) + total_line + note


def build_report_html(rows: list, *, date_str: str, run_context: str = "GitHub Actions",
                      beijing_time: str = "", dry_run: bool = False,
                      extra_note: str = "", note_level: str = "info",
                      quota_overview: str = "") -> str:
    """把汇总行渲染成一封好看的 HTML 邮件（Executive Dashboard 风格）。"""
    total = len(rows)
    ok_count = sum(1 for r in rows if r.status in OK_STATUSES)
    failed = [r for r in rows if r.status not in OK_STATUSES and r.status != "skipped"]
    failed_count = len(failed)
    skipped_count = sum(1 for r in rows if r.status == "skipped")

    if failed_count == 0 and skipped_count == 0:
        banner_bg = "linear-gradient(135deg,#059669,#10b981)"
        banner_fallback = "#059669"
        banner_label = "全部成功"
        banner_label_color = "#065f46"
        banner_label_bg = "#d1fae5"
        headline = f"今日全部签到成功！ {ok_count}/{total}"
    elif failed_count == 0:
        # 没有失败但有跳过（缺 cookie、拿不到代理等）：不能报「全部成功」，
        # 分母也要去掉跳过的，否则会出现「全部签到成功！ 3/5」这种自相矛盾的话
        banner_bg = "linear-gradient(135deg,#d97706,#f59e0b)"
        banner_fallback = "#d97706"
        banner_label = f"{skipped_count} 个跳过"
        banner_label_color = "#92400e"
        banner_label_bg = "#fef3c7"
        headline = f"已签到 {ok_count}/{total - skipped_count}，另有 {skipped_count} 个账号被跳过"
    else:
        banner_bg = "linear-gradient(135deg,#dc2626,#ef4444)"
        banner_fallback = "#dc2626"
        banner_label = f"{failed_count} 个失败"
        banner_label_color = "#991b1b"
        banner_label_bg = "#fee2e2"
        headline = f"有 {failed_count} 个账号签到失败"

    # ---- 头部横幅：品牌小字 + 大标题 + 状态胶囊（不依赖 emoji）----
    banner = (
        f'<div style="background:{banner_bg};background-image:{banner_bg};'
        f'padding:32px 36px 30px;">'
        f'<div style="font-size:11px;font-weight:600;letter-spacing:2.5px;'
        f'color:rgba(255,255,255,.75);">NEWAPI CHECKIN · 每日签到</div>'
        f'<div style="color:#ffffff;font-size:24px;font-weight:800;margin-top:12px;'
        f'line-height:1.3;">{_esc(headline)}</div>'
        f'<div style="margin-top:14px;">'
        f'<span style="display:inline-block;padding:5px 14px;border-radius:999px;'
        f'background:{banner_label_bg};color:{banner_label_color};'
        f'font-size:12px;font-weight:700;">{banner_label}</span>'
        f'<span style="display:inline-block;margin-left:10px;color:rgba(255,255,255,.9);'
        f'font-size:13px;">{_esc(date_str)} · {_esc(run_context)}</span>'
        f'</div></div>'
    )

    # ---- KPI 统计卡：总账号 / 成功 / 失败（有跳过时补第四张，否则会漏掉这批账号）----
    kpi_cells = 4 if skipped_count else 3
    kpi_width = f"{100 / kpi_cells:.1f}%"

    def _kpi(value, label, color, dot_color=None):
        dot = dot_color or color
        return (
            f"<td style='width:{kpi_width};text-align:center;padding:18px 8px 16px;'>"
            f"<div style='font-size:27px;font-weight:800;color:{color};"
            f"font-variant-numeric:tabular-nums;line-height:1.2;'>{value}</div>"
            f"<div style='font-size:11px;color:#94a3b8;margin-top:6px;"
            f"letter-spacing:1px;'>"
            f"<span style='display:inline-block;width:5px;height:5px;border-radius:50%;"
            f"background:{dot};margin-right:5px;vertical-align:1px;'></span>{label}</div></td>"
        )
    stats = (
        "<table role='presentation' style='width:100%;border-collapse:collapse;"
        "background:#ffffff;'>"
        "<tr>"
        + _kpi(total, "总账号", "#1E3A8A")
        + _kpi(ok_count, "成功", "#059669")
        + _kpi(failed_count, "失败", "#dc2626" if failed_count else "#cbd5e1",
               dot_color="#dc2626" if failed_count else "#cbd5e1")
        + (_kpi(skipped_count, "跳过", "#d97706") if skipped_count else "")
        + "</tr></table>"
    )

    # ---- 明细表 ----
    thead = (
        "<tr>"
        "<th style='text-align:left;padding:10px 16px;font-size:11px;color:#64748b;"
        "background:#f8fafc;border-bottom:1px solid #e2e8f0;letter-spacing:.5px;'>账号</th>"
        "<th style='text-align:left;padding:10px 16px;font-size:11px;color:#64748b;"
        "background:#f8fafc;border-bottom:1px solid #e2e8f0;letter-spacing:.5px;'>结果</th>"
        "<th style='text-align:left;padding:10px 16px;font-size:11px;color:#64748b;"
        "background:#f8fafc;border-bottom:1px solid #e2e8f0;letter-spacing:.5px;'>策略</th>"
        "<th style='text-align:right;padding:10px 16px;font-size:11px;color:#64748b;"
        "background:#f8fafc;border-bottom:1px solid #e2e8f0;letter-spacing:.5px;'>剩余额度</th></tr>"
    )
    body_rows = []
    for idx, r in enumerate(rows):
        # 余额为主、本次奖励跟括号，和控制台表格共用同一份换算与文案规则
        quota = log.format_balance(getattr(r, "balance", None),
                                   getattr(r, "quota_per_unit", None), r.quota)
        stripe = "background:#ffffff;" if idx % 2 == 0 else "background:#fcfdfe;"
        detail = (
            f"<div style='font-size:11px;color:#94a3b8;margin-top:2px;'>{_esc(r.detail)}</div>"
            if r.detail else ""
        )
        body_rows.append(
            "<tr>"
            f"<td style='padding:11px 16px;font-size:13px;color:#1f2937;font-weight:600;"
            f"border-bottom:1px solid #f1f5f9;{stripe}'>{_esc(r.name)}{detail}</td>"
            f"<td style='padding:11px 16px;border-bottom:1px solid #f1f5f9;{stripe}'>{_badge(r.status)}</td>"
            f"<td style='padding:11px 16px;font-size:12px;color:#64748b;"
            f"border-bottom:1px solid #f1f5f9;{stripe}'>{_esc(r.strategy)}</td>"
            f"<td style='padding:11px 16px;font-size:13px;color:#334155;text-align:right;"
            f"font-variant-numeric:tabular-nums;border-bottom:1px solid #f1f5f9;{stripe}'>"
            f"{_esc(quota)}</td>"
            "</tr>"
        )
    table = (
        "<table role='presentation' style='width:100%;border-collapse:collapse;"
        "border:1px solid #e2e8f0;border-radius:8px;'>" + thead + "".join(body_rows) + "</table>"
    )

    # ---- 失败警示：左侧红条卡片 ----
    alert = ""
    if failed_count:
        names = "、".join(_esc(r.name) for r in failed[:10])
        more = f" 等 {failed_count} 个" if failed_count > 10 else ""
        alert = (
            "<div style='margin:18px 0;padding:13px 16px;background:#fef2f2;"
            "border-left:4px solid #dc2626;border-radius:6px;font-size:13px;"
            "color:#991b1b;line-height:1.6;'>"
            f"<b>需要关注</b>：{names}{more} 未能成功签到，请查看运行日志排查。</div>"
        )

    # extra_note 分两档：info 是灰色小字（"由 3 个分片合并"这类背景说明），
    # warn 是橙色警示块（缺片这类「上表不是全部账号」的情况）。缺片用灰字太容易被
    # 划过去，而那恰恰是最需要被看见的一行。
    if not extra_note:
        note_html = ""
    elif note_level == "warn":
        note_html = (
            "<div style='margin:16px 0;padding:12px 16px;background:#fffbeb;"
            "border-left:4px solid #b45309;border-radius:6px;font-size:13px;"
            f"color:#92400e;line-height:1.6;'>{_esc(extra_note)}</div>"
        )
    else:
        note_html = (
            "<div style='margin:14px 0;padding:10px 14px;background:#f8fafc;"
            f"border-radius:6px;font-size:12px;color:#64748b;'>{_esc(extra_note)}</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;">
<div style="max-width:600px;margin:24px auto;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 4px 24px rgba(30,58,138,.08);">

  {banner}

  <div style="padding:8px 32px 28px;">
    {_section_title("签到总览")}
    {stats}
    {alert}
    {note_html}
    {_section_title("签到明细", accent="#3b82f6")}
    {table}
    {quota_overview}
  </div>

  <div style="padding:18px 32px 26px;color:#94a3b8;font-size:11px;text-align:center;border-top:1px solid #f1f5f9;letter-spacing:.5px;">
    NewAPI Checkin · 自动签到通知
    <div style="margin-top:6px;font-size:11px;color:#cbd5e1;">
      {_esc('运行于 ' + run_context)} · 北京时间 {beijing_time or '—'} · 自动发送，请勿回复
    </div>
  </div>

</div>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# 发信
# --------------------------------------------------------------------------- #

class EmailNotifier:
    """SMTP 发信封装。发送失败返回 False 并 WARN，绝不抛异常。"""

    def __init__(self, cfg: EmailNotifyConfig):
        self.cfg = cfg

    def send(self, subject: str, html: str) -> bool:
        cfg = self.cfg
        if not cfg.ready:
            log.warn("邮件通知未配置完整（enabled/host/username/password/from/to 缺一不可）")
            return False

        msg = MIMEMultipart("alternative")
        # 主题含中文，必须用 Header 编码，否则 smtplib 按 ascii 编码报错
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = cfg.from_addr
        msg["To"] = ", ".join(cfg.to_addrs)
        msg.attach(MIMEText(html, "html", "utf-8"))

        try:
            # local_hostname 必须显式给 ASCII：Windows 中文主机名会让 smtplib
            # 在 EHLO 命令里编码失败（UnicodeEncodeError ascii position 5-7）
            if cfg.use_ssl:
                server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout,
                                          local_hostname="localhost")
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout,
                                      local_hostname="localhost")
                server.ehlo()
                server.starttls()
                server.ehlo()
        except Exception as exc:  # noqa: BLE001 - 网络/端口异常统一降级
            log.warn(f"邮件服务器连接失败: {type(exc).__name__}: {exc}")
            return False

        try:
            server.login(cfg.username, cfg.password)
            # 用 as_bytes() 而非 as_string()：smtplib 对 str 消息按 ascii 重新编码，
            # 中文 HTML 消息会被消息体的非 ascii 字符炸掉；bytes 直接透传 UTF-8。
            server.sendmail(cfg.from_addr, cfg.to_addrs, msg.as_bytes())
            return True
        except Exception as exc:  # noqa: BLE001 - 认证/发送异常统一降级
            log.warn(f"邮件发送失败: {type(exc).__name__}: {exc}")
            return False
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass


def send_report(email_cfg: EmailNotifyConfig, rows: list, *, dry_run: bool = False,
                run_context: str = "GitHub Actions", extra_note: str = "",
                note_level: str = "info", quota_overview: str = "") -> tuple:
    """把汇总行拼成主题 + HTML 并发出，返回 (是否发成功, 邮件主题)。

    两个调用方共用这一份：一是 Runner 跑完一轮自己发，二是 Actions 分片跑完由汇总
    入口合并各片结果统一发。主题规则、KPI 口径、模板都只有一份实现，不会出现两条
    路发出来的邮件长得不一样。

    主题一并返回是为了让调用方把它写进日志 —— 排查「邮件到底发出去没有」时，
    日志里的主题能直接和收件箱对照。未配置 enabled 时返回 (False, "")，
    那是「没开这个功能」，不是失败。
    """
    if not email_cfg.enabled:
        return False, ""
    now = beijing_now()
    failed = sum(1 for r in rows if r.status not in OK_STATUSES and r.status != "skipped")
    subject = build_subject(
        email_cfg.subject_prefix, now.strftime(_DATE_FORMAT),
        failed_count=failed, dry_run=dry_run,
    )
    html = build_report_html(
        rows,
        date_str=now.strftime(_DATE_FORMAT),
        run_context=run_context,
        beijing_time=now.strftime("%Y-%m-%d %H:%M"),
        dry_run=dry_run,
        extra_note=extra_note,
        note_level=note_level,
        quota_overview=quota_overview,
    )
    return EmailNotifier(email_cfg).send(subject, html), subject
