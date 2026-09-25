"""
Waity - 基于 PySide6 + QFluentWidgets 的定时关机提示工具。

行为：
    - 启动后立即显示置顶的提醒对话框（首次显示时屏幕居中，带窗口阴影）；
    - 对话框可拖动；
    - 平滑倒计时进度条；
    - 点“延迟 1 分钟”固定 +60 秒，不关闭窗口；
    - 托盘常驻，单击图标重新显示对话框。
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
    QVBoxLayout, QHBoxLayout, QFrame,
)

from qfluentwidgets import (
    Action, BodyLabel, FluentIcon, PrimaryPushButton, ProgressBar,
    PushButton, SubtitleLabel, SystemTrayMenu, Theme,
    isDarkTheme, setTheme, setThemeColor,
)
from qframelesswindow.utils import getSystemAccentColor


# ==================================================================
# 配置
# ==================================================================
APP_NAME = "Waity"
SOCKET_NAME = "waity_socket"
ICON_FILE = "icon.png"

WIDTH = 600                      # 对话框内容宽度
TICK_MS = 1000                   # 倒计时 / 进度条动画步长
CLOSE_DELAY_MS = 500             # 关闭对话框后退出前的延迟
SHUTDOWN_BUFFER_S = 5            # “立即关机”缓冲
DELAY_S = 60                     # 每次延迟增加的秒数
NOTIFY_TIMEOUT_MS = 500          # 单实例消息超时
LOCK_TIMEOUT_MS = 100            # 单实例加锁超时

# 窗口阴影
SHADOW_MARGIN = 24               # 窗口四周为阴影预留的透明边距（px）
SHADOW_BLUR = 24                 # 阴影模糊半径
SHADOW_OFFSET_Y = 4              # 阴影向下偏移
SHADOW_COLOR = QColor(0, 0, 0, 90)   # 阴影颜色（半透明黑）


# ==================================================================
# 工具
# ==================================================================
def resource_path(name: str) -> str:
    """兼容 PyInstaller 与开发环境。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def format_time(seconds: int) -> str:
    """'X 分钟' / 'X 分 X 秒' / 'X 秒'"""
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m} 分钟" if s == 0 else f"{m} 分 {s} 秒"
    return f"{seconds} 秒"


def shutdown_now(delay: int = 0) -> None:
    """异步执行系统关机，避免阻塞主线程。"""
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/s", "/f", "/t", str(delay)])
    else:
        QProcess.startDetached("shutdown", ["-h", "-t", str(delay)])


def cancel_shutdown() -> None:
    """异步取消已计划的关机（Windows）。"""
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/a"])


def center_on_screen(widget: QWidget) -> None:
    """将顶层 widget 居中到所在屏幕。"""
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
    """QLockFile 保证唯一实例；QLocalSocket 用于唤起已运行的实例。"""

    def __init__(self) -> None:
        lock_path = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.TempLocation),
            f"{SOCKET_NAME}.lock",
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
# ShutdownMessageBox（独立顶层窗口，无遮罩，带阴影）
# ==================================================================
class ShutdownMessageBox(QWidget):
    """关机提示对话框：圆角、可拖动、带窗口阴影、含平滑进度条。"""

    def __init__(self, countdown: int) -> None:
        super().__init__()
        self.remaining = countdown
        self.total = countdown
        self._progress_anim: QVariantAnimation | None = None
        self._drag_offset: QPoint | None = None

        self._setup_window()
        self._setup_content()
        self._setup_buttons()
        self.update_content()

    # ---------- 窗口 ----------
    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        # 透明背景仅用于让阴影边距与圆角外的区域真正透明
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # 外层留出阴影边距
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN
        )

        # 圆角背景容器
        self.container = QFrame(self)
        self.container.setObjectName("shutdownContainer")
        self.container.setFixedWidth(WIDTH)
        self._apply_style()
        self._attach_shadow()

        outer.addWidget(self.container)

    def _apply_style(self) -> None:
        if isDarkTheme():
            bg, border = "#2B2B2B", "rgba(255, 255, 255, 0.08)"
        else:
            bg, border = "#F3F3F3", "rgba(0, 0, 0, 0.06)"
        self.container.setStyleSheet(
            f"#shutdownContainer {{ background-color: {bg};"
            f" border: 1px solid {border}; border-radius: 8px; }}"
        )

    def _attach_shadow(self) -> None:
        """为圆角容器挂上系统风格的柔和阴影。"""
        shadow = QGraphicsDropShadowEffect(self.container)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setOffset(0, SHADOW_OFFSET_Y)
        shadow.setColor(SHADOW_COLOR)
        self.container.setGraphicsEffect(shadow)

    # ---------- 内容 ----------
    def _setup_content(self) -> None:
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(12)

        self.contentLabel = BodyLabel("", self.container)
        self.contentLabel.setWordWrap(True)

        self.progressBar = ProgressBar(self.container)
        self.progressBar.setValue(100)

        layout.addWidget(SubtitleLabel("即将关机", self.container))
        layout.addWidget(self.contentLabel)
        layout.addSpacing(4)
        layout.addWidget(self.progressBar)

        self._contentLayout = layout

    def _setup_buttons(self) -> None:
        self.accept_btn = PrimaryPushButton(FluentIcon.ACCEPT, "已阅", self.container)
        self.shutdown_btn = PushButton(FluentIcon.POWER_BUTTON, "立即关机", self.container)
        self.delay_btn = PushButton(FluentIcon.DATE_TIME, "延迟 1 分钟", self.container)
        self.cancel_btn = PushButton(FluentIcon.CLOSE, "取消关机计划", self.container)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.delay_btn)
        row.addWidget(self.shutdown_btn)
        row.addStretch(1)
        row.addWidget(self.accept_btn)

        self._contentLayout.addSpacing(8)
        self._contentLayout.addLayout(row)

    # ---------- 内容 / 进度 ----------
    def update_content(
        self, remaining: int | None = None, total: int | None = None
    ) -> None:
        if remaining is not None:
            self.remaining = remaining
        if total is not None:
            self.total = total

        self.contentLabel.setText(
            f"计算机将在{format_time(self.remaining)}后自动关闭。"
            "请及时保存您的工作或选择其他操作。"
        )
        self._animate_progress(self._target_progress())

    def _target_progress(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, round(self.remaining * 100 / self.total)))

    def _animate_progress(self, target: int) -> None:
        """让进度在 TICK_MS 内线性过渡到目标值，避免每秒跳变。"""
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
            FluentIcon.DATE_TIME, "延迟 1 分钟",
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

        # 不可见宿主
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(resource_path(ICON_FILE)))
        self.setWindowFlags(Qt.Tool)
        self.resize(1, 1)

        # 主题
        setTheme(Theme.AUTO)
        if sys.platform in ("win32", "darwin"):
            setThemeColor(getSystemAccentColor(), save=False)

        # 对话框
        self.message_box = ShutdownMessageBox(countdown)
        self.message_box.accept_btn.clicked.connect(self.on_accept)
        self.message_box.shutdown_btn.clicked.connect(self.on_shutdown_now)
        self.message_box.delay_btn.clicked.connect(self.on_delay_clicked)
        self.message_box.cancel_btn.clicked.connect(self.cancel_shutdown)

        # 托盘
        self.tray = TrayIcon(self)
        self.tray.update_remaining(self.remaining)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

        # 单实例服务端
        self.server = QLocalServer(self)
        self.server.removeServer(SOCKET_NAME)
        self.server.listen(SOCKET_NAME)
        self.server.newConnection.connect(self._on_new_connection)

        # 倒计时
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

        self.show_reminder()

    # ---------- 显示 / 隐藏 ----------
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
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--countdown", type=int, default=60,
                        help="倒计时时长（秒），默认 60 秒")
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
