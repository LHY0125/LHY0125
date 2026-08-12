#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
waka_stats.py — 生成 README 中 wakatime 详细统计区块（<!--START_SECTION:devstats-->）。

用法（在仓库根目录执行）：
    python3 .github/scripts/waka_stats.py

数据源：
- 设置环境变量 WAKATIME_API_KEY 时：调用 /users/current 端点（返回完整数据，含 AI 编码统计）
- 未设置但设了 WAKA_ALLOW_PUBLIC=1 时：降级调用公开 /users/{PUBLIC_USER_ID} 端点（本地预览，无 AI 字段）
- 未设置且未显式允许时：报错退出（CI 中禁止静默降级，避免提交缺失 AI 区块的 README）

展示维度（语言统计由 waka-readme 负责，脚本不重复）：
    总览时长/日均 → 最佳编码日 → AI 编码详情（代码行/会话/模型）→ 编辑器 → 操作系统 → 活动类别

退出码：0 成功；1 失败（API 错误 / JSON 异常 / README 缺失）。失败时不改动 README。
"""

import base64
import json
import os
import re
import sys
import urllib.request
from datetime import date
from types import MappingProxyType
from typing import Any

# 公开 user id（用于无 API key 时的降级预览，对应 README badge 里的 ID）
PUBLIC_USER_ID: str = "9747123e-0660-43be-a937-7b33dfdc85b4"
SECTION: str = "devstats"
TIME_RANGE: str = "last_7_days"
API_BASE: str = "https://wakatime.com/api/v1"

SCRIPT_DIR: str = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT: str = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))
README_PATH: str = os.path.join(REPO_ROOT, "README.md")

BAR_FULL: str = "█"
BAR_EMPTY: str = "░"
BAR_WIDTH: int = 20

# AI 模型名 → 更可读的展示名（只读映射）
MODEL_NAMES = MappingProxyType({
    "Deepseek": "DeepSeek",
    "Claude-Code": "Claude Code",
    "Opencode-Cli": "OpenCode CLI",
    "Vscode-Wakatime": "VS Code",
})

WEEKDAY_CN: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def log(msg: str) -> None:
    """进度输出（保证 utf-8，避免 Windows 控制台乱码；失败时兜底到 stderr）。"""
    try:
        print(msg, flush=True)
    except Exception:
        try:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()
        except Exception:
            pass


def fetch_stats() -> dict[str, Any]:
    """拉取统计数据。有 key 用 current 端点；无 key 仅当显式允许时降级公开端点。"""
    key = os.environ.get("WAKATIME_API_KEY")
    if key:
        url = f"{API_BASE}/users/current/stats/{TIME_RANGE}"
        token = base64.b64encode(key.encode("utf-8")).decode("ascii")
        headers = {"Authorization": "Basic " + token}
    elif os.environ.get("WAKA_ALLOW_PUBLIC") == "1":
        url = f"{API_BASE}/users/{PUBLIC_USER_ID}/stats/{TIME_RANGE}"
        headers = {"User-Agent": "waka-stats/1.0"}
    else:
        raise RuntimeError(
            "缺少 WAKATIME_API_KEY（CI 中禁止静默降级公开 API；本地预览请设 WAKA_ALLOW_PUBLIC=1）"
        )
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if "data" not in payload:
        raise RuntimeError("API 未返回 data 字段")
    return payload["data"]


def fmt_hms(sec: Any) -> str:
    """紧凑时长：6h 12m / 42m / 10s（用于代码块内，保持对齐）。"""
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    if m:
        return f"{m}m"
    return f"{s}s"


def fmt_cn(sec: Any) -> str:
    """中文时长：30 小时 52 分 / 42 分钟 / 10 秒（用于总览行）。"""
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h and m:
        return f"{h} 小时 {m} 分"
    if h:
        return f"{h} 小时"
    if m and s:
        return f"{m} 分 {s} 秒"
    if m:
        return f"{m} 分钟"
    return f"{s} 秒"


def fmt_num(n: Any) -> str:
    """整数千分位：5211 → 5,211"""
    return f"{int(n or 0):,}"


def disp_width(s: str) -> int:
    """显示宽度：中文/全角字符算 2 列，其余算 1 列（等宽字体下对齐）。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in s)


def pad(s: str, width: int) -> str:
    """按显示宽度在右侧补空格到 width 列。"""
    return s + " " * max(0, width - disp_width(s))


def pad_left(s: str, width: int) -> str:
    """按显示宽度在左侧补空格（右对齐）到 width 列。"""
    return " " * max(0, width - disp_width(s)) + s


def bar(fraction: float, width: int = BAR_WIDTH) -> str:
    """进度条：fraction 取 0~1，█ 填充 / ░ 空白。条长 = 占比，与百分比刻度一致。"""
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def render_section(title: str, items: Any, top_n: int) -> str:
    """通用小节：名称 + 时长 + 进度条 + 百分比。条长 = percent/100（与百分比一致）。
    条对齐列 = 14(label 右对齐) + 12(value 右对齐) + 2 空格。"""
    if not items:
        return ""
    items = sorted(items, key=lambda x: x.get("total_seconds") or 0, reverse=True)[:top_n]
    body = "\n".join(
        f"{pad_left(it.get('name', '?').strip()[:16], 14)}"
        f"{pad_left(fmt_hms(it.get('total_seconds') or 0), 12)}  "
        f"{bar((it.get('percent') or 0) / 100)}  "
        f"{(it.get('percent') or 0):>5.1f}%"
        for it in items
    )
    return f"#### {title}\n\n```txt\n{body}\n```"


def render_best_day(data: dict[str, Any]) -> str:
    """最佳编码日。"""
    best = data.get("best_day")
    if not best or not best.get("date"):
        return ""
    try:
        y, m, d = (int(p) for p in best["date"].split("-")[:3])
        label = f"{m}/{d}（{WEEKDAY_CN[date(y, m, d).weekday()]}）"
    except (ValueError, IndexError):
        label = best["date"]
    value = fmt_cn(best.get("total_seconds") or 0)
    body = f"{pad_left(label, 14)}{pad_left(value, 12)}  {BAR_FULL * BAR_WIDTH}"
    return f"#### 🏆 最佳编码日\n\n```txt\n{body}\n```"


def render_ai(data: dict[str, Any]) -> str:
    """AI 编码详情：AI/人类代码行比例 + 会话 + 提示词事件 + 主要模型。"""
    ai_add = data.get("ai_additions") or 0
    hu_add = data.get("human_additions") or 0
    sessions = data.get("ai_sessions")
    events = data.get("ai_prompt_events_total")
    has_lines = ai_add + hu_add > 0

    parts = []
    lines = []
    if has_lines:
        pct = ai_add / (ai_add + hu_add) * 100
        lines.append(
            f"{pad_left('AI', 14)}{pad_left(f'{fmt_num(ai_add)} 行', 12)}  "
            f"{bar(pct / 100)}  {pct:>5.1f}%"
        )
        lines.append(
            f"{pad_left('Human', 14)}{pad_left(f'{fmt_num(hu_add)} 行', 12)}  "
            f"{bar(hu_add / (ai_add + hu_add))}  {100 - pct:>5.1f}%"
        )
    if sessions or events:
        meta = []
        if sessions:
            meta.append(f"AI 会话 {fmt_num(sessions)} 次")
        if events:
            meta.append(f"提示词 {fmt_num(events)} 次")
        if meta:
            lines.append("")
            lines.append("  ·  ".join(meta))
    if lines:
        parts.append("#### ✨ AI 编码详情\n\n```txt\n" + "\n".join(lines) + "\n```")

    # 主要 AI 模型（按生成行数），条长 = 行数占比
    mb = data.get("ai_model_line_changes") or {}
    models = sorted(mb.items(), key=lambda kv: kv[1], reverse=True)[:5]
    if models and max(v for _, v in models) > 0:
        total = sum(v for _, v in models) or 1
        body = "\n".join(
            f"{pad_left(MODEL_NAMES.get(name, name)[:12], 14)}"
            f"{pad_left(f'{fmt_num(ln)} 行', 12)}  "
            f"{bar(ln / total)}  {ln / total * 100:>5.1f}%"
            for name, ln in models
        )
        parts.append("#### 🧠 主要 AI 模型\n\n```txt\n" + body + "\n```")
    return "\n\n".join(parts)


def build_block(data: dict[str, Any]) -> str:
    """组装 devstats 区块的 markdown 内容。"""
    total = data.get("total_seconds") or 0
    total_all = data.get("total_seconds_including_other_language") or 0
    daily = data.get("daily_average") or 0
    daily_all = data.get("daily_average_including_other_language") or 0

    lines = [
        "### ⏱ 近 7 天编码总览",
        "",
        f"- 总时长：**{fmt_cn(total_all)}**（含未分类）｜纯代码 **{fmt_cn(total)}**",
        f"- 日均：**{fmt_cn(daily_all)}**（含未分类）｜纯代码 **{fmt_cn(daily)}**",
        "",
    ]
    sections = [
        render_best_day(data),
        render_ai(data),
        render_section("🖥 编辑器分布", data.get("editors"), 6),
        render_section("💻 操作系统", data.get("operating_systems"), 4),
        render_section("⚡ 活动类别", data.get("categories"), 6),
    ]
    body = "\n\n".join(p for p in sections if p)
    if body:
        lines.append(body)
    return "\n".join(lines)


def main() -> int:
    try:
        data = fetch_stats()
    except Exception as e:
        log(f"[waka_stats] 拉取 wakatime 数据失败: {e}")
        return 1

    if data.get("status") not in (None, "ok"):
        log(f"[waka_stats] API 返回异常 status={data.get('status')!r}")
        return 1

    try:
        content = build_block(data)
    except Exception as e:
        log(f"[waka_stats] 生成统计区块失败: {e}")
        return 1

    if not os.path.exists(README_PATH):
        log(f"[waka_stats] 找不到 README: {README_PATH}")
        return 1
    with open(README_PATH, encoding="utf-8") as f:
        readme = f.read()

    start = f"<!--START_SECTION:{SECTION}-->"
    end = f"<!--END_SECTION:{SECTION}-->"
    replacement = f"{start}\n\n{content.strip()}\n\n{end}"

    if start in readme and end in readme:
        pattern = re.compile(re.escape(start) + r"[\s\S]*?" + re.escape(end))
        new_readme, n = pattern.subn(lambda _: replacement, readme)
    else:
        new_readme = readme.rstrip() + "\n\n" + replacement + "\n"
        n = 1

    if new_readme == readme:
        log("[waka_stats] README 无变化，跳过写入")
        return 0

    with open(README_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_readme)
    log("[waka_stats] README 已更新 devstats 区块")
    return 0


if __name__ == "__main__":
    sys.exit(main())
