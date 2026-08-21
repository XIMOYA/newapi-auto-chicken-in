"""邮件通知测试：主题、HTML 模板、状态徽章、发送降级。全程不真发信。"""

import pytest

from newapi_checkin import notify as nt
from newapi_checkin.config import build_config


def make_row(name="A", status="success", strategy="S1", detail="", quota=None):
    return nt.log.SummaryRow(name, status, strategy, detail, quota)


# --------------------------------------------------------------------------- #
# 主题
# --------------------------------------------------------------------------- #


class TestSubject:
    def test_success_no_prefix(self):
        assert nt.build_subject("NewAPI 签到日报", "2026-08-12", failed_count=0) == \
            "NewAPI 签到日报 2026-08-12"

    def test_failure_prefix(self):
        assert nt.build_subject("NewAPI 签到日报", "2026-08-12", failed_count=2) == \
            "[失败] NewAPI 签到日报 2026-08-12"

    def test_dry_run_suffix(self):
        assert nt.build_subject("NewAPI 签到日报", "2026-08-12", failed_count=0,
                                dry_run=True) == "NewAPI 签到日报 2026-08-12（连通性检查）"

    def test_failure_and_dry_run(self):
        assert nt.build_subject("P", "2026-08-12", failed_count=1, dry_run=True) == \
            "[失败] P 2026-08-12（连通性检查）"


# --------------------------------------------------------------------------- #
# HTML 模板
# --------------------------------------------------------------------------- #


class TestReportHtml:
    def test_contains_account_and_result(self):
        rows = [make_row("github5@likiq.top", "already_done", "S4 浏览器内直发")]
        html = nt.build_report_html(rows, date_str="2026-08-12")
        assert "github5@likiq.top" in html
        assert "今日已签" in html
        assert "S4 浏览器内直发" in html

    def test_html_escapes_dangerous_chars(self):
        rows = [make_row('<img src=x onerror=alert(1)>', "unknown",
                         "S1", "<script>alert('x')</script>")]
        html = nt.build_report_html(rows, date_str="2026-08-12")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "&lt;img" in html

    def test_banner_headline_success(self):
        rows = [make_row("A", "success"), make_row("B", "success")]
        html = nt.build_report_html(rows, date_str="2026-08-12")
        assert "今日全部签到成功" in html
        assert "2/2" in html
        assert "linear-gradient(135deg,#059669" in html

    def test_banner_headline_failure_and_alert(self):
        rows = [make_row("A", "success"), make_row("B", "auth_failed")]
        html = nt.build_report_html(rows, date_str="2026-08-12")
        assert "有 1 个账号签到失败" in html
        assert "需要关注" in html
        assert "linear-gradient(135deg,#dc2626" in html

    def test_stats_counts(self):
        rows = [
            make_row("A", "success"),
            make_row("B", "already_done"),
            make_row("C", "skipped"),
            make_row("D", "network_error"),
        ]
        html = nt.build_report_html(rows, date_str="2026-08-12")
        assert "4</div>" in html  # 总账号
        assert "2</div>" in html  # 成功
        assert "1</div>" in html  # 失败

    def test_skipped_accounts_are_not_reported_as_all_success(self):
        """有账号被跳过时不能报「全部成功」，分母也不能把跳过的算进去。

        原来 failed_count 排除了 skipped，5 个账号里 3 成功 2 跳过会打出
        「今日全部签到成功！ 3/5」，横幅还是绿色「全部成功」，跳过的两个
        账号在 KPI 里根本看不见。
        """
        rows = [
            make_row("A", "success"),
            make_row("B", "already_done"),
            make_row("C", "success"),
            make_row("D", "skipped"),
            make_row("E", "skipped"),
        ]
        html = nt.build_report_html(rows, date_str="2026-08-12")
        assert "今日全部签到成功" not in html
        assert "3/3" in html                    # 分母去掉了 2 个跳过的
        assert "另有 2 个账号被跳过" in html
        assert "2 个跳过" in html               # 横幅胶囊
        assert "跳过</div>" in html             # KPI 第四张卡
        assert "linear-gradient(135deg,#d97706" in html   # 橙色而非绿色

    def test_all_success_without_skips_keeps_green_banner(self):
        rows = [make_row("A", "success"), make_row("B", "already_done")]
        html = nt.build_report_html(rows, date_str="2026-08-12")
        assert "今日全部签到成功" in html
        assert "linear-gradient(135deg,#059669" in html
        assert "跳过</div>" not in html         # 没有跳过就不加第四张卡

    def test_failure_takes_precedence_over_skips(self):
        rows = [make_row("A", "network_error"), make_row("B", "skipped")]
        html = nt.build_report_html(rows, date_str="2026-08-12")
        assert "有 1 个账号签到失败" in html
        assert "linear-gradient(135deg,#dc2626" in html
        assert "跳过</div>" in html             # 失败态也要能看到跳过数

    def test_quota_shown(self):
        rows = [make_row("A", "success", quota=500000)]
        html = nt.build_report_html(rows, date_str="2026-08-12")
        assert "500000" in html

    def test_badge_status_style_mapping(self):
        # success 与 already_done 颜色一致（绿），文字不同；徽章含语义色圆点
        assert "#059669" in nt._badge("success")
        assert "#059669" in nt._badge("already_done")
        assert "#dc2626" in nt._badge("network_error")
        assert "#b45309" in nt._badge("unknown")
        assert "签到成功" in nt._badge("success")
        assert "今日已签" in nt._badge("already_done")
        assert 'border-radius:50%' in nt._badge("success")  # 色点存在

    def test_full_document(self):
        rows = [make_row("A", "success")]
        html = nt.build_report_html(rows, date_str="2026-08-12", beijing_time="2026-08-12 08:05")
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #


class TestNotifyConfig:
    def test_defaults_disabled(self):
        cfg = build_config({"accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}]})
        assert cfg.notify.email.enabled is False
        assert cfg.notify.email.smtp_host == "smtp.aliyun.com"
        assert cfg.notify.email.smtp_port == 465
        assert cfg.notify.email.use_ssl is True

    def test_from_config(self):
        cfg = build_config(
            {
                "notify": {
                    "email": {
                        "enabled": True,
                        "smtp_host": "smtp.aliyun.com",
                        "smtp_port": 465,
                        "username": "huanziyuan@aliyun.com",
                        "password": "secret",
                        "from_addr": "huanziyuan@aliyun.com",
                        "to_addrs": ["a@qq.com", "b@qq.com"],
                    }
                },
                "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
            }
        )
        email = cfg.notify.email
        assert email.ready is True
        assert email.to_addrs == ["a@qq.com", "b@qq.com"]

    def test_ready_requires_all_fields(self):
        cfg = nt.EmailNotifyConfig(enabled=True, smtp_host="h", username="u",
                                   password="p", from_addr="f", to_addrs=[])
        assert cfg.ready is False


# --------------------------------------------------------------------------- #
# 发送（全 mock，不真连网络）
# --------------------------------------------------------------------------- #


class TestNotifierSend:
    def _notifier(self):
        return nt.EmailNotifier(
            nt.EmailNotifyConfig(
                enabled=True, smtp_host="smtp.aliyun.com", smtp_port=465,
                use_ssl=True, username="u", password="p",
                from_addr="f@aliyun.com", to_addrs=["t@qq.com"],
            )
        )

    def test_not_ready_returns_false(self):
        cfg = nt.EmailNotifyConfig(enabled=False)
        assert nt.EmailNotifier(cfg).send("s", "html") is False

    def test_send_success(self, monkeypatch):
        sent = {}

        class FakeServer:
            def __init__(self, *a, **kw):
                pass

            def login(self, user, pwd):
                sent["login"] = (user, pwd)

            def sendmail(self, from_addr, to_addrs, message):
                sent["from"] = from_addr
                sent["to"] = to_addrs
                sent["size"] = len(message)

            def quit(self):
                sent["quit"] = True

        monkeypatch.setattr(nt.smtplib, "SMTP_SSL", FakeServer)
        assert self._notifier().send("主题", "<html>内容</html>") is True
        assert sent["login"] == ("u", "p")
        assert sent["from"] == "f@aliyun.com"
        assert sent["to"] == ["t@qq.com"]
        assert sent["size"] > 0
        assert sent["quit"] is True

    def test_send_connection_failure_degrades(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("connection refused")

        monkeypatch.setattr(nt.smtplib, "SMTP_SSL", boom)
        assert self._notifier().send("主题", "html") is False

    def test_send_login_failure_degrades(self, monkeypatch):
        class FakeServer:
            def __init__(self, *a, **kw):
                pass

            def login(self, user, pwd):
                raise smtplib.SMTPAuthenticationError(535, b"auth failed")

            def sendmail(self, *a, **kw):
                raise AssertionError("不应走到 sendmail")

            def quit(self):
                pass

        monkeypatch.setattr(nt.smtplib, "SMTP_SSL", FakeServer)
        assert self._notifier().send("主题", "html") is False

    def test_starttls_branch(self, monkeypatch):
        used_starttls = []

        class FakePlainServer:
            def __init__(self, *a, **kw):
                pass

            def ehlo(self):
                pass

            def starttls(self):
                used_starttls.append(True)

            def login(self, user, pwd):
                pass

            def sendmail(self, from_addr, to_addrs, message):
                pass

            def quit(self):
                pass

        monkeypatch.setattr(nt.smtplib, "SMTP", FakePlainServer)
        cfg = nt.EmailNotifyConfig(enabled=True, smtp_host="h", smtp_port=587, use_ssl=False,
                                   username="u", password="p", from_addr="f", to_addrs=["t"])
        assert nt.EmailNotifier(cfg).send("s", "html") is True
        assert used_starttls == [True]


# --------------------------------------------------------------------------- #
# send_report：Runner 和 Actions 汇总 job 共用的那一份组装+发送入口
# --------------------------------------------------------------------------- #


class TestSendReport:
    def _cfg(self, enabled=True):
        return nt.EmailNotifyConfig(
            enabled=enabled, smtp_host="smtp.aliyun.com", smtp_port=465, use_ssl=True,
            username="u", password="p", from_addr="f@aliyun.com", to_addrs=["t@qq.com"],
        )

    @pytest.fixture()
    def captured(self, monkeypatch):
        """截下最终要发的主题与 HTML，不碰 smtplib。"""
        box = {}

        def fake_send(self, subject, html):
            box["subject"], box["html"] = subject, html
            return True

        monkeypatch.setattr(nt.EmailNotifier, "send", fake_send)
        return box

    def test_disabled_is_not_a_failure(self, captured):
        assert nt.send_report(self._cfg(enabled=False), [make_row()]) == (False, "")
        assert captured == {}

    def test_returns_subject_for_logging(self, captured):
        sent, subject = nt.send_report(self._cfg(), [make_row()])
        assert sent is True
        assert subject == captured["subject"] and subject

    def test_failed_rows_mark_subject(self, captured):
        _, subject = nt.send_report(self._cfg(), [make_row(status="failed")])
        assert subject.startswith("[失败] ")

    def test_skipped_is_not_counted_as_failure(self, captured):
        """跳过不算失败，主题不该挂 [失败]（与 Summary.failed 口径一致）。"""
        _, subject = nt.send_report(self._cfg(), [make_row(status="skipped")])
        assert not subject.startswith("[失败] ")

    def test_dry_run_marks_subject(self, captured):
        _, subject = nt.send_report(self._cfg(), [make_row()], dry_run=True)
        assert "连通性检查" in subject

    def test_extra_note_lands_in_html(self, captured):
        nt.send_report(self._cfg(), [make_row()], extra_note="缺第 2 片")
        assert "缺第 2 片" in captured["html"]

    def test_warn_note_uses_amber_block(self, captured):
        """缺片提示要走橙色警示块，灰色小字会被划过去。"""
        nt.send_report(self._cfg(), [make_row()], extra_note="缺第 2 片", note_level="warn")
        assert "#fffbeb" in captured["html"] and "border-left:4px solid #b45309" in captured["html"]

    def test_info_note_stays_quiet(self, captured):
        nt.send_report(self._cfg(), [make_row()], extra_note="共 3 片已合并")
        assert "#b45309" not in captured["html"]
