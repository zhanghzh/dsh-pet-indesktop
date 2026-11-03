# -*- coding: utf-8 -*-
"""读 ZCode / DeepSeek Harness (DSH) 各自的用量记录，变成桌宠的碎碎念。

**ZCode 数据从哪来**：ZCode CLI 把每次模型请求的用量落进
``~/.zcode/cli/db/db.sqlite`` 的 ``model_usage`` 表（输入/输出/思考/缓存各分项、
耗时、首字延迟，明文未脱敏——脱敏的是 ``cli/log/*.jsonl`` 那份日志）。一条 SQL
就是精确值，不需要像 zcode-speed-panel 那样从网络字节**估算**。

**DSH 数据从哪来**：``~/.dsh/sessions/<工作区>/<会话>/session.v4.jsonl.zstd``
逐事件记录会话流水，每个事件都带毫秒 ``time``；``assistant/message`` 事件带
per-message ``usage``（uncached/cacheRead/output/totalTokens），``step/start`` /
``step/end`` 成对给出每步 LLM 耗时。会话可能跨天，totals 没法按天切，所以**只扫
今天有写入的会话文件**（mtime ≥ 今日零点，通常 0~3 个），解压后按事件时间精确
汇出"今天"——比起 dsh-archive-manager 插件的 ``session_projcache``（JSON 明文
但只有会话生命周期合计），这条路多付一次 zstd 解压（~1MB，毫秒级）换按天精确。

**为什么敢在 GUI 线程同步查**：ZCode 侧实测汇总 0.9ms；DSH 侧解压+解析今天
活跃会话实测 <10ms——都比一帧 16ms 预算小。ZCode 连接仍上双保险
（``timeout=0.2`` + ``busy_timeout``），宁可这一轮不报也不让桌宠卡一帧。

**失败语义**：库不存在（没装 ZCode）/表缺失/查询抛错 → ZCode 半边 None；
``~/.dsh`` 不存在 / 没有 zstandard / 解析失败 → DSH 半边 None。两半各自独立，
任何一边失败都只影响自己那段文案。碎碎念是装饰，绝不因为装饰报错或拖慢桌宠。

**口径**（两边一致）：
- 「今日」按**本地时区**零点切（用户感知的"今天"）；
- 总量含缓存读（ZCode ``computed_total_tokens`` / DSH ``totalTokens``，与计费
  口径一致）；
- 速度 = **今日平均**：当天所有「已完成且有产出」的请求上
  ``Σ(输出[+思考]) token ÷ Σ 耗时``。这就是 zcode-speed-panel ``avg`` 字段的同
  口径值（实测全程均值 42.1 ≈ 面板 ``avg`` 42.7）。面板另有一个实时 ``tps``
  ——那是网络下行字节的 700ms 采样；但用量行只在请求**结束后**落库/落盘，
  正在流式输出的那一次没有读数，"当前速度"算不出来。桌宠是几分钟瞟一眼的
  播报，稳定的日均也比抖动的瞬时值合适。
- ``cancelled`` / ``error`` / 0 产出的请求不参与速度（token 数不可信或耗时
  不该摊进均值）。
"""
from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger("dsh-pet-standalone")

#: ZCode CLI 用量库（``%USERPROFILE%/.zcode/cli/db/db.sqlite``）。
DB_PATH = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"

#: DSH 会话流水根目录（``%USERPROFILE%/.dsh/sessions``）。
DSH_SESSIONS_DIR = Path.home() / ".dsh" / "sessions"

# 连接超时（秒）：查询成本 <1ms，超时只可能来自 ZCode 侧的写锁瞬间。
_BUSY_TIMEOUT_MS = 200


def _day_start_ms() -> int:
    """今天本地零点的毫秒时间戳（``model_usage.started_at`` 是毫秒整数）。"""
    day0 = datetime.datetime.combine(
        datetime.date.today(), datetime.time.min)
    return int(day0.timestamp() * 1000)


def _format_tokens(n: int) -> str:
    """token 数的口语化：亿 / 万 两级，碎碎念里念出来顺口。"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _connect_ro() -> sqlite3.Connection | None:
    """只读连接；库不存在 / 打不开 → None（调用方静默跳过）。"""
    if not DB_PATH.is_file():
        return None
    try:
        con = sqlite3.connect(
            f"file:{DB_PATH.as_posix()}?mode=ro", uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000.0)
        con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return con
    except sqlite3.Error:
        logger.debug("usage_report: 打开 ZCode 用量库失败", exc_info=True)
        return None


def query_usage() -> dict | None:
    """今日 token 总量 + 今日平均输出速度；拿不到 → None。

    返回 ``{"today_total", "today_output", "today_cache_read", "today_requests",
    "today_avg_tps", "avg_requests"}``（``today_avg_tps`` 在当天没有「已完成且有
    产出」的请求时为 None——"今天还没正经跑完过活"本身就是要播报的状态，不算失败）。

    速度口径见模块 docstring：当天 ``Σ(输出+思考) ÷ Σ耗时``，即 zcode-speed-panel
    ``avg`` 字段的同口径值（面板另有的实时 ``tps`` 是网络字节采样，数据库做不出
    也无需做——一行 usage 只在请求结束后才落 token）。只统计 ``completed`` 且
    有产出的行：``cancelled`` / ``error`` 的 token 数不可信，0 产出的请求耗时
    也不该摊进均值。
    """
    con = _connect_ro()
    if con is None:
        return None
    try:
        day0 = _day_start_ms()
        try:
            today = con.execute(
                "SELECT COUNT(*),"
                " COALESCE(SUM(computed_total_tokens), 0),"
                " COALESCE(SUM(output_tokens + reasoning_tokens), 0),"
                " COALESCE(SUM(cache_read_input_tokens), 0)"
                " FROM model_usage WHERE started_at >= ?",
                (day0,)).fetchone()
            avg = con.execute(
                "SELECT COUNT(*),"
                " SUM(output_tokens + reasoning_tokens) * 1000.0 / SUM(duration_ms)"
                " FROM model_usage"
                " WHERE started_at >= ? AND status = 'completed'"
                " AND duration_ms > 0 AND (output_tokens + reasoning_tokens) > 0",
                (day0,)).fetchone()
        except sqlite3.Error:
            # 表不存在（老版本 ZCode）/ schema 变动：与没装同样对待。
            logger.debug("usage_report: model_usage 查询失败", exc_info=True)
            return None
    except sqlite3.Error:
        logger.debug("usage_report: 用量查询失败", exc_info=True)
        return None
    finally:
        con.close()

    request_count, total, output, cache_read = today
    avg_requests, today_avg_tps = avg if avg else (0, None)
    return {
        "today_total": int(total),
        "today_output": int(output),
        "today_cache_read": int(cache_read),
        "today_requests": int(request_count),
        "today_avg_tps": (float(today_avg_tps)
                          if today_avg_tps is not None else None),
        "avg_requests": int(avg_requests or 0),
    }


def _dsh_today_files(day0_ms: int) -> list[Path]:
    """今天有写入的 DSH 会话文件（``mtime ≥ 今日零点``，通常 0~3 个）。"""
    root = DSH_SESSIONS_DIR
    if not root.is_dir():
        return []
    out = []
    for f in root.glob("*/*/session.v4.jsonl.zstd"):
        try:
            if f.stat().st_mtime * 1000 >= day0_ms:
                out.append(f)
        except OSError:
            continue
    return out


def dsh_query_usage() -> dict | None:
    """DSH 今天的 token 总量 + 今日平均输出速度；拿不到 → None。

    只解压今天有写入的会话（见 :func:`_dsh_today_files`），逐事件按时间切出
    「今天」：

    - **用量**：``assistant/message`` 事件（``time ≥ 今日零点``）的 ``usage``
      求和——``totalTokens = uncached 输入 + cacheRead + 输出``，与 ZCode 的
      ``computed_total_tokens`` 同口径（含缓存读）；
    - **速度**：``step/start`` / ``step/end`` 按 (turn, step) 配对出每步 LLM
      耗时，步的 token 取落在该步里的 assistant/message 输出；只计**今天结束**
      的步。``Σ 输出 ÷ Σ 步耗时``，与 ZCode 的 ``Σ token ÷ Σ duration`` 同口径。

    ``zstandard`` 缺失 / ``~/.dsh`` 不存在 / 解析失败 → None（DSH 半边静默缺席，
    不影响 ZCode 半边）。zstandard 惰性导入：没装 DSH 的机器不为它付 import 成本。
    """
    try:
        import zstandard  # noqa: F401 —— 有没有装是"要不要扫 DSH"的判据
    except ImportError:
        return None
    day0 = _day_start_ms()
    files = _dsh_today_files(day0)
    if not files:
        return None

    total = output = 0
    messages = 0
    step_start: dict[tuple, int] = {}
    step_span: dict[tuple, int] = {}
    step_tokens: dict[tuple, int] = {}
    try:
        dctx = zstandard.ZstdDecompressor()
        for f in files:
            try:
                with open(f, "rb") as fh:
                    text = dctx.stream_reader(fh).read().decode(
                        "utf-8", errors="replace")
            except (OSError, zstandard.ZstdError):
                logger.debug("usage_report: DSH 会话解压失败 %s", f,
                             exc_info=True)
                continue
            for line in text.splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                kind = obj.get("type")
                tm = obj.get("time")
                data = obj.get("data") or {}
                if kind == "assistant/message" and isinstance(tm, int) \
                        and tm >= day0:
                    usage = data.get("usage")
                    if isinstance(usage, dict):
                        total += int(usage.get("totalTokens") or 0)
                        out_tok = int(usage.get("outputTokens") or 0)
                        output += out_tok
                        messages += 1
                        key = (data.get("turn"), data.get("step"))
                        step_tokens[key] = step_tokens.get(key, 0) + out_tok
                elif kind == "step/start":
                    key = (data.get("turn"), data.get("step"))
                    if key not in step_start:
                        step_start[key] = tm
                elif kind == "step/end" and isinstance(tm, int):
                    key = (data.get("turn"), data.get("step"))
                    # 同名步重开（重试）取最近一次 end；span 用首个 start。
                    if key not in step_start:
                        step_start[key] = tm
                    step_span[key] = tm
    except Exception:  # noqa: BLE001 —— 装饰功能，任何意外都整半边放弃
        logger.debug("usage_report: DSH 用量解析失败", exc_info=True)
        return None

    if messages <= 0:
        return None
    dur_sum = tok_sum = 0
    requests = 0
    for key, end_ms in step_span.items():
        if end_ms < day0:
            continue
        span = end_ms - step_start.get(key, end_ms)
        if span <= 0:
            continue
        dur_sum += span
        tok_sum += step_tokens.get(key, 0)
        requests += 1
    return {
        "today_total": total,
        "today_output": output,
        "today_messages": messages,
        "today_requests": requests,
        "today_avg_tps": (tok_sum * 1000.0 / dur_sum
                          if dur_sum > 0 and tok_sum > 0 else None),
    }


def build_bubble_text() -> str | None:
    """查一次 ZCode + DSH 的今日用量，拼成碎碎念文案；两边都没数据 → None。

    一边有就只播那边；两边都有则并成一句（气泡按 6 秒展示，见调用方）。
    口吻对齐现有 self_talk_texts（"……" 收尾）。
    """
    parts = []
    zc = query_usage()
    if zc and zc["today_requests"] > 0:
        seg = (f"ZCode 陪跑了 {zc['today_requests']} 次请求、"
               f"{_format_tokens(zc['today_total'])} token")
        if zc["today_avg_tps"] is not None:
            seg += f"，今日平均输出 {zc['today_avg_tps']:.1f} tok/s"
        parts.append(seg)
    dsh = dsh_query_usage()
    if dsh and dsh["today_requests"] > 0:
        seg = (f"DSH 陪跑了 {dsh['today_requests']} 步、"
               f"{_format_tokens(dsh['today_total'])} token")
        if dsh["today_avg_tps"] is not None:
            seg += f"，今日平均输出 {dsh['today_avg_tps']:.1f} tok/s"
        parts.append(seg)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0] + "，休息一下眼睛呀……"
    return "今天两边的账单：" + "；".join(parts) + "。休息一下眼睛呀……"


__all__ = ["DB_PATH", "query_usage", "build_bubble_text", "_format_tokens"]
