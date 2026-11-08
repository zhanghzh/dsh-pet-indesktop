# -*- coding: utf-8 -*-
"""显示器息屏（Windows 专属）：右键菜单手动熄屏。

机制与同类工具（t-mart/monoff、nircmd ``monitor off``）同源：向全部顶层窗口
广播 ``WM_SYSCOMMAND / SC_MONITORPOWER(2)``（``HWND_BROADCAST``），Windows 把
显示器切到关断；**键鼠一动由系统原生唤醒**——本模块不监听输入、不装钩子。
桌宠只发令，唤醒归系统，两侧语义对称，没有需要自己保活的状态。

入口：「息屏」菜单项（``shared.add_monitor_sleep`` → :func:`trigger_monitor_sleep`）：
先等 ``CLICK_SETTLE_MS``（600ms）让点菜单的手离开鼠标，再熄屏；随后
``GUARD_RESEND_DELAYS_MS``（1s / 2s）宽限期内各补发一次——鼠标传感器的
微抖会把刚熄的屏立刻点亮（monoff 的已知坑），补发把宽限期内的误唤醒按
回去；宽限期后一切键鼠输入恢复正常唤醒。**刻意不用** ``BlockInput`` 或
低级键鼠钩子去"吞输入"：前者非管理员进程直接失败，后者为 2 秒的窗口
引入整层钩子生命周期，收益配不上风险。

**刻意不做锁屏自动熄屏**（2026-11-05 产品决策）：被控机场景（UU 远程等）
依赖显示管线持续存在，DPMS 断电会让取流断连；且"锁着就保持黑"的补发
策略与远程唤醒互相打架。自动熄屏需求交给 Windows 电源计划（电源计划里
"关闭显示"计时器只在会话空闲时触发，且不广播 SC_MONITORPOWER）。

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

from PySide6.QtCore import QTimer

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
