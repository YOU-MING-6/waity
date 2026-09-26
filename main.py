"""
==============================================================================
 ShutdownTool — 基于 PySide6 + QFluentWidgets 的定时关机提示工具
==============================================================================

【这个程序是做什么的？】

    启动后，屏幕中央会弹出一个窗口，提示“计算机将在 XX 后自动关闭”，
    同时右下角托盘会出现一个小图标。用户可以：

        · 点“已阅”         → 暂时关闭窗口，倒计时继续
        · 点“延迟 1 分钟”  → 倒计时 +60 秒，窗口不关闭
        · 点“立即关机”     → 关机（可留一点缓冲时间）
        · 点“取消关机计划” → 撤销关机，退出程序
        · 单击托盘图标     → 重新显示窗口

【代码结构】

    第 1 部分  Config            — 所有可调参数集中在此
    第 2 部分  Utils             — 通用工具函数
    第 3 部分  SingleInstance    — 保证只运行一个实例
    第 4 部分  ShutdownMessageBox— 弹窗 UI
    第 5 部分  TrayIcon          — 系统托盘
    第 6 部分  MainWindow        — 主控制器
    第 7 部分  main()            — 程序入口
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
    setTheme, setThemeColor, isDarkTheme,
)
from qframelesswindow.utils import getSystemAccentColor


# ══════════════════════════════════════════════════════════════════════════════
# 第 1 部分：Config —— 所有可调参数集中在这里
# ══════════════════════════════════════════════════════════════════════════════

# ---------- 应用信息 ----------
APP_ID = "shutdowntool"
APP_NAME = "shutdowntool"
APP_DESCRIPTION = "定时关机提示工具"

ICON_FILE = "icon.png"

SOCKET_NAME = f"{APP_ID}_socket"
LOCK_FILE = f"{APP_ID}.lock"

# ---------- 尺寸与时间 ----------
WIDTH = 600
TICK_MS = 1000
CLOSE_DELAY_MS = 500
SHUTDOWN_BUFFER_S = 0
DELAY_S = 60
NOTIFY_TIMEOUT_MS = 500
LOCK_TIMEOUT_MS = 100

# ---------- 窗口阴影 ----------
SHADOW_MARGIN = 24
SHADOW_BLUR = 24
SHADOW_OFFSET_Y = 4
SHADOW_COLOR = QColor(0, 0, 0, 90)

# ---------- 视觉细节 ----------
RADIUS = 8
INNER_RADIUS = RADIUS - 1


# ══════════════════════════════════════════════════════════════════════════════
# 第 2 部分：Utils —— 通用工具函数
# ══════════════════════════════════════════════════════════════════════════════

def resource_path(name: str) -> str:
    """
    返回资源文件的绝对路径，兼容开发环境与 PyInstaller 打包后的环境。

    打包后资源会被解压到 sys._MEIPASS 指向的临时目录；
    开发环境则直接使用脚本所在目录。
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def format_time(seconds: int) -> str:
    """
    把秒数格式化成易读的字符串。
    """
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m} 分钟" if s == 0 else f"{m} 分 {s} 秒"
    return f"{seconds} 秒"


def shutdown_now(delay: int = 0) -> None:
    """
    异步调用系统关机命令。

    使用 QProcess.startDetached 而非 os.system，避免阻塞主线程。
    """
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/s", "/f", "/t", str(delay)])
    else:
        QProcess.startDetached("shutdown", ["-h", "-t", str(delay)])


def cancel_shutdown() -> None:
    """撤销已经计划好的系统关机（仅 Windows 有效）。"""
    if sys.platform == "win32":
        QProcess.startDetached("shutdown", ["/a"])


def center_on_screen(widget: QWidget) -> None:
    """把顶层窗口居中到当前屏幕的可用区域。"""
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    widget.adjustSize()
    widget.move(
        geo.x() + (geo.width() - widget.width()) // 2,
        geo.y() + (geo.height() - widget.height()) // 2,
    )


def play_startup_sound() -> None:
    """
    程序成功启动后播放 Windows 11 的 UAC 提示音。
    非 Windows 平台静默返回，播放失败也不抛异常（不影响主流程）。
    """
    if sys.platform != "win32":
        return
    try:
        import winsound
    except ImportError:
        return

    # 播放系统自带的声音文件
    media_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Media")
    for name in (
        "Windows User Account Control.wav",
        "Windows User Account Control (Windows 10).wav",
    ):
        wav = os.path.join(media_dir, name)
        if os.path.isfile(wav):
            try:
                winsound.PlaySound(
                    wav,
                    winsound.SND_FILENAME
                    | winsound.SND_ASYNC      # 异步播放，不阻塞主线程
                    | winsound.SND_NODEFAULT, # 找不到文件就别放默认音
                )
                return
            except RuntimeError:
                pass

    # 2) 回退：系统默认的“信息提示音”，听感接近 UAC
    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# 第 3 部分：SingleInstance —— 保证只有一个程序实例在运行
# ══════════════════════════════════════════════════════════════════════════════

class SingleInstance:
    """
    单实例控制器。

    两种机制配合：
        · QLockFile    — 保证同一时刻只有一个程序拿到锁
        · QLocalSocket — 让新启动的程序给已运行的程序发消息
    """

    def __init__(self) -> None:
        lock_path = os.path.join(
            QStandardPaths.writableLocation(QStandardPaths.TempLocation),
            LOCK_FILE,
        )
        self._lock = QLockFile(lock_path)

    def acquire(self) -> bool:
        """尝试获取锁；返回 False 表示已有实例在运行。"""
        return self._lock.tryLock(LOCK_TIMEOUT_MS)

    def release(self) -> None:
        """释放锁。"""
        self._lock.unlock()

    def notify_show(self) -> None:
        """向已运行的实例发送 "SHOW" 消息，让对方把窗口显示出来。"""
        sock = QLocalSocket()
        sock.connectToServer(SOCKET_NAME)
        if sock.waitForConnected(NOTIFY_TIMEOUT_MS):
            sock.write(b"SHOW")
            sock.waitForBytesWritten(NOTIFY_TIMEOUT_MS)
            sock.disconnectFromServer()


# ══════════════════════════════════════════════════════════════════════════════
# 第 4 部分：ShutdownMessageBox —— 弹窗 UI
# ══════════════════════════════════════════════════════════════════════════════
# 【视觉结构】
#
# ┌─────────────────────────────────────────────┐
# │  要关机吗？                                  │
# │  xxxxx；计算机将在…                          │ ← contentFrame（纯白）
# │  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░   │
# ├─────────────────────────────────────────────┤ ← 1px 分隔线
# │ [已阅][延迟 1 分钟][立即关机]   [取消关机计划] │
# │                                             │ ← buttonFrame（浅灰）
# └─────────────────────────────────────────────┘
# # ──────────────────────────────────────────────────────────────────────────────

class ShutdownMessageBox(QWidget):
    """关机提示对话框。"""

    def __init__(self, countdown: int) -> None:
        super().__init__()

        # 状态
        self.remaining = countdown
        self.total = countdown
        self._progress_anim = None
        self._drag_offset = None

        # 构建 UI
        self._setup_window()
        self._setup_content()
        self._setup_buttons()
        self._apply_style()
        self.update_content()

    # ──────────────────────────────────────────────────────────────────────
    # 窗口构建
    # ──────────────────────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        """创建最外层的窗口和圆角背景容器。"""
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        # 外层布局，留出阴影边距
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN
        )

        # 圆角容器
        self.container = QFrame(self)
        self.container.setObjectName("shutdownContainer")
        self.container.setAttribute(Qt.WA_StyledBackground, True)
        self.container.setFixedWidth(WIDTH)
        self._attach_shadow()
        outer.addWidget(self.container)

        # container 内部：上下两块
        main_layout = QVBoxLayout(self.container)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 内容区
        self.contentFrame = QFrame(self.container)
        self.contentFrame.setObjectName("contentFrame")
        self.contentFrame.setAttribute(Qt.WA_StyledBackground, True)
        main_layout.addWidget(self.contentFrame)

        # 按钮区
        self.buttonFrame = QFrame(self.container)
        self.buttonFrame.setObjectName("buttonFrame")
        self.buttonFrame.setAttribute(Qt.WA_StyledBackground, True)
        main_layout.addWidget(self.buttonFrame)

    def _attach_shadow(self) -> None:
        """给 container 添加柔和阴影。"""
        shadow = QGraphicsDropShadowEffect(self.container)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setOffset(0, SHADOW_OFFSET_Y)
        shadow.setColor(SHADOW_COLOR)
        self.container.setGraphicsEffect(shadow)

    def _apply_style(self) -> None:
        """根据当前主题应用配色样式。"""
        if isDarkTheme():
            container_bg = "#2B2B2B"
            container_border = "#3A3A3A"
            button_bg = "#1F1F1F"
        else:
            container_bg = "#FFFFFF"
            container_border = "#E5E5E5"
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

    # ──────────────────────────────────────────────────────────────────────
    # 内容与按钮
    # ──────────────────────────────────────────────────────────────────────

    def _setup_content(self) -> None:
        """构建上半部分：标题、描述、进度条。"""
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

    def _setup_buttons(self) -> None:
        """构建下半部分：四个操作按钮。"""
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
        row.addWidget(self.accept_btn)
        row.addWidget(self.delay_btn)
        row.addWidget(self.shutdown_btn)
        row.addStretch(1)
        row.addWidget(self.cancel_btn)

    # ──────────────────────────────────────────────────────────────────────
    # 内容与进度更新
    # ──────────────────────────────────────────────────────────────────────

    def update_content(
        self, remaining: int | None = None, total: int | None = None
    ) -> None:
        """刷新显示的剩余时间和进度条。"""
        if remaining is not None:
            self.remaining = remaining
        if total is not None:
            self.total = total

        self.contentLabel.setText(
            f"当前为放学时段；计算机将在 {format_time(self.remaining)}后自动关闭。"
        )
        self._animate_progress(self._target_progress())

    def _target_progress(self) -> int:
        """计算进度条目标百分比（0-100 的整数）。"""
        if self.total <= 0:
            return 0
        return max(0, min(100, round(self.remaining * 100 / self.total)))

    def _animate_progress(self, target: int) -> None:
        """让进度条在 TICK_MS 毫秒内平滑过渡到目标值。"""
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

    # ──────────────────────────────────────────────────────────────────────
    # 拖动窗口
    # ──────────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:
        """鼠标按下时记录光标相对窗口左上角的偏移。"""
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        """按住鼠标移动时，把窗口移动到新位置。"""
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        """松开鼠标结束拖动。"""
        self._drag_offset = None
        super().mouseReleaseEvent(event)


# ══════════════════════════════════════════════════════════════════════════════
# 第 5 部分：TrayIcon —— 系统托盘
# ══════════════════════════════════════════════════════════════════════════════

class TrayIcon(QSystemTrayIcon):
    """系统托盘图标。"""

    def __init__(self, controller: "MainWindow") -> None:
        super().__init__(parent=controller)
        self.setIcon(controller.windowIcon())

        menu = SystemTrayMenu(parent=controller)

        self._time_action = Action(FluentIcon.POWERBUTTON, "剩余时间：", controller)
        menu.addAction(self._time_action)

        menu.addSeparator()

        menu.addAction(Action(
            FluentIcon.HISTORY, "延迟 1 分钟",
            controller, triggered=controller.on_delay_clicked,
        ))
        menu.addAction(Action(
            FluentIcon.POWERBUTTON, "立即关机",
            controller, triggered=controller.on_shutdown_now,
        ))
        
        menu.addSeparator()

        menu.addAction(Action(
            FluentIcon.CLOSE, "取消关机计划",
            controller, triggered=controller.cancel_shutdown,
        ))

        self.setContextMenu(menu)

    def update_remaining(self, remaining: int) -> None:
        """更新托盘菜单里的剩余时间文字和悬停提示。"""
        text = format_time(remaining)
        self._time_action.setText(f"剩余时间：{text}")
        self.setToolTip(f"{text}后将自动关机")


# ══════════════════════════════════════════════════════════════════════════════
# 第 6 部分：MainWindow —— 主控制器
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QWidget):
    """整个程序的中枢：串联对话框、托盘、定时器、单实例。"""

    def __init__(self, countdown: int, single_instance: SingleInstance) -> None:
        super().__init__()

        # ---- 状态 ----
        self._si = single_instance
        self.remaining = countdown
        self.total = countdown
        self._centered = False

        # ---- 宿主窗口（不可见）----
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(resource_path(ICON_FILE)))
        self.setWindowFlags(Qt.Tool)
        self.resize(1, 1)

        # ---- 主题（只在启动时应用一次）----
        self._apply_theme()

        # ---- UI 组件 ----
        self._setup_message_box(countdown)
        self._setup_tray()
        self._setup_single_instance_server()
        self._setup_timer()

        # ---- 首次显示 ----
        self.show_reminder()

    # ──────────────────────────────────────────────────────────────────────
    # 主题
    # ──────────────────────────────────────────────────────────────────────

    def _apply_theme(self) -> None:
        """应用主题：根据启动时的系统状态选择深浅色。"""
        setTheme(Theme.AUTO)
        if sys.platform in ("win32", "darwin"):
            setThemeColor(getSystemAccentColor(), save=False)

    # ──────────────────────────────────────────────────────────────────────
    # 组件初始化
    # ──────────────────────────────────────────────────────────────────────

    def _setup_message_box(self, countdown: int) -> None:
        """创建对话框并连接按钮信号。"""
        self.message_box = ShutdownMessageBox(countdown)
        self.message_box.accept_btn.clicked.connect(self.on_accept)
        self.message_box.shutdown_btn.clicked.connect(self.on_shutdown_now)
        self.message_box.delay_btn.clicked.connect(self.on_delay_clicked)
        self.message_box.cancel_btn.clicked.connect(self.cancel_shutdown)

    def _setup_tray(self) -> None:
        """创建系统托盘。"""
        self.tray = TrayIcon(self)
        self.tray.update_remaining(self.remaining)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.show()

    def _setup_single_instance_server(self) -> None:
        """启动本地 socket 服务，接收其他实例的唤醒请求。"""
        self.server = QLocalServer(self)
        self.server.removeServer(SOCKET_NAME)
        self.server.listen(SOCKET_NAME)
        self.server.newConnection.connect(self._on_new_connection)

    def _setup_timer(self) -> None:
        """启动倒计时定时器。"""
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)

    # ──────────────────────────────────────────────────────────────────────
    # 显示窗口
    # ──────────────────────────────────────────────────────────────────────

    def show_reminder(self) -> None:
        """显示并置顶对话框。首次显示时会先居中。"""
        if not self._centered:
            center_on_screen(self.message_box)
            self._centered = True
        self.message_box.show()
        self.message_box.raise_()
        self.message_box.activateWindow()

    # ──────────────────────────────────────────────────────────────────────
    # 单实例消息处理
    # ──────────────────────────────────────────────────────────────────────

    def _on_new_connection(self) -> None:
        """有新的程序实例启动并尝试连接时触发。"""
        sock = self.server.nextPendingConnection()
        if not sock:
            return
        if sock.waitForReadyRead(NOTIFY_TIMEOUT_MS):
            if sock.readAll().data() == b"SHOW":
                self.show_reminder()
        sock.disconnectFromServer()

    # ──────────────────────────────────────────────────────────────────────
    # 托盘交互
    # ──────────────────────────────────────────────────────────────────────

    def on_tray_activated(self, reason) -> None:
        """托盘图标被点击时调用：单击或双击都重新显示窗口。"""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_reminder()

    # ──────────────────────────────────────────────────────────────────────
    # 倒计时逻辑
    # ──────────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        """每秒触发一次：剩余秒数减 1，刷新 UI 或执行关机。"""
        self.remaining -= 1
        if self.remaining > 0:
            self._refresh_ui()
        else:
            self.timer.stop()
            shutdown_now()

    def _refresh_ui(self) -> None:
        """把最新的剩余秒数同步到对话框和托盘。"""
        self.message_box.update_content(self.remaining, self.total)
        self.tray.update_remaining(self.remaining)

    # ──────────────────────────────────────────────────────────────────────
    # 按钮回调
    # ──────────────────────────────────────────────────────────────────────

    def on_accept(self) -> None:
        """“已阅”：只关掉窗口，倒计时继续。"""
        self.message_box.close()

    def on_delay_clicked(self) -> None:
        """“延迟 1 分钟”：剩余时间和总时间都 +DELAY_S，窗口保持显示。"""
        self.remaining += DELAY_S
        self.total += DELAY_S
        self._refresh_ui()

    def on_shutdown_now(self) -> None:
        """“立即关机”：停止倒计时，关机。"""
        self.timer.stop()
        shutdown_now(SHUTDOWN_BUFFER_S)

    def cancel_shutdown(self) -> None:
        """“取消关机计划”：撤销系统关机命令，关闭窗口，稍后退出。"""
        cancel_shutdown()
        self.message_box.close()
        QTimer.singleShot(CLOSE_DELAY_MS, self._quit)

    # ──────────────────────────────────────────────────────────────────────
    # 退出清理
    # ──────────────────────────────────────────────────────────────────────

    def _quit(self) -> None:
        """依次清理资源，最后退出应用。"""
        self.timer.stop()
        self.tray.hide()
        self.server.close()
        self._si.release()
        QApplication.quit()


# ══════════════════════════════════════════════════════════════════════════════
# 第 7 部分：main() —— 程序入口
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """程序主入口。"""

    # ---- 1. 解析命令行参数 ----
    parser = argparse.ArgumentParser(description=APP_DESCRIPTION)
    parser.add_argument(
        "--countdown",
        type=int,
        default=15,
        help="默认倒计时时长（秒）",
    )
    args = parser.parse_args()

    if args.countdown <= 0:
        print("错误：--countdown 必须为大于 0 的整数")
        sys.exit(1)

    # ---- 2. 创建 QApplication ----
    app = QApplication(sys.argv)

    # ---- 3. 单实例检查 ----
    si = SingleInstance()
    if not si.acquire():
        si.notify_show()
        sys.exit(0)

    # ---- 4. 创建主窗口 ----
    window = MainWindow(args.countdown, si)

    # ---- 5. 启动成功，播放提示音 ----
    play_startup_sound()

    # ---- 6. 进入事件循环 ----
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
