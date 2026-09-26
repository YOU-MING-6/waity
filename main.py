"""
一款基于 PySide6 + QFluentWidgets 的定时关机提示工具。

界面参考 Windows 11 记事本弹窗：
    - 内容区纯白；
    - 按钮区偏灰；
    - 中间一条细分割线；
    - 圆角 + 柔和阴影。
"""
import os
import sys
import argparse

from PySide6.QtCore import (
    Qt, QTimer, QVariantAnimation, QEasingCurve, QPoint, QProcess,
    QLockFile, QStandardPaths,
)
from PySide6.QtGui import QColor, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QWidget, QSystemTrayIcon, QGraphicsDropShadowEffect,
    QFrame, QVBoxLayout, QHBoxLayout,
)

from qfluentwidgets import (
    Action, BodyLabel, FluentIcon, PrimaryPushButton, ProgressBar,
    PushButton, SubtitleLabel, SystemTrayMenu, Theme,
    setTheme, setThemeColor, isDarkTheme, qconfig,
)
from qframelesswindow.utils import getSystemAccentColor


# ==================================================================
# 配置
# ==================================================================
APP_ID = "shutdowntool"
APP_NAME = "shutdowntool"
APP_DESCRIPTION = "定时关机提示工具"

ICON_FILE = "icon.png"
ICON_NIGHT_FILE = "icon_night.png"

SOCKET_NAME = f"{APP_ID}_socket"
LOCK_FILE = f"{APP_ID}.lock"

WIDTH = 600
TICK_MS = 1000
CLOSE_DELAY_MS = 500
SHUTDOWN_BUFFER_S = 5
DELAY_S = 60
NOTIFY_TIMEOUT_MS = 500
LOCK_TIMEOUT_MS = 100

SHADOW_MARGIN = 24
SHADOW_BLUR = 24
SHADOW_OFFSET_Y = 4
SHADOW_COLOR = QColor(0, 0, 0, 90)

RADIUS = 8                       # 对话框圆角半径
INNER_RADIUS = RADIUS - 1        # 内部区域圆角（比外层小 1px，避免盖住边框）


# ==================================================================
# 工具
# ==================================================================
def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, name)
    if os.path.exists(path):
        return path
    fallback = os.path.join(base, ICON_FILE)
    return fallback if os.path.exists(fallback) else path


def format_time(seconds: int) -> str:
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m} 分钟" if s == 0 else f"{m} 分 {s} 秒"
    return f"{seconds} 秒"


def shutdown_now(delay: int = 0) -> None:
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/s", "/f", "/t", str(delay)])
    else:
        QProcess.startDetached("shutdown", ["-h", "-t", str(delay)])


def cancel_shutdown() -> None:
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/a"])


def center_on_screen(widget: QWidget) -> None:
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    widget.adjustSize()
    widget.move(
        geo.x() + (geo.width() - widget.width()) // 2,
        geo.y() + (geo.height() - widget.height()) // 2,
    )


# ==================================================================
# 单实例
# ==================================================================
class SingleInstance:
    def __init__(self) -> None:
        lock_path = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.TempLocation),
            LOCK_FILE,
        )
        self._lock = QLockFile(lock_path)

    def acquire(self) -> bool:
        return self._lock.tryLock(LOCK_TIMEOUT_MS)

    def release(self) -> None:
        self._lock.unlock()

    def notify_show(self) -> None:
        sock = QLocalSocket()
        sock.connectToServer(SOCKET_NAME)
        if sock.waitForConnected(NOTIFY_TIMEOUT_MS):
            sock.write(b"SHOW")
            sock.waitForBytesWritten(NOTIFY_TIMEOUT_MS)
            sock.disconnectFromServer()


# ==================================================================
# ShutdownMessageBox（Win11 记事本弹窗风格）
# ==================================================================
class ShutdownMessageBox(QWidget):
    """
    结构：
        ┌───────────────────────────┐
        │  内容区（纯白）            │  ← contentFrame
        │  标题 / 描述 / 进度条      │
        ├───────────────────────────┤  ← 1px 分隔线
        │  按钮区（浅灰）            │  ← buttonFrame
        │  [已阅][延迟][关机] [取消] │
        └───────────────────────────┘
    """

    def __init__(self, countdown: int) -> None:
        super().__init__()
        self.remaining = countdown
        self.total = countdown
        self._progress_anim: QVariantAnimation | None = None
        self._drag_offset: QPoint | None = None

        self._setup_window()
        self._setup_content()
        self._setup_buttons()
        self._apply_style()
        self.update_content()

    # ---------- 窗口 ----------
    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # 外层留阴影边距
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN
        )

        # 圆角背景容器（承载阴影、边框、整体圆角）
        self.container = QFrame(self)
        self.container.setObjectName("shutdownContainer")
        self.container.setAttribute(Qt.WA_StyledBackground, True)
        self.container.setFixedWidth(WIDTH)
        self._attach_shadow()
        outer.addWidget(self.container)

        # 容器内部：上内容区 + 下按钮区，无间距
        main_layout = QVBoxLayout(self.container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 内容区（白底，透明以继承容器白）
        self.contentFrame = QFrame(self.container)
        self.contentFrame.setObjectName("contentFrame")
        self.contentFrame.setAttribute(Qt.WA_StyledBackground, True)
        main_layout.addWidget(self.contentFrame)

        # 按钮区（浅灰底）
        self.buttonFrame = QFrame(self.container)
        self.buttonFrame.setObjectName("buttonFrame")
        self.buttonFrame.setAttribute(Qt.WA_StyledBackground, True)
        main_layout.addWidget(self.buttonFrame)

    def _attach_shadow(self) -> None:
        shadow = QGraphicsDropShadowEffect(self.container)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setOffset(0, SHADOW_OFFSET_Y)
        shadow.setColor(SHADOW_COLOR)
        self.container.setGraphicsEffect(shadow)

    def _apply_style(self) -> None:
        """由 MainWindow._on_theme_changed 在主题切换时也会调用。"""
        if isDarkTheme():
            container_bg, container_border = "#2B2B2B", "#3A3A3A"
            button_bg = "#1F1F1F"
        else:
            container_bg, container_border = "#FFFFFF", "#E5E5E5"
            button_bg = "#F5F5F5"

        self.container.setStyleSheet(f"""
            #shutdownContainer {{
                background-color: {container_bg};
                border: 1px solid {container_border};
                border-radius: {RADIUS}px;
            }}
            #contentFrame {{
                background-color: transparent;
                border: none;
            }}
            #buttonFrame {{
                background-color: {button_bg};
                border: none;
                border-top: 1px solid {container_border};
                border-bottom-left-radius: {INNER_RADIUS}px;
                border-bottom-right-radius: {INNER_RADIUS}px;
            }}
        """)

    # ---------- 内容 ----------
    def _setup_content(self) -> None:
        layout = QVBoxLayout(self.contentFrame)
        layout.setSpacing(8)
        layout.setContentsMargins(24, 24, 24, 20)

        self.contentLabel = BodyLabel("", self.contentFrame)
        self.contentLabel.setWordWrap(True)

        self.progressBar = ProgressBar(self.contentFrame)
        self.progressBar.setValue(100)

        layout.addWidget(SubtitleLabel("要关机吗？", self.contentFrame))
        layout.addWidget(self.contentLabel)
        layout.addSpacing(4)
        layout.addWidget(self.progressBar)

    # ---------- 按钮 ----------
    def _setup_buttons(self) -> None:
        self.accept_btn = PrimaryPushButton(
            FluentIcon.ACCEPT, "已阅", self.buttonFrame
        )
        self.delay_btn = PushButton(
            FluentIcon.HISTORY, "延迟 1 分钟", self.buttonFrame
        )
        self.shutdown_btn = PushButton(
            FluentIcon.POWER_BUTTON, "立即关机", self.buttonFrame
        )
        self.cancel_btn = PushButton(
            FluentIcon.CLOSE, "取消关机计划", self.buttonFrame
        )

        row = QHBoxLayout(self.buttonFrame)
        row.setContentsMargins(24, 16, 24, 16)
        row.setSpacing(8)
        # 常规操作靠左，破坏性操作靠右，用留白隔开
        row.addWidget(self.accept_btn)
        row.addWidget(self.delay_btn)
        row.addWidget(self.shutdown_btn)
        row.addStretch(1)
        row.addWidget(self.cancel_btn)

    # ---------- 内容 / 进度 ----------
    def update_content(
        self, remaining: int | None = None, total: int | None = None
    ) -> None:
        if remaining is not None:
            self.remaining = remaining
        if total is not None:
            self.total = total

        self.contentLabel.setText(
            f"当前为放学时段；计算机将在 {format_time(self.remaining)}后自动关闭。"
        )
        self._animate_progress(self._target_progress())

    def _target_progress(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, round(self.remaining * 100 / self.total)))

    def _animate_progress(self, target: int) -> None:
        if self._progress_anim is None:
            self._progress_anim = QVariantAnimation(self)
            self._progress_anim.setEasingCurve(QEasingCurve.Type.Linear)
            self._progress_anim.valueChanged.connect(
                lambda v: self.progressBar.setValue(int(v))
            )
        self._progress_anim.stop()
        self._progress_anim.setDuration(TICK_MS)
        self._progress_anim.setStartValue(self.progressBar.value())
        self._progress_anim.setEndValue(target)
        self._progress_anim.start()

    # ---------- 拖动 ----------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)


# ==================================================================
# TrayIcon
# ==================================================================
class TrayIcon(QSystemTrayIcon):
    def __init__(self, controller: "MainWindow") -> None:
        super().__init__(parent=controller)
        self.setIcon(controller.windowIcon())

        menu = SystemTrayMenu(parent=controller)
        self._time_action = Action(FluentIcon.HISTORY, "剩余时间：", controller)
        menu.addAction(self._time_action)
        menu.addSeparator()
        menu.addAction(Action(
            FluentIcon.SYNC, "延迟 1 分钟",
            controller, triggered=controller.on_delay_clicked,
        ))
        menu.addAction(Action(
            FluentIcon.CLOSE, "取消关机计划",
            controller, triggered=controller.cancel_shutdown,
        ))
        self.setContextMenu(menu)

    def update_remaining(self, remaining: int) -> None:
        text = format_time(remaining)
        self._time_action.setText(f"剩余时间：{text}")
        self.setToolTip(f"{APP_NAME}：{text}后自动关机")


# ==================================================================
# MainWindow（不可见宿主 + 控制器）
# ==================================================================
class MainWindow(QWidget):
    def __init__(self, countdown: int, single_instance: SingleInstance) -> None:
        super().__init__()
        self._si = single_instance
        self.remaining = countdown
        self.total = countdown
        self._centered = False

        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.Tool)
        self.resize(1, 1)

        setTheme(Theme.AUTO)
        if sys.platform in ("win32", "darwin"):
            setThemeColor(getSystemAccentColor(), save=False)

        qconfig.themeChanged.connect(self._on_theme_changed)
        self._apply_icon()

        self.message_box = ShutdownMessageBox(countdown)
        self.message_box.accept_btn.clicked.connect(self.on_accept)
        self.message_box.shutdown_btn.clicked.connect(self.on_shutdown_now)
        self.message_box.delay_btn.clicked.connect(self.on_delay_clicked)
        self.message_box.cancel_btn.clicked.connect(self.cancel_shutdown)

        self.tray = TrayIcon(self)
        self.tray.update_remaining(self.remaining)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        self.server = QLocalServer(self)
        self.server.removeServer(SOCKET_NAME)
        self.server.listen(SOCKET_NAME)
        self.server.newConnection.connect(self._on_new_connection)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

        self.show_reminder()

    # ---------- 图标 ----------
    def _current_icon_path(self) -> str:
        name = ICON_NIGHT_FILE if isDarkTheme() else ICON_FILE
        return resource_path(name)

    def _apply_icon(self) -> None:
        icon = QIcon(self._current_icon_path())
        self.setWindowIcon(icon)
        if hasattr(self, "tray"):
            self.tray.setIcon(icon)

    def _on_theme_changed(self, *_):
        self._apply_icon()
        if hasattr(self, "message_box"):
            self.message_box._apply_style()

    # ---------- 显示 ----------
    def show_reminder(self) -> None:
        if not self._centered:
            center_on_screen(self.message_box)
            self._centered = True
        self.message_box.show()
        self.message_box.raise_()
        self.message_box.activateWindow()

    # ---------- 单实例消息 ----------
    def _on_new_connection(self) -> None:
        sock = self.server.nextPendingConnection()
        if not sock:
            return
        if sock.waitForReadyRead(NOTIFY_TIMEOUT_MS):
            if sock.readAll().data() == b"SHOW":
                self.show_reminder()
        sock.disconnectFromServer()

    # ---------- 托盘 ----------
    def on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_reminder()

    # ---------- 倒计时 ----------
    def _tick(self) -> None:
        self.remaining -= 1
        if self.remaining > 0:
            self._refresh_ui()
        else:
            self.timer.stop()
            shutdown_now()

    def _refresh_ui(self) -> None:
        self.message_box.update_content(self.remaining, self.total)
        self.tray.update_remaining(self.remaining)

    # ---------- 按钮 ----------
    def on_accept(self) -> None:
        self.message_box.close()

    def on_delay_clicked(self) -> None:
        self.remaining += DELAY_S
        self.total += DELAY_S
        self._refresh_ui()

    def on_shutdown_now(self) -> None:
        self.timer.stop()
        shutdown_now(SHUTDOWN_BUFFER_S)

    def cancel_shutdown(self) -> None:
        cancel_shutdown()
        self.message_box.close()
        QTimer.singleShot(CLOSE_DELAY_MS, self._quit)

    # ---------- 退出 ----------
    def _quit(self) -> None:
        self.timer.stop()
        self.tray.hide()
        self.server.close()
        self._si.release()
        QApplication.quit()


# ==================================================================
# 入口
# ==================================================================
def main() -> None:
    parser = argparse.ArgumentParser(description=APP_DESCRIPTION)
    parser.add_argument("--countdown", type=int, default=15,
                        help="默认倒计时时长（秒）")
    args = parser.parse_args()
    if args.countdown <= 0:
        print("错误：--countdown 必须为大于 0 的整数")
        sys.exit(1)

    app = QApplication(sys.argv)

    si = SingleInstance()
    if not si.acquire():
        si.notify_show()
        sys.exit(0)

    window = MainWindow(args.countdown, si)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
