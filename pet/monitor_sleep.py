# -*- coding: utf-8 -*-
"""显示器息屏（Windows 专属）：菜单手动熄屏 + 锁屏 3 分钟自动熄屏。

机制与同类工具（t-mart/monoff、nircmd ``monitor off``）同源：向全部顶层窗口
广播 ``WM_SYSCOMMAND / SC_MONITORPOWER(2)``（``HWND_BROADCAST``），Windows 把
显示器切到关断；**键鼠一动由系统原生唤醒**——本模块不监听输入、不装钩子。
桌宠只发令，唤醒归系统，两侧语义对称，没有需要自己保活的状态。

两个入口
--------
1. 「息屏」菜单项（``shared.add_monitor_sleep`` → :func:`trigger_monitor_sleep`）：
   先等 ``CLICK_SETTLE_MS``（600ms）让点菜单的手离开鼠标，再熄屏；随后
   ``GUARD_RESEND_DELAYS_MS``（1s / 2s）宽限期内各补发一次——鼠标传感器的
   微抖会把刚熄的屏立刻点亮（monoff 的已知坑），补发把宽限期内的误唤醒按
   回去；宽限期后一切键鼠输入恢复正常唤醒。**刻意不用** ``BlockInput`` 或
   低级键鼠钩子去"吞输入"：前者非管理员进程直接失败，后者为 2 秒的窗口
   引入整层钩子生命周期，收益配不上风险。

2. 锁屏自动熄屏（``OverlayShell._on_suspend_changed`` → :class:`LockMonitorSleep`）：
   会话锁上（``session_watcher`` 已探测的 ``WTS_SESSION_LOCK``）后
   ``LOCK_OFF_DELAY_MS``（3 分钟）熄屏；此后每 ``LOCK_KEEP_OFF_INTERVAL_MS``
   （60s）补发一次——锁屏界面的系统通知/唤醒源会把屏重新点亮，补发保证
   "仍锁着就保持黑"。解锁即停（键鼠唤醒由系统接管）。挂起的边角：锁屏期间
   睡眠又唤醒时 ``power_resume`` 先到、锁事件不会重发——控制器记住"会话仍
   锁着"，恢复后**重开倒计时**而非直接按黑（恢复瞬间用户可能正要亮屏登录）。

   只挂在 overlay 壳：legacy 捕获窗拓扑连锁屏探测都没有（``AppShell`` 的
   SessionWatcher 不接 ``on_suspend_change``、不注册 WTS 通知），沿既有
   降级口径，本功能不为它新增探测面。

win32 边界全部收在 :func:`_send_monitor_off`（测试打桩它即替换系统调用，
**绝不在单测里真熄屏**）。广播用 ``SendMessageTimeoutW(SMTO_ABORTIFHUNG)``
而不是 ``SendMessageW``：HWND_BROADCAST + SendMessage 会逐个同步等每个
顶层窗口应答，一个挂死的窗口就能把 GUI 线程吊死；超时版本 200ms 即返回。

线程：只在 GUI 线程创建与触发（QTimer 与菜单回调都在 GUI 线程）。
"""
from __future__ import annotations

import ctypes
import logging
import sys

from PySide6.QtCore import QObject, QTimer

log = logging.getLogger(__name__)

# winuser.h：WM_SYSCOMMAND(0x0112) 的 wParam=SC_MONITORPOWER(0xF170)，lParam：
# 1=低功耗 2=关断。桌宠只发 2（关断）；点亮交给键鼠输入与系统。
HWND_BROADCAST = 0xFFFF
WM_SYSCOMMAND = 0x0112
SC_MONITORPOWER = 0xF170
MONITOR_OFF = 2

#: HWND_BROADCAST 的同步等待上限：挂死窗口不拖 GUI 线程（见模块 docstring）
SEND_TIMEOUT_MS = 200
SMTO_ABORTIFHUNG = 0x0002

#: 点击菜单 → 熄屏的等待：菜单收尾动画 + 手离开鼠标
CLICK_SETTLE_MS = 600
#: 熄屏后的防抖补发时刻（距熄屏）：宽限期内误唤醒一律按回（monoff 先例）
GUARD_RESEND_DELAYS_MS = (1000, 2000)

#: 锁屏 → 自动熄屏的倒计时（产品口径：锁屏 3 分钟）
LOCK_OFF_DELAY_MS = 3 * 60 * 1000
#: 熄屏后的保黑补发间隔：锁屏界面被通知/唤醒源点亮时再按回去
LOCK_KEEP_OFF_INTERVAL_MS = 60 * 1000


def is_supported(platform: str | None = None) -> bool:
    """平台分派（菜单可用性 / 触发层共用同一判定，先例：``memory_trim``）。"""
    return (sys.platform if platform is None else platform) == "win32"


def _send_monitor_off() -> bool:
    """win32 边界：广播 ``SC_MONITORPOWER(2)`` 关断显示器；返回是否送达。"""
    if not is_supported():
        return False
    # lpdwResult 按 DWORD_PTR（指针宽度）给缓冲：SendMessageTimeoutW 侧按
    # 平台字宽回写，给 32 位 c_ulong 会在 64 位系统上越界写邻接内存。
    result = ctypes.c_size_t()
    try:
        sent = int(ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, MONITOR_OFF,
            SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS, ctypes.byref(result)))
    except Exception:
        log.debug("息屏广播失败", exc_info=True)
        return False
    return sent != 0


def trigger_monitor_sleep() -> None:
    """「息屏」入口：settle → 熄屏 → 宽限期补发。

    时刻读模块常量（不是默认参数绑定）：测试 monkeypatch 常量即可把整条
    时序缩到 0ms，不用赌真实墙钟。非 Windows 上 no-op。
    """
    if not is_supported():
        return

    def _sleep_with_guard() -> None:
        _send_monitor_off()
        for delay in GUARD_RESEND_DELAYS_MS:
            QTimer.singleShot(delay, _send_monitor_off)

    QTimer.singleShot(CLICK_SETTLE_MS, _sleep_with_guard)


class LockMonitorSleep(QObject):
    """锁屏 N 分钟自动熄屏（只观测会话事件；输入唤醒归系统）。

    事件面 :meth:`on_suspend_change` 收 ``SessionWatcher.on_suspend_change``
    的转发（``active`` 与 reason 见 ``session_watcher._SUSPEND_MESSAGES``）。
    发送边界经构造器注入（默认真机广播；单测注计数器，绝不真熄屏）。
    """

    def __init__(self, parent=None, *, delay_ms: int | None = None,
                 keep_interval_ms: int | None = None,
                 send=None) -> None:
        super().__init__(parent)
        self._delay_ms = LOCK_OFF_DELAY_MS if delay_ms is None else int(delay_ms)
        self._keep_ms = (LOCK_KEEP_OFF_INTERVAL_MS if keep_interval_ms is None
                         else int(keep_interval_ms))
        self._send = _send_monitor_off if send is None else send
        # 会话是否仍锁着：挂起/恢复不重发锁事件，靠本位跨过 S3 仍记得要补
        self._locked = False
        self._countdown = QTimer(self)
        self._countdown.setSingleShot(True)
        self._countdown.timeout.connect(self._fire)
        self._keep_off = QTimer(self)
        self._keep_off.setInterval(self._keep_ms)
        self._keep_off.timeout.connect(self._send_keep_off)

    # ------------------------------------------------------------ 事件入口
    def on_suspend_change(self, active: bool, reason: str = "") -> None:
        """会话锁/解锁/挂起/恢复状态变化（异常由调用方隔离，见壳侧接线）。"""
        reason = str(reason or "")
        if active and reason == "session_lock":
            self._locked = True
            self._countdown.start(self._delay_ms)
            return
        if reason == "session_unlock":
            self._locked = False
        if reason == "power_suspend":
            # 挂起时冻结计时（恢复瞬间的超时不该立刻把屏按黑），唤醒侧重开
            self._countdown.stop()
            self._keep_off.stop()
            return
        if active:
            return
        # active=False：解锁 / 挂起恢复 / 用户输入自愈——先全停，仍锁着才重开
        self._countdown.stop()
        self._keep_off.stop()
        if self._locked and reason != "session_unlock":
            self._countdown.start(self._delay_ms)

    # ------------------------------------------------------------ 触发
    def _fire(self) -> None:
        try:
            self._send()
        except Exception:
            log.exception("锁屏自动熄屏发送失败")
        self._keep_off.start(self._keep_ms)

    def _send_keep_off(self) -> None:
        try:
            self._send()
        except Exception:
            log.exception("锁屏保黑补发失败")

    # ------------------------------------------------------------ 观测面（测试/诊断）
    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def countdown_active(self) -> bool:
        return self._countdown.isActive()

    @property
    def keep_off_active(self) -> bool:
        return self._keep_off.isActive()
