# -*- coding: utf-8 -*-
"""「息屏」单测：广播边界 / 手动防抖时序 / 锁屏自动熄屏状态机 / 菜单接线。

绝不在单测里真熄屏：win32 边界（``SendMessageTimeoutW``）在广播测试里喂假
``windll``，其余用例打桩 ``_send_monitor_off`` / 给控制器注入计数器。
时序用宽预算条件轮询（先例：``test_memory_trim._wait_until``），不赌固定
sleep——0ms QTimer 的每一拍都要过事件循环。
"""
from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMenu

from pet import monitor_sleep
from pet.context_menus import registry as registry_mod
from pet.context_menus import shared as shared_mod


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _wait_until(app, predicate, *, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(interval)
    app.processEvents()
    return predicate()


class _Pet:
    """菜单建造器的最小宿主（先例：``test_memory_trim._Pet``）。"""

    def __init__(self, values=None):
        self.cfg = dict(values or {})
        self.bubbles: list[str] = []
        self.idles = ["待机"]
        self.turns: list[str] = []
        self.moves: list[str] = []
        self.clicks: list[str] = []
        self.acts: list[str] = []
        self.playback_speed = 1.0
        self.scale = 1.0
        self.drag_physics = False
        self.no_move = False

    def show_bubble(self, text, duration_ms=3200, **_kwargs):
        self.bubbles.append(text)

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


# ---------------------------------------------------------------- 平台分派
def test_is_supported_only_on_windows():
    assert monitor_sleep.is_supported("win32") is True
    assert monitor_sleep.is_supported("darwin") is False
    assert monitor_sleep.is_supported("linux") is False
    assert monitor_sleep.is_supported() == (sys.platform == "win32")


# ---------------------------------------------------------------- win32 广播边界
class _FakeUser32:
    def __init__(self, ret):
        self.ret = ret
        self.calls = []

    def SendMessageTimeoutW(self, *args):
        self.calls.append(args)
        return self.ret


def test_send_monitor_off_broadcasts_sc_monitorpower(monkeypatch):
    fake = _FakeUser32(1)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(monitor_sleep.ctypes, "windll",
                        SimpleNamespace(user32=fake))

    assert monitor_sleep._send_monitor_off() is True

    assert len(fake.calls) == 1
    hwnd, msg, wparam, lparam, flags, timeout, _result = fake.calls[0]
    assert hwnd == monitor_sleep.HWND_BROADCAST
    assert msg == monitor_sleep.WM_SYSCOMMAND
    assert wparam == monitor_sleep.SC_MONITORPOWER
    assert lparam == monitor_sleep.MONITOR_OFF
    assert flags == monitor_sleep.SMTO_ABORTIFHUNG
    assert timeout == monitor_sleep.SEND_TIMEOUT_MS


def test_send_monitor_off_reports_broadcast_failure(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(monitor_sleep.ctypes, "windll",
                        SimpleNamespace(user32=_FakeUser32(0)))
    assert monitor_sleep._send_monitor_off() is False


def test_send_monitor_off_is_noop_off_windows(monkeypatch):
    fake = _FakeUser32(1)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(monitor_sleep.ctypes, "windll",
                        SimpleNamespace(user32=fake))
    assert monitor_sleep._send_monitor_off() is False
    assert fake.calls == []


# ---------------------------------------------------------------- 手动入口
def test_trigger_monitor_sleep_settles_then_sends_with_guard(app, monkeypatch):
    sends: list[float] = []
    monkeypatch.setattr(monitor_sleep, "_send_monitor_off",
                        lambda: sends.append(time.monotonic()) or True)
    monkeypatch.setattr(monitor_sleep, "CLICK_SETTLE_MS", 0)
    monkeypatch.setattr(monitor_sleep, "GUARD_RESEND_DELAYS_MS", (0, 0))

    monitor_sleep.trigger_monitor_sleep()

    assert sends == []  # 先等菜单收尾，不立刻熄
    assert _wait_until(app, lambda: len(sends) >= 3)
    assert len(sends) == 3  # 初次熄屏 + 两次宽限期补发，之后不再发
    baseline = len(sends)
    assert not _wait_until(app, lambda: len(sends) > baseline, timeout=0.5)


def test_trigger_monitor_sleep_is_noop_off_windows(app, monkeypatch):
    sends: list[int] = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(monitor_sleep, "_send_monitor_off",
                        lambda: sends.append(1))
    monitor_sleep.trigger_monitor_sleep()
    assert not _wait_until(app, lambda: bool(sends), timeout=0.5)
    assert sends == []


def test_menu_action_triggers_the_delayed_sleep(app, monkeypatch):
    """菜单条目点下去 → 同一条 trigger 时序（settle → 熄 → 宽限补发）。"""
    sends: list[int] = []
    monkeypatch.setattr(monitor_sleep, "_send_monitor_off",
                        lambda: sends.append(1) or True)
    monkeypatch.setattr(monitor_sleep, "CLICK_SETTLE_MS", 0)
    monkeypatch.setattr(monitor_sleep, "GUARD_RESEND_DELAYS_MS", (0, 0))
    menu = QMenu()
    action = shared_mod.add_monitor_sleep(menu, _Pet())
    assert action is not None
    action.trigger()  # 菜单不可见：close_on_trigger 直接派发回调
    assert _wait_until(app, lambda: len(sends) >= 3)
    menu.close()


# ---------------------------------------------------------------- 锁屏状态机
def _make_controller():
    sends: list[int] = []
    controller = monitor_sleep.LockMonitorSleep(
        delay_ms=0, keep_interval_ms=0, send=lambda: sends.append(1))
    return controller, sends


def test_lock_countdown_fires_once_then_keeps_off_until_unlock(app):
    controller, sends = _make_controller()
    assert not controller.countdown_active

    controller.on_suspend_change(True, "session_lock")
    assert controller.locked is True
    assert controller.countdown_active
    assert _wait_until(app, lambda: len(sends) >= 1)
    assert controller.keep_off_active  # 熄屏后保黑循环在补发
    assert _wait_until(app, lambda: len(sends) >= 2)

    controller.on_suspend_change(False, "session_unlock")
    assert controller.locked is False
    assert not controller.countdown_active
    assert not controller.keep_off_active
    baseline = len(sends)
    assert not _wait_until(app, lambda: len(sends) > baseline, timeout=0.5)


def test_power_suspend_freezes_and_resume_restarts_while_still_locked(app):
    controller, sends = _make_controller()
    controller.on_suspend_change(True, "session_lock")
    assert _wait_until(app, lambda: len(sends) >= 1)

    controller.on_suspend_change(True, "power_suspend")
    assert not controller.countdown_active
    assert not controller.keep_off_active
    assert controller.locked is True  # 挂起不改锁态
    baseline = len(sends)
    assert not _wait_until(app, lambda: len(sends) > baseline, timeout=0.5)

    controller.on_suspend_change(False, "power_resume")
    assert controller.countdown_active  # 仍锁着：重开倒计时，不直接按黑
    assert _wait_until(app, lambda: len(sends) > baseline)


def test_resume_after_unlock_does_not_restart(app):
    controller, sends = _make_controller()
    controller.on_suspend_change(True, "session_lock")
    controller.on_suspend_change(False, "session_unlock")
    assert not controller.locked
    controller.on_suspend_change(True, "power_suspend")
    controller.on_suspend_change(False, "power_resume")
    assert not controller.countdown_active  # 已解锁：恢复不重开
    assert not controller.keep_off_active
    assert sends == []


def test_user_input_resume_restarts_only_while_locked(app):
    controller, _sends = _make_controller()
    controller.on_suspend_change(True, "session_lock")
    controller.on_suspend_change(True, "power_suspend")
    controller.on_suspend_change(False, "user-input")
    assert controller.countdown_active  # 输入自愈兜底：仍锁着重开倒计时

    controller.on_suspend_change(False, "session_unlock")
    assert not controller.countdown_active


# ---------------------------------------------------------------- 菜单接线
def test_registry_registers_monitor_sleep_with_a_windows_gate(monkeypatch):
    assert "monitor_sleep" in registry_mod.MENU_ACTIONS.ids
    assert registry_mod.ACTION_LABELS["monitor_sleep"] == "息屏"
    pet = _Pet()

    monkeypatch.setattr(sys, "platform", "win32")
    assert "monitor_sleep" in registry_mod.MENU_ACTIONS.available_ids(pet)

    monkeypatch.setattr(sys, "platform", "darwin")
    assert "monitor_sleep" not in registry_mod.MENU_ACTIONS.available_ids(pet)


def test_shared_builder_offers_monitor_sleep_on_windows_only(app, monkeypatch):
    pet = _Pet()

    monkeypatch.setattr(sys, "platform", "win32")
    menu = QMenu()
    action = shared_mod.add_monitor_sleep(menu, pet)
    windows_labels = [item.text() for item in menu.actions()]
    menu.close()

    monkeypatch.setattr(sys, "platform", "darwin")
    other_menu = QMenu()
    other_action = shared_mod.add_monitor_sleep(other_menu, pet)
    other_labels = [item.text() for item in other_menu.actions()]
    other_menu.close()

    assert action is not None and "息屏" in windows_labels
    assert other_action is None and other_labels == []
    app.processEvents()
