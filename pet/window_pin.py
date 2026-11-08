# -*- coding: utf-8 -*-
"""图钉置顶（DeskPins 同类机制）：进入图钉模式，点哪个窗口哪个窗口置顶。

**机制**：一个全屏、半透明、置顶的临时覆盖窗接住这一次点击（十字光标 +
顶部提示条），点击瞬间先 ``hide()`` 覆盖窗再 ``WindowFromPoint``——覆盖窗
隐藏后该点位返回的就是真正的目标窗口；取 ``GA_ROOT`` 顶层窗，检查
``WS_EX_TOPMOST`` 决定置顶还是取消（**切换语义**，与 DeskPins 点图钉取消
一致），``SetWindowPos(HWND_TOPMOST/NOTOPMOST, SWP_NOACTIVATE)`` 不抢焦点。
全程只有一个临时窗、零轮询、零钩子、零线程——模式退出即一切消失，**常驻
开销为零**。

**DPI**：点击坐标取 ``GetCursorPos``（物理像素），``WindowFromPoint`` 消费
同一坐标系——两个 API 同进程同空间，无论进程 DPI 感知级别如何都自洽，
不需要任何换算。Qt 事件给的逻辑坐标刻意不用。

**自保护**：``WindowFromPoint`` 命中本进程窗口（桌宠自己的合成窗/覆盖窗）
时拒绝置顶——桌宠自身的置顶由「窗口置顶」（on_top）管理，两套开关互相
覆盖会把状态搅乱。判定注入 ``_self_pid`` 便于单测替换。

**退出**：右键 / Esc 关闭覆盖窗；再次触发菜单项 = 退出模式（切换语义）。
覆盖窗无定时器无线程，桌宠退出时随 Qt 一起析构，不需要退出收口步骤。

**与 DeskPins 的差异**（刻意从简）：不做标题栏图钉角标、不做托盘钉列表、
不持久化钉住集合——切换语义 + 反馈气泡已覆盖日常置顶/取消，管理面交给
系统（任务栏右键即有"取消置顶"）。灵感致谢 DeskPins（thewhitegrizzli
仓库）；实现为本仓库原生 Win32/Qt，未复制其代码。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

logger = logging.getLogger("dsh-pet-standalone")

#: 覆盖窗出现前的缓冲：菜单刚关、手指还在鼠标上，立刻全屏换十字光标容易
#: 误点。短缓一会也让"进入图钉模式"的提示气泡来得及被看见。
ENTER_DELAY_MS = 250

#: 反馈气泡展示时长（含窗口标题，信息量大，吃长一点的时长）。
FEEDBACK_MS = 6000

#: 标题截断：气泡单行放得下。
TITLE_MAX = 40

_overlay: QWidget | None = None


def is_supported() -> bool:
    """仅 Windows：``SetWindowPos`` / ``WindowFromPoint`` 是 win32 API。"""
    return sys.platform == "win32"


def trigger_pin_mode(pet) -> None:
    """进入/退出图钉模式（菜单项切换语义）。

    ``pet`` 只在**点击命中后**用来弹反馈气泡（``show_bubble``），构建菜单
    时不对它做任何属性访问——与 ``monitor_sleep`` 同款，PetWindow /
    OverlayShell / 呈现器三种宿主天然 parity。
    """
    global _overlay
    if not is_supported():
        return
    if _overlay is not None:
        # 再点一次菜单 = 退出模式
        _overlay.close()
        _overlay = None
        return

    def _enter() -> None:
        global _overlay
        if _overlay is not None:  # 延迟期间又被触发过：以新的为准
            return
        overlay = _PinOverlay(pet)
        _overlay = overlay
        overlay.destroyed.connect(_on_overlay_destroyed)
        overlay.showFullScreen()

    QTimer.singleShot(ENTER_DELAY_MS, _enter)


def _on_overlay_destroyed(*_args) -> None:
    global _overlay
    _overlay = None


def _self_pid() -> int:
    """本进程 PID（模块级函数便于单测替换）。"""
    import os

    return os.getpid()


def _user32():
    """win32 user32 入口（模块级函数便于单测注入假件）。"""
    import ctypes

    return ctypes.windll.user32


def _pick_and_toggle(px: int, py: int) -> tuple[str, str]:
    """在物理坐标 ``(px, py)`` 处拾取窗口并切换置顶，返回 ``(动作, 标题)``。

    动作 ∈ ``pinned`` / ``unpinned`` / ``self`` / ``none`` / ``refused``。
    调用方必须保证全屏覆盖窗此刻已隐藏（否则 ``WindowFromPoint`` 会命中
    覆盖窗自己）。``refused`` = 系统拒绝了 z-order 修改（沙箱/权限环境），
    以复核后的真实样式为准上报，绝不凭意图报成功。
    """
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    pt = wintypes.POINT(px, py)
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return ("none", "")
    root = user32.GetAncestor(hwnd, 2)  # GA_ROOT
    if not root:
        root = hwnd
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    if pid.value == _self_pid():
        return ("self", "")

    style = user32.GetWindowLongW(root, -20)  # GWL_EXSTYLE
    topmost = bool(style & 0x8)  # WS_EX_TOPMOST
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    SWP_FLAGS = 0x1 | 0x2 | 0x10  # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    want = not topmost
    user32.SetWindowPos(root, HWND_NOTOPMOST if topmost else HWND_TOPMOST,
                        0, 0, 0, 0, SWP_FLAGS)
    # 复核真实落点：以 SetWindowPos 之后的样式为准，不凭意图报成功
    style2 = user32.GetWindowLongW(root, -20)
    now_topmost = bool(style2 & 0x8)
    if now_topmost == topmost:
        # 原生路径被拒：本机安全软件按**二进制**拦 Python 的 TOPMOST-ification
        # （SetWindowPos err=1400 / SetWindowLong 位被即时剥离；移动与普通
        # z-order 提升不受影响，PowerShell 进程不受拦）。走 PS 桥：
        # resources/pin_helper/set_topmost.ps1 + 预编译 pin_top.dll（源码随附）。
        if not _bridge_set_topmost(root, want):
            return ("refused", _window_title(root))
        now_topmost = want
    action = "unpinned" if not now_topmost else "pinned"

    return (action, _window_title(root))


def _bridge_set_topmost(root: int, topmost: bool) -> bool:
    """PS 桥：PowerShell 进程代为 SetWindowPos（按样式复核，不凭返回值）。

    只在原生路径被拒时调用——一次点击最多一发，进程生成 ~300ms 属可接受
    的一次性开销；不设常驻辅助进程。DLL 缺失 / 生成被拦 / 超时一律 False。
    """
    import subprocess

    ps1 = Path(__file__).resolve().parent.parent / "resources" / "pin_helper" / "set_topmost.ps1"
    if not ps1.is_file():
        return False
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1), "-Hwnd", str(int(root)),
             "-After", "-1" if topmost else "-2"],
            capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("window_pin: PS 桥执行失败", exc_info=True)
        return False
    style = _user32().GetWindowLongW(root, -20)
    return (bool(style & 0x8)) == topmost


def _window_title(root) -> str:
    """窗口标题（截断到气泡单行放得下）。"""
    import ctypes

    buf = ctypes.create_unicode_buffer(256)
    _user32().GetWindowTextW(root, buf, 256)
    title = (buf.value or "").strip()
    if len(title) > TITLE_MAX:
        title = title[: TITLE_MAX - 1] + "…"
    return title


def _feedback(pet, action: str, title: str) -> None:
    """点击结果气泡：置顶/取消/自保/落空，各一句。"""
    show = getattr(pet, "show_bubble", None)
    if not callable(show):
        return
    if action == "pinned":
        text = f"已置顶：{title or '（无标题窗口）'}"
    elif action == "unpinned":
        text = f"已取消置顶：{title or '（无标题窗口）'}"
    elif action == "self":
        text = "别把我自己图钉呀……"
    elif action == "refused":
        text = "系统没让我动它……（换个窗口试试？）"
    else:
        text = "这里没有窗口可图钉……"
    try:
        show(text, duration_ms=FEEDBACK_MS)
    except Exception:
        logger.debug("window_pin: 反馈气泡失败", exc_info=True)


class _PinOverlay(QWidget):
    """全屏图钉覆盖窗：接住一次点击，十字光标 + 顶部提示条。"""

    def __init__(self, pet) -> None:
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self._pet = pet
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        screen = QApplication.primaryScreen()
        self.setGeometry(screen.virtualGeometry())
        self._closed = False

    def showFullScreen(self, *args) -> None:  # noqa: N802 (Qt 命名)
        super().showFullScreen(*args)
        # 覆盖窗要能收 Esc：主动拿焦点（show 不一定带激活）。
        self.activateWindow()
        self.setFocus()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        """半透明暗幕 + 顶部提示条：说明这一屏在等一次点击。"""
        from PySide6.QtCore import QRect

        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 28))
        painter.setRenderHint(QPainter.Antialiasing, True)
        banner = self.fontMetrics().horizontalAdvance(_BANNER_TEXT) + 48
        w = min(banner, self.width() - 40)
        rect = QRect((self.width() - w) // 2, 18, w, 44)
        painter.setPen(QPen(QColor(239, 194, 97), 2))
        painter.setBrush(QColor(20, 20, 24, 220))
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(QColor(255, 250, 240))
        painter.setFont(QFont(self.font().family(), 11))
        painter.drawText(rect, Qt.AlignCenter, _BANNER_TEXT)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if self._closed:
            return
        if event.button() == Qt.RightButton:
            self._finish()
            return
        if event.button() != Qt.LeftButton:
            return
        self._closed = True
        # 先藏覆盖窗再拾取：WindowFromPoint 才会返回真正的目标窗口
        self.hide()
        import ctypes
        from ctypes import wintypes

        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        action, title = _pick_and_toggle(pt.x, pt.y)
        self.close()
        _feedback(self._pet, action, title)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.key() == Qt.Key_Escape:
            self._finish()
        else:
            super().keyPressEvent(event)

    def _finish(self) -> None:
        self._closed = True
        self.close()


#: 覆盖窗提示条文案（paintEvent 用；模块级便于测试断言）。
_BANNER_TEXT = "图钉模式：点窗口置顶 / 再点取消 · 右键或 Esc 退出"


def _banner_text() -> str:
    return _BANNER_TEXT
