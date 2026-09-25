"""
Waity - 基于 PySide6 + QFluentWidgets 的定时关机提示工具。

行为：
    - 启动后立即显示置顶的提醒对话框（无全屏遮罩）；
    - 点击对话框阴影区域会有提示音 + 抖动反馈；
    - 托盘常驻，可随时唤起 / 延迟 / 取消。

分层：
    Config          - 常量集中管理
    Utils           - 与 UI 无关的工具函数
    SingleInstance  - 单实例控制（QLockFile + QLocalSocket）
    ShutdownMessageBox - 关机提示对话框
    TrayIcon        - 系统托盘
    MainWindow      - 不可见的控制器宿主（协调对话框 / 托盘 / 定时器）
    main()          - 参数解析与启动
"""
import os
import sys
import time
import argparse

from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QPoint,
    QLockFile, QStandardPaths,
)
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon

from qfluentwidgets import (
    Action, BodyLabel, FluentIcon, InfoBar, InfoBarPosition,
    MessageBoxBase, PrimaryPushButton, PushButton, SubtitleLabel,
    SystemTrayMenu, Theme, setTheme, setThemeColor,
)
from qframelesswindow.utils import getSystemAccentColor


# ==================================================================
# Config
# ==================================================================
APP_NAME = "Waity"
SOCKET_NAME = "waity_single_instance_socket"
LOCK_FILE_NAME = "waity_single_instance.lock"
ICON_FILE_NAME = "icon.png"

MESSAGE_BOX_WIDTH = 580          # 对话框固定宽度
SHAKE_DURATION_MS = 500          # 抖动动画时长
DIALOG_CLOSE_DELAY_MS = 500      # 关闭对话框后的延迟（等待关闭动画）
IMMEDIATE_SHUTDOWN_DELAY = 5     # “立即关机”前保留的缓冲秒数
NOTIFY_TIMEOUT_MS = 500          # 单实例消息等待超时
LOCK_ACQUIRE_TIMEOUT_MS = 100    # 尝试获取锁的超时
LOCK_RETRY_INTERVAL_S = 0.1      # --overwrite 时轮询获取锁的间隔
LOCK_RETRY_TOTAL_S = 3.0         # --overwrite 时等待旧实例退出的最长时间


# ==================================================================
# Utils
# ==================================================================
def get_resource_path(relative_path: str) -> str:
    """获取资源绝对路径，兼容开发 / PyInstaller / Nuitka 三种环境。"""
    if hasattr(sys, "_MEIPASS"):            # PyInstaller
        base_path = sys._MEIPASS
    elif "__compiled__" in globals():       # Nuitka
        base_path = os.path.dirname(sys.executable)
    else:                                   # 开发环境
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def format_time(seconds: int) -> str:
    """将秒数格式化为 'X 分钟' / 'X 分 X 秒' / 'X 秒'（无前导空格）。"""
    if seconds >= 60:
        minutes, rem = divmod(seconds, 60)
        return f"{minutes} 分钟" if rem == 0 else f"{minutes} 分 {rem} 秒"
    return f"{seconds} 秒"


def run_shutdown(force: bool = False, delay: int = 0) -> None:
    """执行系统关机命令。"""
    if sys.platform == "win32":
        force_flag = "/f" if force else ""
        os.system(f"shutdown /s {force_flag} /t {delay}".strip())
    else:
        os.system(f"shutdown -h -t {delay}")


def cancel_system_shutdown() -> None:
    """取消系统已经计划的关机（Windows）。"""
    if sys.platform == "win32":
        os.system("shutdown /a")


def system_beep() -> None:
    """跨平台的提示音。"""
    if sys.platform == "win32":
        import winsound
        winsound.MessageBeep()
    else:
        QApplication.beep()


# ==================================================================
# SingleInstance
# ==================================================================
class SingleInstance:
    """
    基于 QLockFile + QLocalSocket 的单实例控制。
    - QLockFile 保证同一时刻只有一个实例能拿到锁；
    - QLocalSocket 用于向已运行的实例发送 SHOW / QUIT 指令。
    """

    def __init__(
        self,
        socket_name: str = SOCKET_NAME,
        lock_file_name: str = LOCK_FILE_NAME,
    ) -> None:
        self.socket_name = socket_name
        lock_path = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.TempLocation),
            lock_file_name,
        )
        self._lock = QLockFile(lock_path)

    def acquire(self) -> bool:
        """尝试获取锁；返回 False 表示已有实例在运行。"""
        return self._lock.tryLock(LOCK_ACQUIRE_TIMEOUT_MS)

    def release(self) -> None:
        self._lock.unlock()

    def notify_existing(self, message: str) -> bool:
        """向已存在的实例发送消息，返回是否成功。"""
        socket = QLocalSocket()
        socket.connectToServer(self.socket_name)
        if not socket.waitForConnected(NOTIFY_TIMEOUT_MS):
            return False
        socket.write(message.encode())
        socket.waitForBytesWritten(NOTIFY_TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def wait_for_release(self) -> bool:
        """在 --overwrite 场景下，轮询等待旧实例释放锁。"""
        deadline = time.monotonic() + LOCK_RETRY_TOTAL_S
        while time.monotonic() < deadline:
            if self.acquire():
                return True
            time.sleep(LOCK_RETRY_INTERVAL_S)
        return False


# ==================================================================
# ShutdownMessageBox
# ==================================================================
class ShutdownMessageBox(MessageBoxBase):
    """关机提示对话框（顶层置顶显示）。"""

    def __init__(self, args: argparse.Namespace, parent=None) -> None:
        super().__init__(parent)
        self.args = args
        self.remaining: int = args.countdown
        self._shake_anim: QPropertyAnimation | None = None

        self.widget.setFixedWidth(MESSAGE_BOX_WIDTH)
        self._setup_content()
        self._setup_buttons()
        self.update_content()

    # ---------- 初始化 ----------
    def _setup_content(self) -> None:
        self.titleLabel = SubtitleLabel("即将关机", self)
        self.contentLabel = BodyLabel("", self)
        self.contentLabel.setWordWrap(True)

        self.viewLayout.addWidget(self.titleLabel)
        self.viewLayout.addWidget(self.contentLabel)

    def _setup_buttons(self) -> None:
        # 隐藏 MessageBoxBase 的默认按钮，使用自定义布局
        self.yesButton.hide()
        self.cancelButton.hide()

        self.accept_btn = PrimaryPushButton(FluentIcon.ACCEPT, "已阅", self)
        self.shutdown_btn = PushButton(FluentIcon.POWER_BUTTON, "立即关机", self)
        self.delay_btn = PushButton(
            FluentIcon.DATE_TIME, f"延迟{format_time(self.args.delay)}", self
        )
        self.cancel_btn = PushButton(FluentIcon.CLOSE, "取消关机计划", self)

        # 次要操作在左，主操作在右（符合 Fluent 视觉习惯）
        self.buttonLayout.addWidget(self.cancel_btn)
        self.buttonLayout.addWidget(self.delay_btn)
        self.buttonLayout.addWidget(self.shutdown_btn)
        self.buttonLayout.addStretch(1)
        self.buttonLayout.addWidget(self.accept_btn)

        if self.args.hide_cancel:
            self.cancel_btn.hide()

    # ---------- 内容更新 ----------
    def update_content(self, remaining: int | None = None) -> None:
        if remaining is not None:
            self.remaining = remaining
        self.contentLabel.setText(
            f"计算机将在{format_time(self.remaining)}后自动关闭。"
            "请及时保存您的工作或选择其他操作。"
        )

    # ---------- 交互：点击对话框阴影区域时的反馈 ----------
    def mousePressEvent(self, event) -> None:
        clicked_outside = not self.widget.geometry().contains(
            event.position().toPoint()
        )
        if clicked_outside:
            self.play_feedback()
        super().mousePressEvent(event)

    # ---------- 公共反馈：提示音 + 抖动 ----------
    def play_feedback(self) -> None:
        """触发提示音和抖动。"""
        if not self.args.no_beep:
            system_beep()
        if not self.args.no_shake:
            self.play_shake()

    def play_shake(self) -> None:
        """复用同一个动画对象，避免反复 new 造成的内存堆积。"""
        if self._shake_anim and self._shake_anim.state() == QPropertyAnimation.Running:
            self._shake_anim.stop()
            self.widget.move(self._shake_anim.startValue())

        base = self.widget.pos()
        anim = QPropertyAnimation(self.widget, b"pos", self)
        anim.setDuration(SHAKE_DURATION_MS)
        anim.setStartValue(base)

        offsets = [-10, 10, -8, 8, -6, 6, -4, 4, -2, 2]
        step = len(offsets) + 1
        for i, dx in enumerate(offsets, start=1):
            anim.setKeyValueAt(i / step, base + QPoint(dx, 0))

        anim.setEndValue(base)
        anim.start()
        self._shake_anim = anim


# ==================================================================
# TrayIcon
# ==================================================================
class TrayIcon(QSystemTrayIcon):
    """系统托盘。"""

    def __init__(self, controller: "MainWindow") -> None:
        super().__init__(parent=controller)
        self.controller = controller
        self.setIcon(controller.windowIcon())

        self.menu = SystemTrayMenu(parent=controller)
        self.time_action = Action(FluentIcon.HISTORY, "剩余时间：", controller)
        self.menu.addAction(self.time_action)
        self.menu.addSeparator()

        delay_action = Action(
            FluentIcon.DATE_TIME,
            f"延迟{format_time(controller.args.delay)}",
            controller,
            triggered=controller.apply_delay,
        )
        self.menu.addAction(delay_action)

        if not controller.args.hide_cancel:
            self.menu.addAction(Action(
                FluentIcon.CLOSE,
                "取消关机计划",
                controller,
                triggered=controller.cancel_shutdown,
            ))

        self.setContextMenu(self.menu)

    def update_remaining(self, remaining: int) -> None:
        text = format_time(remaining)
        self.time_action.setText(f"剩余时间：{text}")
        self.setToolTip(f"{APP_NAME}：{text}后自动关机")


# ==================================================================
# MainWindow（不可见控制器宿主）
# ==================================================================
class MainWindow(QWidget):
    """
    应用主控制器。

    本窗口不显示任何内容，仅作为 MessageBox / Tray / Server / Timer 的宿主。
    启动后立即以置顶方式显示 MessageBox。
    """

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self.remaining: int = args.countdown
        self._reminder_fired: bool = False

        self._setup_window()
        self._setup_theme()
        self._setup_message_box()
        self._setup_tray()
        self._setup_server()
        self._setup_timer()

        # 启动即显示置顶弹窗
        self.show_reminder()

    # ---------- 初始化 ----------
    def _setup_window(self) -> None:
        """设置宿主窗口属性——仅用于承载图标 / 作为 parent，不显示。"""
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(get_resource_path(ICON_FILE_NAME)))
        self.setWindowFlags(Qt.Tool)
        self.resize(1, 1)
        # 注意：不要调用 show() / showFullScreen()

    def _setup_theme(self) -> None:
        setTheme(Theme.AUTO)
        if sys.platform in ("win32", "darwin"):
            setThemeColor(getSystemAccentColor(), save=False)

    def _setup_message_box(self) -> None:
        self.message_box = ShutdownMessageBox(self.args, parent=self)

        # 让弹窗独立置顶；根据 --show-in-taskbar 决定是否隐藏任务栏图标
        flags = self.message_box.windowFlags() | Qt.WindowStaysOnTopHint
        if not self.args.show_in_taskbar:
            flags |= Qt.Tool
        self.message_box.setWindowFlags(flags)

        self.message_box.accept_btn.clicked.connect(self.on_accept)
        self.message_box.shutdown_btn.clicked.connect(self.on_shutdown_now)
        self.message_box.delay_btn.clicked.connect(self.on_delay_clicked)
        self.message_box.cancel_btn.clicked.connect(self.cancel_shutdown)

    def _setup_tray(self) -> None:
        self.tray = TrayIcon(self)
        self.tray.update_remaining(self.remaining)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def _setup_server(self) -> None:
        self.server = QLocalServer(self)
        self.server.removeServer(SOCKET_NAME)   # 清理异常退出的残留
        self.server.listen(SOCKET_NAME)
        self.server.newConnection.connect(self._on_new_connection)

    def _setup_timer(self) -> None:
        if self.remaining <= 0:
            return
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1000)

    # ---------- 单实例：处理新连接 ----------
    def _on_new_connection(self) -> None:
        socket = self.server.nextPendingConnection()
        if not socket:
            return
        if socket.waitForReadyRead(NOTIFY_TIMEOUT_MS):
            data = socket.readAll().data().decode()
            if data == "SHOW":
                self.show_reminder()
                self._show_duplicate_hint()
            elif data == "QUIT":
                self.quit_app()
        socket.disconnectFromServer()

    def _show_duplicate_hint(self) -> None:
        InfoBar.warning(
            title="重复启动",
            content="已唤起原有的 Waity 实例。",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.TOP,
            duration=5000,
            parent=self.message_box,
        )

    # ---------- 托盘交互 ----------
    def on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_reminder()

    # ---------- 倒计时 ----------
    def _tick(self) -> None:
        self.remaining -= 1

        # 使用标记位而非相等判断，避免边界竞态
        if not self._reminder_fired and self.remaining <= self.args.reminder:
            self._reminder_fired = True
            self.show_reminder()

        if self.remaining > 0:
            self._refresh_ui()
        else:
            self.timer.stop()
            self.perform_shutdown()

    def _refresh_ui(self) -> None:
        self.message_box.update_content(self.remaining)
        self.tray.update_remaining(self.remaining)

    # ---------- 显示 / 隐藏 ----------
    def show_reminder(self) -> None:
        """显示并置顶提醒对话框。"""
        self.message_box.show()
        self.message_box.raise_()
        self.message_box.activateWindow()

    def _close_message_box(self) -> None:
        """关闭提醒对话框（等待关闭动画完成后再做后续处理）。"""
        if self.message_box.isVisible():
            self.message_box.close()

    # ---------- 按钮 / 菜单动作 ----------
    def on_accept(self) -> None:
        """已阅：只关对话框，不改变倒计时。"""
        self._close_message_box()

    def on_shutdown_now(self) -> None:
        """立即关机（保留短暂缓冲，避免误触）。"""
        if hasattr(self, "timer") and self.timer.isActive():
            self.timer.stop()
        self.perform_shutdown(delay=IMMEDIATE_SHUTDOWN_DELAY)

    def on_delay_clicked(self) -> None:
        """对话框上的「延迟」按钮：延迟 + 关闭对话框。"""
        self.apply_delay()
        self._close_message_box()

    def apply_delay(self) -> None:
        """延长倒计时 delay 秒（不改变对话框可见性）。"""
        if hasattr(self, "timer") and self.timer.isActive():
            self.remaining += self.args.delay
        else:
            self.remaining = self.args.delay
            self.timer = QTimer(self)
            self.timer.timeout.connect(self._tick)
            self.timer.start(1000)

        # 延迟后重新评估是否允许再次触发提醒
        self._reminder_fired = self.remaining <= self.args.reminder
        self._refresh_ui()

    # ---------- 关机 / 取消 ----------
    def perform_shutdown(self, delay: int = 0) -> None:
        run_shutdown(force=self.args.force, delay=delay)

    def cancel_shutdown(self) -> None:
        """取消关机：撤销系统关机命令并退出应用。"""
        cancel_system_shutdown()
        self._close_message_box()
        QTimer.singleShot(DIALOG_CLOSE_DELAY_MS, self.quit_app)

    def quit_app(self) -> None:
        """干净退出：停定时器、隐藏托盘、关闭 server、释放锁、退出 QApplication。"""
        if hasattr(self, "timer") and self.timer.isActive():
            self.timer.stop()
        if hasattr(self, "tray"):
            self.tray.hide()
        if hasattr(self, "server"):
            self.server.close()
        if hasattr(self, "_single_instance"):
            self._single_instance.release()
        QApplication.quit()


# ==================================================================
# 入口
# ==================================================================
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--countdown", type=int, default=60,
                        help="倒计时时长（秒），默认 60 秒")
    parser.add_argument("--delay", type=int, default=180,
                        help="延迟选项时长（秒），默认 180 秒（3 分钟）")
    parser.add_argument("--reminder", type=int, default=60,
                        help="关机前再次提醒的时长（秒），默认 60 秒")
    parser.add_argument("--show-in-taskbar", action="store_true",
                        help="在任务栏中显示弹窗图标（默认不显示）")
    parser.add_argument("--no-beep", action="store_true",
                        help="禁用点击空白处的提示音")
    parser.add_argument("--no-shake", action="store_true",
                        help="禁用点击空白处的抖动动画")
    parser.add_argument("--force", action="store_true", help="强制关机")
    parser.add_argument("--hide-cancel", action="store_true",
                        help="隐藏“取消关机计划”按钮")
    parser.add_argument("--overwrite", action="store_true",
                        help="如果已有实例在运行，则覆盖它")
    return parser.parse_args()


def _validate_args(args: argparse.Namespace) -> None:
    if args.countdown <= 0 or args.delay <= 0 or args.reminder <= 0:
        print("错误：--countdown, --delay, --reminder 参数必须为大于 0 的整数")
        sys.exit(1)


def _resolve_single_instance(args: argparse.Namespace, si: SingleInstance) -> None:
    """处理单实例逻辑；返回时保证当前进程已持有锁。"""
    if si.acquire():
        return

    if args.overwrite:
        si.notify_existing("QUIT")
        if si.wait_for_release():
            return
        print("无法覆盖已有实例，请手动退出后重试。")
        sys.exit(1)

    si.notify_existing("SHOW")
    print("有运行中的 Waity 实例，已唤起原实例。"
          "使用 --overwrite 参数可以覆盖原有实例。")
    sys.exit(0)


def main() -> None:
    args = parse_args()
    _validate_args(args)

    app = QApplication(sys.argv)

    si = SingleInstance()
    _resolve_single_instance(args, si)

    window = MainWindow(args)
    window._single_instance = si   # 供 quit_app 释放锁
    # 不需要 window.show()：MainWindow 只是不可见宿主
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
