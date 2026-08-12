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
    "success": ("签到成功", "#0d9488", "#ccfbf1"),
    "already_done": ("今日已签", "#0d9488", "#ccfbf1"),
    "skipped": ("已跳过", "#6b7280", "#f3f4f6"),
    "failed": ("签到失败", "#dc2626", "#fee2e2"),
    "auth_failed": ("认证失败", "#dc2626", "#fee2e2"),
    "login_required": ("浏览器未登录", "#dc2626", "#fee2e2"),
    "cf_blocked": ("被盾拦截", "#dc2626", "#fee2e2"),
    "waf_block": ("WAF 封禁", "#dc2626", "#fee2e2"),
    "turnstile_required": ("需要 Turnstile", "#b45309", "#fef3c7"),
    "network_error": ("网络异常", "#dc2626", "#fee2e2"),
    "config_error": ("配置错误", "#dc2626", "#fee2e2"),
    "unknown": ("结果未知", "#b45309", "#fef3c7"),
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

def _badge(status: str) -> str:
    label, fg, bg = _STATUS_STYLE.get(status, _STATUS_STYLE["unknown"])
    return (
        f'<span style="display:inline-block;padding:3px 12px;border-radius:999px;'
        f'font-size:12px;font-weight:600;color:{fg};background:{bg};white-space:nowrap;">'
        f'{label}</span>'
    )


def build_report_html(rows: list, *, date_str: str, run_context: str = "GitHub Actions",
                      beijing_time: str = "", dry_run: bool = False,
                      extra_note: str = "") -> str:
    """把汇总行渲染成一封好看的 HTML 邮件。"""
    total = len(rows)
    ok_count = sum(1 for r in rows if r.status in OK_STATUSES)
    failed = [r for r in rows if r.status not in OK_STATUSES and r.status != "skipped"]
    failed_count = len(failed)
    skipped_count = sum(1 for r in rows if r.status == "skipped")

    if failed_count == 0:
        banner_bg = "linear-gradient(135deg,#10b981,#059669)"
        banner_icon = "✅"
        headline = f"今日全部签到成功！ {ok_count}/{total}"
    else:
        banner_bg = "linear-gradient(135deg,#ef4444,#b91c1c)"
        banner_icon = "⚠️"
        headline = f"有 {failed_count} 个账号签到失败"

    # 统计卡片
    stat_cell = (
        "<td style='width:33.3%;text-align:center;padding:16px 8px;'>"
        "<div style='font-size:28px;font-weight:800;color:#111827;'>{}</div>"
        "<div style='font-size:12px;color:#9ca3af;margin-top:4px;'>{}</div></td>"
    )
    stats = (
        "<table style='width:100%;border-collapse:collapse;margin:0 auto;"
        "background:#f9fafb;border-radius:10px;'>"
        "<tr>"
        + stat_cell.format(total, "总账号")
        + stat_cell.format(ok_count, "成功")
        + stat_cell.format(failed_count, "失败")
        + "</tr></table>"
    )

    # 明细表
    thead = (
        "<tr style='background:#f3f4f6;'>"
        "<th style='text-align:left;padding:10px 14px;font-size:12px;color:#6b7280;"
        "border-bottom:1px solid #e5e7eb;'>账号</th>"
        "<th style='text-align:left;padding:10px 14px;font-size:12px;color:#6b7280;"
        "border-bottom:1px solid #e5e7eb;'>结果</th>"
        "<th style='text-align:left;padding:10px 14px;font-size:12px;color:#6b7280;"
        "border-bottom:1px solid #e5e7eb;'>策略</th>"
        "<th style='text-align:right;padding:10px 14px;font-size:12px;color:#6b7280;"
        "border-bottom:1px solid #e5e7eb;'>额度</th></tr>"
    )
    body_rows = []
    for r in rows:
        quota = "-" if r.quota in (None, "", 0) else str(r.quota)
        detail = f"<div style='font-size:11px;color:#9ca3af;'>{_esc(r.detail)}</div>" if r.detail else ""
        body_rows.append(
            "<tr>"
            f"<td style='padding:9px 14px;font-size:13px;color:#374151;border-bottom:1px solid #f3f4f6;'>"
            f"{_esc(r.name)}{detail}</td>"
            f"<td style='padding:9px 14px;border-bottom:1px solid #f3f4f6;'>{_badge(r.status)}</td>"
            f"<td style='padding:9px 14px;font-size:12px;color:#6b7280;border-bottom:1px solid #f3f4f6;'>"
            f"{_esc(r.strategy)}</td>"
            f"<td style='padding:9px 14px;font-size:13px;color:#374151;text-align:right;"
            f"border-bottom:1px solid #f3f4f6;'>{_esc(quota)}</td>"
            "</tr>"
        )
    table = (
        "<table style='width:100%;border-collapse:collapse;'>" + thead + "".join(body_rows) + "</table>"
    )

    # 失败警示
    alert = ""
    if failed_count:
        names = "、".join(_esc(r.name) for r in failed[:10])
        more = f" 等 {failed_count} 个" if failed_count > 10 else ""
        alert = (
            "<div style='margin:18px 0;padding:14px 16px;background:#fef2f2;border:1px solid #fecaca;"
            "border-radius:8px;font-size:13px;color:#b91c1c;'>"
            f"<b>⚠️ 需要关注：</b>{names}{more} 未能成功签到，请查看运行日志排查。</div>"
        )

    note_html = f"<div style='margin:14px 0;font-size:12px;color:#9ca3af;'>{_esc(extra_note)}</div>" if extra_note else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;">
<div style="max-width:640px;margin:24px auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08);">

  <div style="background:{banner_bg};padding:28px 32px;">
    <div style="font-size:36px;line-height:1;">{banner_icon}</div>
    <div style="color:#ffffff;font-size:22px;font-weight:700;margin-top:10px;">{_esc(headline)}</div>
    <div style="color:rgba(255,255,255,.88);font-size:13px;margin-top:6px;">{date_str} · {run_context}</div>
  </div>

  <div style="padding:24px 32px 6px;">
    {stats}
    {alert}
    {note_html}
    <div style="font-size:14px;font-weight:700;color:#111827;margin:22px 0 10px;">📋 签到明细</div>
    {table}
  </div>

  <div style="padding:20px 32px 28px;color:#9ca3af;font-size:12px;text-align:center;border-top:1px solid #f3f4f6;margin-top:18px;">
    {_esc('运行于 ' + run_context)} · 北京时间 {beijing_time or '—'} · 自动发送，请勿回复
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
