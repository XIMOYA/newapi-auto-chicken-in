"""PySide6 桌面控制面板。核心 CLI 不导入此模块，便于无 GUI 环境运行。"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import __version__
from . import autostart, daemon_control
from .config import CONFIG_FILE, ConfigError, load_config, runtime_root
from .config_store import ConfigDocument, export_plain, import_json, load_document, save_document
from .daemon import DaemonClient, daemon_is_running, start_daemon_process
from .scheduler import ScheduleError, ScheduleConfig
from .secure_config import ConfigEncryptionError

try:  # 可选依赖：CLI/测试不应因没有 PySide6 而无法导入项目
    from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
    from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QSpinBox,
        QSplitter,
        QStackedWidget,
        QStyle,
        QSystemTrayIcon,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTextBrowser,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )
    _QT_ERROR: Optional[Exception] = None
except ImportError as exc:  # pragma: no cover - 取决于运行环境
    _QT_ERROR = exc


def asset_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", runtime_root()))
    candidate = base / "assets" / name
    if candidate.exists():
        return candidate
    return runtime_root() / "assets" / name


def _open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


if _QT_ERROR is not None:  # pragma: no cover

    def run_gui(argv: Optional[list[str]] = None) -> int:
        raise RuntimeError(f"PySide6 未安装，无法启动 GUI: {_QT_ERROR}")

else:

    class RequestThread(QThread):
        succeeded = Signal(object)
        failed = Signal(str)

        def __init__(self, callback: Callable[[], Any], parent: Optional[QObject] = None) -> None:
            super().__init__(parent)
            self.callback = callback

        def run(self) -> None:  # noqa: D401
            try:
                self.succeeded.emit(self.callback())
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(f"{type(exc).__name__}: {exc}")


    class StatusCard(QFrame):
        def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            self.setObjectName("StatusCard")
            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 13, 16, 13)
            self.title = QLabel(title)
            self.title.setObjectName("CardTitle")
            self.value = QLabel("—")
            self.value.setObjectName("CardValue")
            self.detail = QLabel("")
            self.detail.setObjectName("CardDetail")
            self.detail.setWordWrap(True)
            layout.addWidget(self.title)
            layout.addWidget(self.value)
            layout.addWidget(self.detail)

        def set_value(self, value: str, detail: str = "") -> None:
            self.value.setText(value)
            self.detail.setText(detail)


    class AccountDialog(QDialog):
        def __init__(self, record: Optional[dict] = None, parent: Optional[QWidget] = None) -> None:
            super().__init__(parent)
            record = record or {}
            self.setWindowTitle("编辑账号" if record else "新增账号")
            self.setMinimumWidth(560)
            form = QFormLayout(self)
            form.setContentsMargins(20, 20, 20, 12)
            self.name_edit = QLineEdit(str(record.get("name") or ""))
            self.url_edit = QLineEdit(str(record.get("url") or ""))
            self.cookie_edit = QLineEdit(str(record.get("cookie") or ""))
            self.cookie_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.user_id_edit = QLineEdit("" if record.get("user_id", record.get("userId")) in (None, "") else str(record.get("user_id", record.get("userId"))))
            self.proxy_edit = QLineEdit(str(record.get("proxy") or ""))
            self.proxy_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.checkin_path_edit = QLineEdit(str(record.get("checkin_path") or ""))
            self.browser_path_edit = QLineEdit(str(record.get("browser_path") or "/dashboard"))
            self.enabled_check = QCheckBox("启用此账号")
            self.enabled_check.setChecked(bool(record.get("enabled", True)))
            form.addRow("账号名称", self.name_edit)
            form.addRow("站点 URL", self.url_edit)
            form.addRow("Cookie", self._secret_row(self.cookie_edit))
            form.addRow("用户 ID", self.user_id_edit)
            form.addRow("代理", self._secret_row(self.proxy_edit))
            form.addRow("签到路径", self.checkin_path_edit)
            form.addRow("浏览器路径", self.browser_path_edit)
            form.addRow("状态", self.enabled_check)
            form.addRow(QLabel("Cookie 和代理仅用于运行签到，列表中不会显示明文。"))
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            form.addRow(buttons)

        @staticmethod
        def _secret_row(editor: QLineEdit) -> QWidget:
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            toggle = QToolButton()
            toggle.setText("显示")
            toggle.setCheckable(True)
            toggle.toggled.connect(lambda visible: (
                editor.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password),
                toggle.setText("隐藏" if visible else "显示"),
            ))
            layout.addWidget(editor, 1)
            layout.addWidget(toggle)
            return container

        def payload(self) -> dict:
            user_id = self.user_id_edit.text().strip()
            return {
                "name": self.name_edit.text().strip(),
                "url": self.url_edit.text().strip(),
                "cookie": self.cookie_edit.text().strip(),
                "user_id": int(user_id) if user_id else None,
                "proxy": self.proxy_edit.text().strip() or None,
                "checkin_path": self.checkin_path_edit.text().strip() or None,
                "browser_path": self.browser_path_edit.text().strip() or "/dashboard",
                "enabled": self.enabled_check.isChecked(),
            }

        def accept(self) -> None:  # noqa: N802
            try:
                payload = self.payload()
            except ValueError:
                QMessageBox.warning(self, "账号错误", "用户 ID 必须是整数。")
                return
            if not payload["name"]:
                QMessageBox.warning(self, "账号错误", "账号名称不能为空。")
                return
            if not payload["url"].startswith(("http://", "https://")):
                QMessageBox.warning(self, "账号错误", "站点 URL 必须以 http:// 或 https:// 开头。")
                return
            super().accept()


    class MainWindow(QMainWindow):
        def __init__(self, smoke_test: bool = False) -> None:
            super().__init__()
            self._smoke_test = smoke_test
            self._closing = False
            self._workers: list[RequestThread] = []
            self._refreshing = False
            self._schedule_dirty = False
            self._schedule_save_pending = False
            self._last_rows: dict[str, dict] = {}
            self._accounts = []
            self._account_records: list[dict] = []
            self._document: Optional[ConfigDocument] = None
            self._config_raw: dict = {}
            self._daemon_process: Optional[subprocess.Popen] = None
            self._daemon_enabled = daemon_control.is_enabled()
            self.setWindowTitle("NewAPI 签到中心")
            self.setMinimumSize(1000, 690)
            self.resize(1180, 760)
            self._build_ui()
            self._build_tray()
            self._load_accounts()
            self._set_autostart_state()
            if not self._smoke_test:
                if self._daemon_enabled:
                    self._ensure_daemon()
                elif daemon_is_running():
                    self._request("stop")
                self._timer = QTimer(self)
                self._timer.timeout.connect(self.refresh)
                self._timer.start(2500)
                QTimer.singleShot(200, self.refresh)

        # ------------------------------ UI ------------------------------ #
        def _build_ui(self) -> None:
            root = QWidget()
            self.setCentralWidget(root)
            outer = QVBoxLayout(root)
            outer.setContentsMargins(24, 20, 24, 20)
            outer.setSpacing(16)

            header = QHBoxLayout()
            brand = QVBoxLayout()
            title = QLabel("NewAPI 签到中心")
            title.setObjectName("PageTitle")
            subtitle = QLabel(f"安全、可见、可控的定时签到守护面板  ·  v{__version__}")
            subtitle.setObjectName("Subtitle")
            brand.addWidget(title)
            brand.addWidget(subtitle)
            header.addLayout(brand)
            header.addStretch(1)
            self.connection_label = QLabel("正在连接 daemon…")
            self.connection_label.setObjectName("ConnectionLabel")
            header.addWidget(self.connection_label, alignment=Qt.AlignmentFlag.AlignTop)
            outer.addLayout(header)

            cards = QGridLayout()
            cards.setSpacing(12)
            self.daemon_card = StatusCard("后台守护")
            self.schedule_card = StatusCard("下一次签到")
            self.last_card = StatusCard("最近结果")
            self.account_card = StatusCard("账号数量")
            cards.addWidget(self.daemon_card, 0, 0)
            cards.addWidget(self.schedule_card, 0, 1)
            cards.addWidget(self.last_card, 0, 2)
            cards.addWidget(self.account_card, 0, 3)
            outer.addLayout(cards)

            tabs = QTabWidget()
            tabs.setDocumentMode(True)
            tabs.addTab(self._build_dashboard_tab(), "总览")
            tabs.addTab(self._build_account_tab(), "账号管理")
            tabs.addTab(self._build_schedule_tab(), "定时任务")
            tabs.addTab(self._build_logs_tab(), "运行日志")
            outer.addWidget(tabs, 1)

            self.setStyleSheet(
                """
                QMainWindow, QWidget { background: #0e1628; color: #e8eefb; }
                #PageTitle { font-size: 27px; font-weight: 700; color: #f6f8ff; }
                #Subtitle, #CardTitle, #CardDetail { color: #91a1bd; }
                #ConnectionLabel { color: #6ca7ff; padding: 8px 12px; }
                #StatusCard { background: #15223a; border: 1px solid #233657; border-radius: 14px; }
                #CardValue { font-size: 21px; font-weight: 700; color: #f6f8ff; padding-top: 2px; }
                QTabWidget::pane { border: 1px solid #233657; border-radius: 12px; background: #111d32; }
                QTabBar::tab { background: transparent; color: #8fa0bf; padding: 10px 20px; margin-right: 4px; }
                QTabBar::tab:selected { color: #72aaff; border-bottom: 2px solid #4e8fff; }
                QTableWidget, QListWidget, QPlainTextEdit, QTextBrowser, QLineEdit, QComboBox, QSpinBox {
                    background: #0b1425; border: 1px solid #263b60; border-radius: 8px; color: #e8eefb;
                    selection-background-color: #28549b; padding: 6px;
                }
                QHeaderView::section { background: #182947; color: #b8c7e0; border: 0; padding: 8px; }
                QPushButton {
                    min-height: 34px;
                    padding: 7px 16px 9px 16px;
                    border: 1px solid #4b83d1;
                    border-bottom: 3px solid #17498f;
                    border-radius: 10px;
                    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                stop: 0 #347fe0, stop: 0.48 #2465c7, stop: 1 #1d56aa);
                    color: #ffffff;
                    font-weight: 650;
                }
                QPushButton:hover {
                    border-color: #79adff;
                    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                stop: 0 #4d96f0, stop: 0.48 #3479e5, stop: 1 #2865c0);
                }
                QPushButton:pressed {
                    padding-top: 9px;
                    padding-bottom: 7px;
                    border-top-color: #17498f;
                    border-right-color: #17498f;
                    border-left-color: #17498f;
                    border-bottom-width: 1px;
                    background: #1b55aa;
                }
                QPushButton:focus {
                    border-color: #9bc4ff;
                }
                QPushButton:disabled {
                    border-color: #304362;
                    border-bottom-width: 1px;
                    background: #273755;
                    color: #71809b;
                }
                QPushButton#Secondary {
                    border-color: #405b82;
                    border-bottom-color: #172a49;
                    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                stop: 0 #294568, stop: 1 #1b2c4a);
                    color: #c8d6ee;
                }
                QPushButton#Secondary:hover {
                    border-color: #6a8fca;
                    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                stop: 0 #365b86, stop: 1 #244064);
                }
                QPushButton#Secondary:pressed {
                    border-bottom-color: #172a49;
                    background: #203858;
                }
                QPushButton#Danger {
                    border-color: #c55b72;
                    border-bottom-color: #652337;
                    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                stop: 0 #aa4660, stop: 1 #8f3348);
                }
                QPushButton#Danger:hover {
                    border-color: #f08aa0;
                    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
                                                stop: 0 #c85d78, stop: 1 #a83f58);
                }
                QPushButton#Danger:pressed {
                    border-bottom-color: #652337;
                    background: #863047;
                }
                QCheckBox { spacing: 8px; color: #d4def0; }
                QGroupBox { border: 1px solid #263b60; border-radius: 10px; margin-top: 12px; padding: 12px; }
                QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #8fb5ff; }
                QLabel#Hint { color: #8193b0; }
                """
            )

        def _build_dashboard_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(12)

            toolbar = QHBoxLayout()
            self.run_button = QPushButton("立即签到")
            self.run_button.clicked.connect(lambda: self._run_command("run_now"))
            self.manual_button = QPushButton("手动验证")
            self.manual_button.setObjectName("Secondary")
            self.manual_button.setToolTip("仅此操作会打开有头浏览器进行人工验证")
            self.manual_button.clicked.connect(lambda: self._run_command("run_manual"))
            self.daemon_toggle = QCheckBox("后台守护进程")
            self.daemon_toggle.setChecked(self._daemon_enabled)
            self.daemon_toggle.setToolTip("控制后台定时签到 daemon 的启动和停止；关闭后 GUI 不会自动重启它。")
            self.daemon_toggle.toggled.connect(self._on_daemon_toggle)
            refresh_button = QPushButton("刷新")
            refresh_button.setObjectName("Secondary")
            refresh_button.clicked.connect(self.refresh)
            open_config = QPushButton("打开配置")
            open_config.setObjectName("Secondary")
            open_config.clicked.connect(lambda: self._open_config())
            open_logs = QPushButton("日志目录")
            open_logs.setObjectName("Secondary")
            open_logs.clicked.connect(lambda: _open_path(runtime_root() / "data" / "logs"))
            toolbar.addWidget(self.run_button)
            toolbar.addWidget(self.manual_button)
            toolbar.addWidget(self.daemon_toggle)
            toolbar.addStretch(1)
            toolbar.addWidget(refresh_button)
            toolbar.addWidget(open_config)
            toolbar.addWidget(open_logs)
            layout.addLayout(toolbar)

            self.account_table = QTableWidget(0, 5)
            self.account_table.setHorizontalHeaderLabels(["账号", "站点", "启用", "上次结果", "详情"])
            self.account_table.setAlternatingRowColors(True)
            self.account_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.account_table.verticalHeader().setVisible(False)
            self.account_table.horizontalHeader().setStretchLastSection(True)
            self.account_table.setColumnWidth(0, 180)
            self.account_table.setColumnWidth(1, 330)
            self.account_table.setColumnWidth(2, 75)
            self.account_table.setColumnWidth(3, 115)
            layout.addWidget(self.account_table, 1)

            self.dashboard_hint = QLabel("定时签到和立即签到固定无头运行，不会弹出浏览器；只有“手动验证”会显示窗口。关闭面板不会停止 daemon。")
            self.dashboard_hint.setObjectName("Hint")
            layout.addWidget(self.dashboard_hint)
            return page

        def _build_account_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(12)

            toolbar = QHBoxLayout()
            add_button = QPushButton("新增账号")
            add_button.clicked.connect(self._add_account)
            edit_button = QPushButton("编辑账号")
            edit_button.setObjectName("Secondary")
            edit_button.clicked.connect(self._edit_account)
            delete_button = QPushButton("删除账号")
            delete_button.setObjectName("Danger")
            delete_button.clicked.connect(self._delete_account)
            toggle_button = QPushButton("启用/停用")
            toggle_button.setObjectName("Secondary")
            toggle_button.clicked.connect(self._toggle_account)
            refresh_button = QPushButton("刷新配置")
            refresh_button.setObjectName("Secondary")
            refresh_button.clicked.connect(self._load_accounts)
            toolbar.addWidget(add_button)
            toolbar.addWidget(edit_button)
            toolbar.addWidget(delete_button)
            toolbar.addWidget(toggle_button)
            toolbar.addStretch(1)
            toolbar.addWidget(refresh_button)
            layout.addLayout(toolbar)

            self.manage_table = QTableWidget(0, 6)
            self.manage_table.setHorizontalHeaderLabels(["账号", "站点", "状态", "Cookie", "代理", "用户 ID"])
            self.manage_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            self.manage_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
            self.manage_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self.manage_table.verticalHeader().setVisible(False)
            self.manage_table.horizontalHeader().setStretchLastSection(True)
            self.manage_table.setColumnWidth(0, 170)
            self.manage_table.setColumnWidth(1, 320)
            self.manage_table.setColumnWidth(2, 85)
            self.manage_table.setColumnWidth(3, 85)
            self.manage_table.setColumnWidth(4, 75)
            layout.addWidget(self.manage_table, 1)

            ai_box = QGroupBox("AI 视觉辅助配置")
            ai_layout = QGridLayout(ai_box)
            self.ai_enabled_check = QCheckBox("启用 AI 辅助")
            self.ai_base_url_edit = QLineEdit()
            self.ai_base_url_edit.setPlaceholderText("例如：https://api.openai.com/v1")
            self.ai_model_edit = QLineEdit()
            self.ai_model_edit.setPlaceholderText("例如：gpt-4o-mini")
            self.ai_key_edit = QLineEdit()
            self.ai_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.ai_key_edit.setPlaceholderText("API Key 默认掩码显示")
            ai_save_button = QPushButton("保存 AI 配置")
            ai_save_button.setObjectName("Secondary")
            ai_save_button.clicked.connect(self._save_ai_config)
            ai_layout.addWidget(self.ai_enabled_check, 0, 0, 1, 2)
            ai_layout.addWidget(QLabel("Base URL"), 1, 0)
            ai_layout.addWidget(self.ai_base_url_edit, 1, 1)
            ai_layout.addWidget(QLabel("Model"), 2, 0)
            ai_layout.addWidget(self.ai_model_edit, 2, 1)
            ai_layout.addWidget(QLabel("API Key"), 3, 0)
            ai_layout.addWidget(AccountDialog._secret_row(self.ai_key_edit), 3, 1)
            ai_layout.addWidget(ai_save_button, 4, 0, 1, 2)
            layout.addWidget(ai_box)

            sync_box = QGroupBox("远程配置同步")
            sync_layout = QGridLayout(sync_box)
            self.sync_status_label = QLabel("未配置远程同步")
            self.sync_status_label.setObjectName("Hint")
            self.sync_enabled_check = QCheckBox("启用远程配置同步")
            self.sync_auto_check = QCheckBox("每次签到前自动请求一次")
            self.sync_url_edit = QLineEdit()
            self.sync_url_edit.setPlaceholderText("例如：https://config.example.com/api/config")
            self.sync_method_combo = QComboBox()
            self.sync_method_combo.addItems(["GET", "POST"])
            self.sync_token_edit = QLineEdit()
            self.sync_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.sync_token_edit.setPlaceholderText("可选；请求 Token 默认以 Bearer 方式发送")
            self.sync_token_header_edit = QLineEdit("Authorization")
            self.sync_token_prefix_edit = QLineEdit("Bearer")
            self.sync_response_field_edit = QLineEdit()
            self.sync_response_field_edit.setPlaceholderText("可选，例如 data.encrypted；留空自动识别")
            self.sync_timeout_spin = QSpinBox()
            self.sync_timeout_spin.setRange(5, 300)
            self.sync_timeout_spin.setSuffix(" 秒")
            save_sync_button = QPushButton("保存同步设置")
            save_sync_button.setObjectName("Secondary")
            save_sync_button.clicked.connect(self._save_sync_config)
            sync_now_button = QPushButton("立即获取并解密保存")
            sync_now_button.clicked.connect(self._sync_config_now)
            sync_layout.addWidget(self.sync_status_label, 0, 0, 1, 4)
            sync_layout.addWidget(self.sync_enabled_check, 1, 0, 1, 2)
            sync_layout.addWidget(self.sync_auto_check, 1, 2, 1, 2)
            sync_layout.addWidget(QLabel("API URL"), 2, 0)
            sync_layout.addWidget(self.sync_url_edit, 2, 1, 1, 3)
            sync_layout.addWidget(QLabel("方法"), 3, 0)
            sync_layout.addWidget(self.sync_method_combo, 3, 1)
            sync_layout.addWidget(QLabel("请求 Token"), 3, 2)
            sync_layout.addWidget(AccountDialog._secret_row(self.sync_token_edit), 3, 3)
            sync_layout.addWidget(QLabel("Token 请求头"), 4, 0)
            sync_layout.addWidget(self.sync_token_header_edit, 4, 1)
            sync_layout.addWidget(QLabel("Token 前缀"), 4, 2)
            sync_layout.addWidget(self.sync_token_prefix_edit, 4, 3)
            sync_layout.addWidget(QLabel("响应字段"), 5, 0)
            sync_layout.addWidget(self.sync_response_field_edit, 5, 1)
            sync_layout.addWidget(QLabel("超时"), 5, 2)
            sync_layout.addWidget(self.sync_timeout_spin, 5, 3)
            sync_layout.addWidget(save_sync_button, 6, 0, 1, 2)
            sync_layout.addWidget(sync_now_button, 6, 2, 1, 2)
            sync_layout.addWidget(QLabel("说明：远程响应可直接返回明文 JSON，也可返回 AES-256-GCM 密文；解密使用 security.config_key 或 CHECKIN_CONFIG_KEY，成功后明文写回本地 config.json。"), 7, 0, 1, 4)
            layout.addWidget(sync_box)

            security_box = QGroupBox("配置安全（AES-256-GCM）")
            security_layout = QGridLayout(security_box)
            self.security_status_label = QLabel("等待配置加载…")
            self.security_status_label.setObjectName("Hint")
            self.security_key_edit = QLineEdit()
            self.security_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.security_key_edit.setPlaceholderText("至少 8 个字符；启用加密时保存到 security.config_key")
            self.security_key_confirm_edit = QLineEdit()
            self.security_key_confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.security_key_confirm_edit.setPlaceholderText("再次输入密钥")
            enable_button = QPushButton("启用/重新加密")
            enable_button.clicked.connect(self._enable_encryption)
            disable_button = QPushButton("关闭加密并导出明文")
            disable_button.setObjectName("Secondary")
            disable_button.clicked.connect(self._disable_encryption)
            export_button = QPushButton("解密导出 JSON")
            export_button.setObjectName("Secondary")
            export_button.clicked.connect(self._export_plain_config)
            import_button = QPushButton("导入 JSON")
            import_button.setObjectName("Secondary")
            import_button.clicked.connect(self._import_config)
            security_layout.addWidget(self.security_status_label, 0, 0, 1, 4)
            security_layout.addWidget(QLabel("新密钥"), 1, 0)
            security_layout.addWidget(self.security_key_edit, 1, 1, 1, 3)
            security_layout.addWidget(QLabel("确认密钥"), 2, 0)
            security_layout.addWidget(self.security_key_confirm_edit, 2, 1, 1, 3)
            security_layout.addWidget(enable_button, 3, 0)
            security_layout.addWidget(disable_button, 3, 1)
            security_layout.addWidget(export_button, 3, 2)
            security_layout.addWidget(import_button, 3, 3)
            security_layout.addWidget(QLabel("说明：密钥可在 config.json 的 security.config_key 中修改；同文件保存密钥不能防御同时取得配置文件和密文的攻击。"), 4, 0, 1, 4)
            layout.addWidget(security_box)
            return page

        def _build_schedule_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(18, 18, 18, 18)
            layout.setSpacing(14)

            settings = QFrame()
            settings.setObjectName("StatusCard")
            grid = QGridLayout(settings)
            grid.setContentsMargins(18, 18, 18, 18)
            self.enabled_check = QCheckBox("启用定时签到")
            self.run_start_check = QCheckBox("daemon 启动时立即执行一次")
            self.headless_check = QCheckBox("使用无头浏览器运行定时任务")
            self.headless_check.setChecked(True)
            self.headless_check.setEnabled(False)
            self.headless_check.setToolTip("定时签到和立即签到固定使用无头浏览器；手动验证才会显示浏览器窗口。")
            self.autostart_check = QCheckBox("Windows 登录时自动启动后台守护进程")
            self.autostart_check.toggled.connect(self._on_autostart_toggled)
            self.times_edit = QLineEdit()
            self.times_edit.setPlaceholderText("例如：09:00, 18:30")
            self.times_edit.textEdited.connect(self._mark_schedule_dirty)
            self.enabled_check.clicked.connect(self._mark_schedule_dirty)
            self.run_start_check.clicked.connect(self._mark_schedule_dirty)
            grid.addWidget(self.enabled_check, 0, 0, 1, 2)
            grid.addWidget(self.run_start_check, 1, 0, 1, 2)
            grid.addWidget(self.headless_check, 2, 0, 1, 2)
            grid.addWidget(self.autostart_check, 3, 0, 1, 2)
            grid.addWidget(QLabel("每日时间点"), 4, 0)
            grid.addWidget(self.times_edit, 4, 1)
            layout.addWidget(settings)

            account_hint = QFrame()
            account_hint.setObjectName("StatusCard")
            account_hint_layout = QVBoxLayout(account_hint)
            account_hint_layout.setContentsMargins(18, 14, 18, 14)
            account_hint_layout.addWidget(QLabel("定时签到自动使用所有已启用账号，无需单独选择账号。"))
            layout.addWidget(account_hint)

            actions = QHBoxLayout()
            save = QPushButton("保存定时配置")
            save.clicked.connect(self._save_schedule)
            actions.addWidget(save)
            actions.addStretch(1)
            self.schedule_hint = QLabel("等待 daemon 返回配置…")
            self.schedule_hint.setObjectName("Hint")
            actions.addWidget(self.schedule_hint)
            layout.addLayout(actions)
            return page

        def _build_logs_tab(self) -> QWidget:
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(16, 16, 16, 16)
            toolbar = QHBoxLayout()
            toolbar.addWidget(QLabel("daemon 最近日志（已做敏感信息保护）"))
            toolbar.addStretch(1)
            clear = QPushButton("清空显示")
            clear.setObjectName("Secondary")
            clear.clicked.connect(lambda: self.log_view.clear())
            toolbar.addWidget(clear)
            layout.addLayout(toolbar)
            self.log_view = QPlainTextEdit()
            self.log_view.setReadOnly(True)
            self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            self.log_view.setFont(QFont("Consolas", 9))
            layout.addWidget(self.log_view, 1)
            return page

        def _build_tray(self) -> None:
            icon_file = asset_path("newapi_checkin.ico")
            icon = QIcon(str(icon_file)) if icon_file.exists() else self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self.setWindowIcon(icon)
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip("NewAPI 签到中心")
            menu = self.tray.contextMenu() or __import__("PySide6.QtWidgets", fromlist=["QMenu"]).QMenu(self)
            show_action = QAction("显示面板", self)
            show_action.triggered.connect(self._show_window)
            run_action = QAction("立即签到", self)
            run_action.triggered.connect(lambda: self._run_command("run_now"))
            quit_action = QAction("退出面板（daemon 继续运行）", self)
            quit_action.triggered.connect(self._exit_gui)
            menu.addAction(show_action)
            menu.addAction(run_action)
            menu.addSeparator()
            menu.addAction(quit_action)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()

        # -------------------------- daemon ------------------------------ #
        def _ensure_daemon(self) -> None:
            if not self._daemon_enabled:
                self.connection_label.setText("后台 daemon 已关闭")
                self.connection_label.setStyleSheet("color: #f3bd67; padding: 8px 12px;")
                return
            if daemon_is_running():
                self.connection_label.setText("● daemon 在线")
                self.connection_label.setStyleSheet("color: #62d6a7; padding: 8px 12px;")
                return
            try:
                self._daemon_process = start_daemon_process()
                self.connection_label.setText("正在启动 daemon…")
            except Exception as exc:  # noqa: BLE001
                self.connection_label.setText(f"daemon 启动失败：{exc}")

        def _request(self, command: str, **payload: Any) -> None:
            if self._daemon_enabled and not daemon_is_running():
                self._ensure_daemon()

            def request_with_retry() -> dict:
                last_error: Optional[Exception] = None
                for attempt in range(12):
                    try:
                        return DaemonClient().request(command, **payload)
                    except (ConnectionError, OSError, EOFError) as exc:
                        last_error = exc
                        if not self._daemon_enabled or attempt >= 11:
                            raise
                        time.sleep(0.25)
                raise last_error or ConnectionError("daemon 未运行")

            worker = RequestThread(request_with_retry, self)
            worker.command = command
            self._workers.append(worker)
            worker.succeeded.connect(lambda result, w=worker: self._worker_done(w, result))
            worker.failed.connect(lambda error, w=worker: self._worker_failed(w, error))
            worker.finished.connect(lambda w=worker: self._worker_finished(w))
            worker.start()

        def _mark_schedule_dirty(self, *_args: Any) -> None:
            self._schedule_dirty = True
            self.schedule_hint.setText("定时配置已修改，请点击“保存定时配置”")

        def _worker_done(self, worker: RequestThread, result: Any) -> None:
            if getattr(worker, "command", "") == "set_schedule" and isinstance(result, dict):
                self._schedule_save_pending = False
                if result.get("ok"):
                    self._schedule_dirty = False
                else:
                    self.schedule_hint.setText(f"保存失败：{result.get('error') or 'daemon 拒绝了配置'}")
            if isinstance(result, dict) and result.get("operation") == "sync_config":
                if result.get("ok"):
                    self.sync_status_label.setText(str(result.get("message") or "远程配置同步完成"))
                    self.dashboard_hint.setText(
                        f"远程配置已更新，本地账号数：{result.get('account_count') or 0}"
                    )
                    self._load_accounts()
                else:
                    self.sync_status_label.setText(f"同步失败：{result.get('error') or '未知错误'}")
                    self.dashboard_hint.setText(str(result.get("error") or "远程配置同步失败"))
                return
            if isinstance(result, dict) and result.get("ok") is False:
                self.dashboard_hint.setText(str(result.get("error") or "操作失败"))
            elif isinstance(result, dict):
                self._apply_response(result)

        def _worker_failed(self, worker: RequestThread, error: str) -> None:
            if getattr(worker, "command", "") == "set_schedule":
                self._schedule_save_pending = False
                self.schedule_hint.setText(f"保存失败：{error}")
            if "daemon 未运行" in error or "Connection" in error:
                if self._daemon_enabled:
                    self.connection_label.setText("daemon 未连接，正在重试…")
                    self._ensure_daemon()
                else:
                    self.connection_label.setText("后台 daemon 已关闭")
            else:
                self.dashboard_hint.setText(error)

        def _worker_finished(self, worker: RequestThread) -> None:
            if worker in self._workers:
                self._workers.remove(worker)
            worker.deleteLater()

        def refresh(self) -> None:
            if self._refreshing:
                return
            self._refreshing = True
            client = DaemonClient()
            worker = RequestThread(lambda: self._fetch_dashboard(client), self)
            self._workers.append(worker)
            worker.succeeded.connect(lambda result, w=worker: self._refresh_done(w, result))
            worker.failed.connect(lambda error, w=worker: self._refresh_failed(w, error))
            worker.finished.connect(lambda w=worker: self._refresh_finished(w))
            worker.start()

        @staticmethod
        def _fetch_dashboard(client: DaemonClient) -> dict:
            schedule = client.request("get_schedule")
            logs = client.request("logs", limit=200)
            return {"schedule_response": schedule, "logs_response": logs}

        def _refresh_done(self, worker: RequestThread, result: dict) -> None:
            self._refreshing = False
            if self._daemon_enabled:
                self.connection_label.setText("● daemon 在线")
                self.connection_label.setStyleSheet("color: #62d6a7; padding: 8px 12px;")
            else:
                self.connection_label.setText("后台 daemon 已关闭")
                self.connection_label.setStyleSheet("color: #f3bd67; padding: 8px 12px;")
            self._apply_response(result.get("schedule_response") or {})
            logs = result.get("logs_response") or {}
            if logs.get("ok"):
                self.log_view.setPlainText("\n".join(logs.get("logs") or []))
                self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

        def _refresh_failed(self, worker: RequestThread, error: str) -> None:
            self._refreshing = False
            if self._daemon_enabled:
                self.connection_label.setText("daemon 未连接，正在重试…")
                self.connection_label.setStyleSheet("color: #f3bd67; padding: 8px 12px;")
                self._ensure_daemon()
            else:
                self.connection_label.setText("后台 daemon 已关闭")
                self.connection_label.setStyleSheet("color: #f3bd67; padding: 8px 12px;")

        def _refresh_finished(self, worker: RequestThread) -> None:
            if worker in self._workers:
                self._workers.remove(worker)
            worker.deleteLater()

        def _apply_response(self, response: dict) -> None:
            if response.get("schedule") is not None:
                self._apply_schedule(response["schedule"])
            status = response.get("status") or {}
            if status:
                self._apply_status(status)

        def _apply_status(self, status: dict) -> None:
            running = bool(status.get("running"))
            self.daemon_card.set_value("运行中" if running else "空闲", f"PID {status.get('pid') or '—'}")
            next_run = status.get("next_run") or "未计划"
            self.schedule_card.set_value(next_run.replace("T", " ")[:16], ", ".join(status.get("times") or []))
            result = status.get("last_result") or {}
            if result:
                rows = result.get("rows") or []
                ok = result.get("ok")
                self.last_card.set_value("成功" if ok else "有失败", result.get("finished_at") or "刚刚")
                self._last_rows = {str(row.get("name")): row for row in rows}
                self._update_account_table()
            elif status.get("last_error"):
                self.last_card.set_value("异常", status["last_error"])

        def _apply_schedule(self, raw: dict) -> None:
            if self._schedule_dirty:
                self.schedule_hint.setText("定时配置已修改，请点击“保存定时配置”")
                return
            try:
                schedule = ScheduleConfig.from_dict(raw)
            except ScheduleError:
                return
            self.enabled_check.setChecked(schedule.enabled)
            self.run_start_check.setChecked(schedule.run_on_start)
            self.headless_check.setChecked(True)
            self.headless_check.setEnabled(False)
            self.headless_check.setToolTip("定时签到和立即签到固定使用无头浏览器；手动验证才会显示浏览器窗口。")
            self.times_edit.setText(", ".join(schedule.times))
            self.schedule_hint.setText("已从 daemon 同步；定时任务使用所有启用账号")

        def _apply_schedule_response(self, response: dict) -> None:
            self._apply_response(response)

        def _load_accounts(self) -> None:
            try:
                document = load_document()
                cfg = load_config()
                self._document = document
                self._config_raw = copy.deepcopy(document.raw)
                self._account_records = [
                    copy.deepcopy(item) for item in (document.raw.get("accounts") or []) if isinstance(item, dict)
                ]
                self._accounts = cfg.accounts
                self._apply_ai_form(document.raw.get("ai") or {})
                self._apply_sync_form(document.raw.get("config_sync") or {})
                self._refresh_security_status()
            except (ConfigError, ConfigEncryptionError, OSError, ValueError) as exc:
                self._document = None
                self._config_raw = {}
                self._account_records = []
                self._accounts = []
                if hasattr(self, "ai_enabled_check"):
                    self._apply_ai_form({})
                if hasattr(self, "sync_enabled_check"):
                    self._apply_sync_form({})
                self.dashboard_hint.setText(str(exc))
                if hasattr(self, "security_status_label"):
                    self.security_status_label.setText(f"配置加载失败：{exc}")
            self.account_card.set_value(str(len(self._accounts)), "已加载配置账号")
            self._update_account_table()
            if hasattr(self, "manage_table"):
                self._update_management_table()

        def _refresh_security_status(self) -> None:
            if self._document is None:
                return
            mode = "已加密" if self._document.encrypted else "明文"
            key_state = "已设置密钥" if self._document.safe_meta().get("key_configured") else "未设置密钥"
            self.security_status_label.setText(
                f"当前配置：{mode}；{key_state}；密文路径：{self._document.encrypted_path}"
            )

        def _apply_ai_form(self, raw: dict) -> None:
            self.ai_enabled_check.setChecked(bool(raw.get("enabled", False)))
            self.ai_base_url_edit.setText(str(raw.get("base_url") or ""))
            self.ai_model_edit.setText(str(raw.get("model") or "gpt-4o-mini"))
            self.ai_key_edit.setText(str(raw.get("api_key") or ""))

        def _apply_sync_form(self, raw: dict) -> None:
            sync = raw if isinstance(raw, dict) else {}
            self.sync_enabled_check.setChecked(bool(sync.get("enabled", False)))
            self.sync_auto_check.setChecked(bool(sync.get("auto_before_checkin", True)))
            self.sync_url_edit.setText(str(sync.get("url") or ""))
            method = str(sync.get("method") or "GET").upper()
            self.sync_method_combo.setCurrentText(method if method in {"GET", "POST"} else "GET")
            self.sync_token_edit.setText(str(sync.get("token") or ""))
            self.sync_token_header_edit.setText(str(sync.get("token_header") or "Authorization"))
            self.sync_token_prefix_edit.setText(str(sync.get("token_prefix") or "Bearer"))
            self.sync_response_field_edit.setText(str(sync.get("response_field") or ""))
            try:
                self.sync_timeout_spin.setValue(max(5, min(300, int(sync.get("timeout") or 20))))
            except (TypeError, ValueError):
                self.sync_timeout_spin.setValue(20)
            self.sync_status_label.setText("远程同步已启用" if sync.get("enabled") else "远程同步未启用")

        def _save_sync_config(self) -> bool:
            if self._document is None:
                QMessageBox.warning(self, "保存同步设置", "当前配置未成功加载，不能覆盖原文件。")
                return False
            raw = copy.deepcopy(self._config_raw or self._document.raw)
            sync = copy.deepcopy(raw.get("config_sync") or {})
            sync.update(
                {
                    "enabled": self.sync_enabled_check.isChecked(),
                    "auto_before_checkin": self.sync_auto_check.isChecked(),
                    "url": self.sync_url_edit.text().strip(),
                    "method": self.sync_method_combo.currentText().strip().upper(),
                    "token": self.sync_token_edit.text().strip(),
                    "token_header": self.sync_token_header_edit.text().strip() or "Authorization",
                    "token_prefix": self.sync_token_prefix_edit.text().strip(),
                    "response_field": self.sync_response_field_edit.text().strip(),
                    "timeout": self.sync_timeout_spin.value(),
                }
            )
            raw["config_sync"] = sync
            try:
                self._document = save_document(
                    raw,
                    self._document.path,
                    encryption_enabled=self._document.encrypted,
                )
                self._config_raw = copy.deepcopy(self._document.raw)
                self._apply_sync_form(self._document.raw.get("config_sync") or {})
                self.sync_status_label.setText("远程同步设置已保存")
                return True
            except (ConfigError, ConfigEncryptionError, OSError, ValueError) as exc:
                QMessageBox.critical(self, "保存同步设置失败", str(exc))
                return False

        def _sync_config_now(self) -> None:
            if not self._save_sync_config():
                return
            self.sync_status_label.setText("正在请求远程配置…")
            self._request("sync_config")

        def _save_ai_config(self) -> None:
            if self._document is None:
                QMessageBox.warning(self, "保存 AI 配置", "当前配置未成功加载，不能覆盖原文件。")
                return
            raw = copy.deepcopy(self._config_raw or self._document.raw)
            ai = copy.deepcopy(raw.get("ai") or {})
            ai.update(
                {
                    "enabled": self.ai_enabled_check.isChecked(),
                    "base_url": self.ai_base_url_edit.text().strip(),
                    "model": self.ai_model_edit.text().strip() or "gpt-4o-mini",
                    "api_key": self.ai_key_edit.text().strip(),
                }
            )
            raw["ai"] = ai
            try:
                self._document = save_document(
                    raw,
                    self._document.path,
                    encryption_enabled=self._document.encrypted,
                )
                self._config_raw = copy.deepcopy(self._document.raw)
                self._load_accounts()
                self._request("reload_config")
                self.dashboard_hint.setText("AI 配置已保存，API Key 仍以掩码方式显示。")
            except (ConfigError, ConfigEncryptionError, OSError, ValueError) as exc:
                QMessageBox.critical(self, "保存 AI 配置失败", str(exc))

        def _selected_account_index(self) -> int:
            row = self.manage_table.currentRow()
            return row if 0 <= row < len(self._account_records) else -1

        def _add_account(self) -> None:
            dialog = AccountDialog(parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._account_records.append(dialog.payload())
                self._save_account_records()

        def _edit_account(self) -> None:
            index = self._selected_account_index()
            if index < 0:
                QMessageBox.information(self, "账号管理", "请先选择一个账号。")
                return
            dialog = AccountDialog(self._account_records[index], self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._account_records[index] = dialog.payload()
                self._save_account_records()

        def _delete_account(self) -> None:
            index = self._selected_account_index()
            if index < 0:
                QMessageBox.information(self, "账号管理", "请先选择一个账号。")
                return
            name = str(self._account_records[index].get("name") or "该账号")
            if QMessageBox.question(self, "删除账号", f"确定删除账号“{name}”吗？") != QMessageBox.StandardButton.Yes:
                return
            del self._account_records[index]
            self._save_account_records()

        def _toggle_account(self) -> None:
            index = self._selected_account_index()
            if index < 0:
                QMessageBox.information(self, "账号管理", "请先选择一个账号。")
                return
            record = self._account_records[index]
            record["enabled"] = not bool(record.get("enabled", True))
            self._save_account_records()

        def _save_account_records(self) -> None:
            if self._document is None:
                QMessageBox.warning(self, "保存配置", "当前配置未成功加载，不能覆盖原文件。")
                return
            raw = copy.deepcopy(self._config_raw or self._document.raw)
            raw["accounts"] = copy.deepcopy(self._account_records)
            try:
                self._document = save_document(
                    raw,
                    self._document.path,
                    encryption_enabled=self._document.encrypted,
                )
                self._config_raw = copy.deepcopy(self._document.raw)
                self.dashboard_hint.setText("账号配置已保存，daemon 将重新加载。")
                self._load_accounts()
                self._request("reload_config")
            except (ConfigError, ConfigEncryptionError, OSError, ValueError) as exc:
                QMessageBox.critical(self, "保存配置失败", str(exc))
                self._load_accounts()

        def _update_management_table(self) -> None:
            self.manage_table.setRowCount(len(self._account_records))
            for row_index, record in enumerate(self._account_records):
                values = [
                    record.get("name", ""),
                    record.get("url", ""),
                    "启用" if record.get("enabled", True) else "停用",
                    "已设置" if record.get("cookie") else "未设置",
                    "已设置" if record.get("proxy") else "无",
                    record.get("user_id", record.get("userId")) or "—",
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    if col in (3, 4):
                        item.setForeground(QColor("#8fb5ff" if value == "已设置" else "#8193b0"))
                    self.manage_table.setItem(row_index, col, item)

        def _security_key_pair(self) -> Optional[str]:
            key = self.security_key_edit.text().strip()
            confirm = self.security_key_confirm_edit.text().strip()
            if len(key) < 8:
                QMessageBox.warning(self, "配置加密", "密钥至少需要 8 个字符。")
                return None
            if key != confirm:
                QMessageBox.warning(self, "配置加密", "两次输入的密钥不一致。")
                return None
            return key

        def _enable_encryption(self) -> None:
            key = self._security_key_pair()
            if not key or self._document is None:
                return
            try:
                self._document = save_document(
                    self._config_raw or self._document.raw,
                    self._document.path,
                    encryption_enabled=True,
                    key=key,
                )
                self._config_raw = copy.deepcopy(self._document.raw)
                self._refresh_security_status()
                self.dashboard_hint.setText(f"配置已加密保存：{self._document.encrypted_path}")
                self._request("reload_config")
            except (ConfigError, ConfigEncryptionError, OSError, ValueError) as exc:
                QMessageBox.critical(self, "启用加密失败", str(exc))

        def _disable_encryption(self) -> None:
            if self._document is None:
                return
            if QMessageBox.question(self, "关闭加密", "将把当前有效配置写回明文 config.json，确定继续吗？") != QMessageBox.StandardButton.Yes:
                return
            try:
                self._document = save_document(
                    self._config_raw or self._document.raw,
                    self._document.path,
                    encryption_enabled=False,
                    key="",
                )
                self._config_raw = copy.deepcopy(self._document.raw)
                self._refresh_security_status()
                self._request("reload_config")
            except (ConfigError, ConfigEncryptionError, OSError, ValueError) as exc:
                QMessageBox.critical(self, "关闭加密失败", str(exc))

        def _export_plain_config(self) -> None:
            if self._document is None:
                return
            destination, _ = QFileDialog.getSaveFileName(self, "解密导出 JSON", str(runtime_root() / "config.decrypted.json"), "JSON (*.json)")
            if not destination:
                return
            try:
                export_plain(self._document.path, Path(destination))
                QMessageBox.information(self, "导出完成", f"已导出明文配置：\n{destination}")
            except (ConfigError, ConfigEncryptionError, OSError, ValueError) as exc:
                QMessageBox.critical(self, "导出失败", str(exc))

        def _import_config(self) -> None:
            destination, _ = QFileDialog.getOpenFileName(self, "导入 JSON", str(runtime_root()), "JSON (*.json)")
            if not destination or self._document is None:
                return
            encrypt = self._document.encrypted
            key = self.security_key_edit.text().strip() or None
            if encrypt and not key:
                key = self._document.security.config_key or None
            try:
                self._document = import_json(Path(destination), self._document.path, encrypt=encrypt, key=key)
                self._config_raw = copy.deepcopy(self._document.raw)
                self._load_accounts()
                self._request("reload_config")
                QMessageBox.information(self, "导入完成", "配置已导入并通过当前配置校验。")
            except (ConfigError, ConfigEncryptionError, OSError, ValueError) as exc:
                QMessageBox.critical(self, "导入失败", str(exc))

        def _update_account_table(self) -> None:
            self.account_table.setRowCount(len(self._accounts))
            for row_index, account in enumerate(self._accounts):
                result = self._last_rows.get(account.name) or {}
                values = [
                    account.name,
                    account.base_url,
                    "启用" if account.enabled else "停用",
                    result.get("status", "—"),
                    result.get("detail", ""),
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    if col == 2 and not account.enabled:
                        item.setForeground(QColor("#7d8aa4"))
                    self.account_table.setItem(row_index, col, item)

        def _save_schedule(self) -> None:
            times = [part.strip() for part in self.times_edit.text().replace("，", ",").split(",") if part.strip()]
            raw = {
                "enabled": self.enabled_check.isChecked(),
                "times": times,
                "account_names": [],
                "run_on_start": self.run_start_check.isChecked(),
                "headless": True,
            }
            try:
                ScheduleConfig.from_dict(raw)
            except ScheduleError as exc:
                QMessageBox.warning(self, "调度配置错误", str(exc))
                return
            self._schedule_save_pending = True
            self._request("set_schedule", schedule=raw)
            self.schedule_hint.setText("配置已提交，正在同步 daemon…")

        def _on_daemon_toggle(self, enabled: bool) -> None:
            if not daemon_control.set_enabled(enabled):
                self.daemon_toggle.blockSignals(True)
                self.daemon_toggle.setChecked(not enabled)
                self.daemon_toggle.blockSignals(False)
                QMessageBox.warning(self, "后台守护进程", "保存 daemon 运行开关失败")
                return
            self._daemon_enabled = enabled
            if enabled:
                self._set_autostart_state()
                self.dashboard_hint.setText("正在启动后台 daemon…")
                self._ensure_daemon()
                return

            if autostart.supported() and autostart.is_enabled() and not autostart.disable():
                self.dashboard_hint.setText("daemon 已关闭，但关闭 Windows 登录自启失败")
            self._set_autostart_state()
            self.connection_label.setText("后台 daemon 已关闭")
            self.connection_label.setStyleSheet("color: #f3bd67; padding: 8px 12px;")
            self.dashboard_hint.setText("后台 daemon 已关闭，GUI 不会自动重启；重新勾选即可恢复。")
            self._request("stop")

        def _on_autostart_toggled(self, enabled: bool) -> None:
            if not autostart.supported():
                return
            success = autostart.enable() if enabled else autostart.disable()
            if not success:
                self.autostart_check.blockSignals(True)
                self.autostart_check.setChecked(not enabled)
                self.autostart_check.blockSignals(False)
                QMessageBox.warning(self, "开机启动", "写入 Windows 当前用户开机启动失败")
                return
            self.schedule_hint.setText("已开启 Windows 登录自启" if enabled else "已关闭 Windows 登录自启")

        def _set_autostart_state(self) -> None:
            supported = autostart.supported()
            registry_enabled = autostart.is_enabled() if supported else False
            if supported and not self._daemon_enabled and registry_enabled:
                autostart.disable()
                registry_enabled = False
            self.autostart_check.setEnabled(supported and self._daemon_enabled)
            self.autostart_check.blockSignals(True)
            self.autostart_check.setChecked(registry_enabled if self._daemon_enabled else False)
            self.autostart_check.blockSignals(False)
            if not supported:
                self.autostart_check.setToolTip("仅 Windows 支持当前用户开机启动")
            elif not self._daemon_enabled:
                self.autostart_check.setToolTip("请先开启后台守护进程，再设置 Windows 登录自启")
            else:
                self.autostart_check.setToolTip("当前用户登录 Windows 时启动后台守护进程")

        def _run_command(self, command: str) -> None:
            self.run_button.setEnabled(False)
            self.manual_button.setEnabled(False)
            self.dashboard_hint.setText("任务已提交，签到过程在 daemon 中运行…")
            self._request(command, account_names=None)
            QTimer.singleShot(1000, lambda: (self.run_button.setEnabled(True), self.manual_button.setEnabled(True)))

        def _open_config(self) -> None:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            _open_path(CONFIG_FILE.parent)

        # -------------------------- tray/window ------------------------- #
        def _show_window(self) -> None:
            self.showNormal()
            self.raise_()
            self.activateWindow()

        def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
            if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
                self._show_window()

        def _exit_gui(self) -> None:
            self._closing = True
            self.tray.hide()
            QApplication.instance().quit()

        def closeEvent(self, event) -> None:  # noqa: N802
            if self._closing:
                event.accept()
                return
            self.hide()
            event.ignore()


    def run_gui(argv: Optional[list[str]] = None) -> int:
        args = list(argv or sys.argv)
        smoke_test = "--smoke-test" in args
        app = QApplication(args)
        app.setApplicationName("NewAPI Checkin")
        app.setQuitOnLastWindowClosed(False)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            # 仍允许面板运行；关闭窗口时不会真正退出，用户可从任务管理器结束。
            pass
        window = MainWindow(smoke_test=smoke_test)
        if smoke_test and window.tray.icon().isNull():
            return 1
        window.show()
        if smoke_test:
            QTimer.singleShot(250, app.quit)
        return app.exec()
