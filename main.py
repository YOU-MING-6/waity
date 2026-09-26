"""
一款基于 PySide6 + QFluentWidgets 的定时关机提示工具。

行为：
    - 启动后立即显示置顶的提醒对话框（Win11 风格）；
    - 对话框使用系统阴影 + 打开/关闭动画；
    - 对话框可拖动；
    - 平滑倒计时进度条；
    - 托盘常驻，单击图标重新显示对话框；
    - 图标跟随深浅主题自动切换（icon.png / icon_night.png）。
"""
import os
import sys
import argparse

from PySide6.QtCore import (
    Qt, QTimer, QVariantAnimation, QEasingCurve, QPoint, QProcess,
    QLockFile, QStandardPaths, QAbstractAnimation,
)
from PySide6.QtGui import QColor, QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QWidget, QSystemTrayIcon, QGraphicsDropShadowEffect,
    QFrame, QVBoxLayout, QHBoxLayout, QDialog,
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

WIDTH = 460                      # 对话框内容宽度
TICK_MS = 1000                   # 倒计时 / 进度条动画步长
CLOSE_DELAY_MS = 500             # 关闭对话框后退出前的延迟
SHUTDOWN_BUFFER_S = 3            # “立即关机”缓冲
DELAY_S = 60                     # 每次延迟增加的秒数
NOTIFY_TIMEOUT_MS = 500
LOCK_TIMEOUT_MS = 100

# 打开 / 关闭动画
OPEN_DURATION_MS = 180
CLOSE_DURATION_MS = 140

# 阴影（外层留白 + DropShadow）
SHADOW_MARGIN = 20
SHADOW_BLUR = 24
SHADOW_OFFSET_Y = 4
SHADOW_COLOR = QColor(0, 0, 0, 80)


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
# ShutdownMessageBox
# ==================================================================
class ShutdownMessageBox(QDialog):
    def __init__(self, countdown: int) -> None:
        super().__init__()
        self.remaining = countdown
        self.total = countdown
        self._progress_anim: QVariantAnimation | None = None
        self._drag_offset: QPoint | None = None
        self._closing = False

        self._setup_window()
        self._setup_content()
        self._setup_buttons()
        self.update_content()

    # ---------- 窗口 ----------
    def _setup_window(self) -> None:
        # 使用 QDialog，让 Qt 走系统对话框行为（激活、Z 序、动画基座）
        self.setWindowFlags(
            Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(False)

        # 外层留出阴影空间
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN
        )
        outer.setSpacing(0)

        self.container = QFrame(self)
        self.container.setObjectName("shutdownContainer")
        self.container.setFixedWidth(WIDTH)
        self._apply_style()
        self._attach_shadow()

        outer.addWidget(self.container)

    def _apply_style(self) -> None:
        if isDarkTheme():
            bg = "#2B2B2B"
            footer_bg = "#323232"
            border = "rgba(255, 255, 255, 0.08)"
            divider = "rgba(255, 255, 255, 0.08)"
        else:
            bg = "#FFFFFF"
            footer_bg = "#F9F9F9"
            border = "rgba(0, 0, 0, 0.06)"
            divider = "rgba(0, 0, 0, 0.08)"

        self.container.setStyleSheet(f"""
            #shutdownContainer {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            #footerArea {{
                background-color: {footer_bg};
                border-top: 1px solid {divider};
                border-bottom-left-radius: 8px;
                border-bottom-right-radius: 8px;
            }}
        """)

    def _attach_shadow(self) -> None:
        shadow = QGraphicsDropShadowEffect(self.container)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setOffset(0, SHADOW_OFFSET_Y)
        shadow.setColor(SHADOW_COLOR)
        self.container.setGraphicsEffect(shadow)

    # ---------- 内容 ----------
    def _setup_content(self) -> None:
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 内容区
        content = QFrame(self.container)
        content.setObjectName("contentArea")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(24, 20, 24, 20)
        cl.setSpacing(10)

        cl.addWidget(SubtitleLabel("要关机吗？", content))

        self.contentLabel = BodyLabel("", content)
        self.contentLabel.setWordWrap(True)
        cl.addWidget(self.contentLabel)

        self.progressBar = ProgressBar(content)
        self.progressBar.setValue(100)
        cl.addWidget(self.progressBar)

        layout.addWidget(content)

        # 底部按钮区
        footer = QFrame(self.container)
        footer.setObjectName("footerArea")
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(16, 12, 16, 12)
        fl.setSpacing(8)

        self._footer_layout = fl
        layout.addWidget(footer)

    def _setup_buttons(self) -> None:
        self.cancel_btn = PushButton(FluentIcon.CLOSE, "取消关机计划")
        self.delay_btn = PushButton(FluentIcon.HISTORY, "延迟 1 分钟")
        self.shutdown_btn = PushButton(FluentIcon.POWER_BUTTON, "立即关机")
        self.accept_btn = PrimaryPushButton(FluentIcon.ACCEPT, "已阅")

        self._footer_layout.addStretch(1)
        self._footer_layout.addWidget(self.cancel_btn)
        self._footer_layout.addWidget(self.delay_btn)
        self._footer_layout.addWidget(self.shutdown_btn)
        self._footer_layout.addWidget(self.accept_btn)

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

    # ---------- 打开 / 关闭动画 ----------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._closing:
            return
        # 淡入
        self.setWindowOpacity(0.0)
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(OPEN_DURATION_MS)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def closeEvent(self, event) -> None:
        # 已经播过关闭动画：真正关闭
        if self._closing:
            event.accept()
            return
        # 第一次 close：拦截，播淡出后再关
        event.ignore()
        self._closing = True
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(CLOSE_DURATION_MS)
        anim.setStartValue(self.windowOpacity())
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        anim.finished.connect(self.close)
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

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

        # 不可见宿主
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.Tool)
        self.resize(1, 1)

        # 主题
        setTheme(Theme.AUTO)
        if sys.platform in ("win32", "darwin"):
            setThemeColor(getSystemAccentColor(), save=False)

        # 监听主题变化：切图标 + 重刷对话框 QSS
        qconfig.themeChanged.connect(self._on_theme_changed)

        # 应用图标（此时主题已确定）
        self._apply_icon()

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

    # ---------- 显示 / 隐藏 ----------
    def show_reminder(self) -> None:
        # 若上次关闭，重建 _closing 状态以允许再次显示
        self.message_box._closing = False

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
        QTimer.singleShot(CLOSE_DELAY_MS + CLOSE_DURATION_MS, self._quit)

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
