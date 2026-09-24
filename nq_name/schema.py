# -*- coding: utf-8 -*-
"""六段式命名：{受试者}_{动作}_{侧别}_{标签}_{Rep}_{机位}.ext"""

from __future__ import annotations

import re
from typing import Any

# 动作代码 → 名称 / 动静 / 错误列表（界面显示名称+代号，文件名只写代号）
ACTIONS: list[dict[str, Any]] = [
    {
        "code": "A01",
        "name": "单腿臀桥",
        "kind": "动态",
        "errors": [{"code": "E01", "name": "骨盆旋转"}],
    },
    {
        "code": "A02",
        "name": "侧平板支撑",
        "kind": "静态",
        "errors": [{"code": "E01", "name": "躯干没有挺直"}],
    },
    {
        "code": "A03",
        "name": "保加利亚蹲",
        "kind": "动态",
        "errors": [{"code": "E01", "name": "躯干没有挺直（腰背弯曲）"}],
    },
    {
        "code": "A04",
        "name": "侧抬腿",
        "kind": "动态",
        "errors": [{"code": "E01", "name": "骨盆旋转"}],
    },
    {
        "code": "A05",
        "name": "单脚站立",
        "kind": "静态",
        "errors": [
            {"code": "E01", "name": "躯干没有挺直-前倾"},
            {"code": "E02", "name": "躯干没有挺直-后倾"},
            {"code": "E03", "name": "骨盆倾斜"},
            {"code": "E04", "name": "跌倒"},
        ],
    },
    {
        "code": "A06",
        "name": "弓箭步",
        "kind": "静态",
        "errors": [{"code": "E01", "name": "骨盆倾斜"}],
    },
    {
        "code": "A07",
        "name": "燕式平衡",
        "kind": "静态",
        "errors": [{"code": "E01", "name": "骨盆旋转"}],
    },
    {
        "code": "A08",
        "name": "股四头肌拉伸",
        "kind": "静态",
        "errors": [{"code": "E01", "name": "躯干没有挺直（身体前倾）"}],
    },
    {
        "code": "A09",
        "name": "髂胫束拉伸",
        "kind": "静态",
        "errors": [{"code": "E01", "name": "骨盆没有倾斜"}],
    },
    {
        "code": "A10",
        "name": "鸟狗式",
        "kind": "动态",
        "errors": [{"code": "E01", "name": "骨盆旋转"}],
    },
    {
        "code": "A11",
        "name": "T字背部伸展",
        "kind": "动态",
        "errors": [{"code": "E01", "name": "胳膊没抬到和躯干一个平面"}],
    },
    {
        "code": "A12",
        "name": "跪姿抬腿",
        "kind": "动态",
        "errors": [{"code": "E01", "name": "骨盆旋转"}],
    },
    {
        "code": "A13",
        "name": "Y字肩胛激活",
        "kind": "动态",
        "errors": [{"code": "E01", "name": "胳膊没抬到和躯干一个平面"}],
    },
]

# 正确动作固定用 E00
CORRECT_ERROR = {"code": "E00", "name": "正确"}

SIDES = [
    {"code": "L", "name": "左侧"},
    {"code": "R", "name": "右侧"},
]

CAMERAS = [
    {"code": "C0", "name": "0°"},
    {"code": "C90", "name": "90°"},
    {"code": "C180", "name": "180°"},
    {"code": "C270", "name": "270°"},
]

# 新格式标签仅为 E00 / E01 …；兼容旧文件名中的 E00C / E01M
NAME_RE = re.compile(
    r"^(?P<subject>P\d+)_"
    r"(?P<action>A\d{2})_"
    r"(?P<side>[LR])_"
    r"(?P<label>E\d{2}[CM]?)_"
    r"(?P<rep>V\d{2})_"
    r"(?P<camera>C\w+)$",
    re.IGNORECASE,
)


def action_by_code(code: str) -> dict[str, Any] | None:
    code = (code or "").upper()
    for item in ACTIONS:
        if item["code"] == code:
            return item
    return None


def errors_for_action(action_code: str) -> list[dict[str, str]]:
    action = action_by_code(action_code)
    errors = [CORRECT_ERROR]
    if action:
        errors.extend(action["errors"])
    return errors


def normalize_subject(raw: str) -> str:
    raw = (raw or "").strip().upper()
    if not raw:
        return ""
    if raw.startswith("P") and raw[1:].isdigit():
        return f"P{raw[1:].zfill(3)}"
    if raw.isdigit():
        return f"P{raw.zfill(3)}"
    return raw


def normalize_rep(raw: str | int) -> str:
    if isinstance(raw, int):
        return f"V{raw:02d}"
    text = (raw or "").strip().upper()
    if not text:
        return ""
    if text.startswith("V") and text[1:].isdigit():
        return f"V{int(text[1:]):02d}"
    if text.isdigit():
        return f"V{int(text):02d}"
    return text


def build_label(error_code: str) -> str:
    """标签仅写入错误代号，如 E00 / E01（不再附带 C/M）。"""
    err = (error_code or "").strip().upper()
    if not re.fullmatch(r"E\d{2}", err):
        raise ValueError(f"错误代号无效: {error_code}")
    return err


def compose_stem(
    subject: str,
    action: str,
    side: str,
    error_code: str,
    rep: str | int,
    camera: str,
    **_ignored: Any,
) -> str:
    subject = normalize_subject(subject)
    action = (action or "").strip().upper()
    side = (side or "").strip().upper()
    camera = (camera or "").strip().upper()
    rep = normalize_rep(rep)
    label = build_label(error_code)

    if not subject:
        raise ValueError("请填写受试者编号")
    if not action_by_code(action):
        raise ValueError(f"未知动作代码: {action}")
    if side not in {"L", "R"}:
        raise ValueError("侧别须为 L 或 R")
    if not rep.startswith("V"):
        raise ValueError("Rep 序号无效")
    if not camera.startswith("C"):
        raise ValueError("机位代号无效")

    allowed = {e["code"] for e in errors_for_action(action)}
    if error_code.strip().upper() not in allowed:
        raise ValueError(f"动作 {action} 不支持错误代码 {error_code}")

    return f"{subject}_{action}_{side}_{label}_{rep}_{camera}"


def parse_stem(stem: str) -> dict[str, str] | None:
    match = NAME_RE.match((stem or "").strip())
    if not match:
        return None
    label = match.group("label").upper()
    error_code = label[:3]
    return {
        "subject": match.group("subject").upper(),
        "action": match.group("action").upper(),
        "side": match.group("side").upper(),
        "error_code": error_code,
        "label": error_code,
        "rep": match.group("rep").upper(),
        "camera": match.group("camera").upper(),
    }


def schema_payload() -> dict[str, Any]:
    return {
        "formula": "{受试者编号}_{动作代码}_{侧别}_{标签}_{Rep序号}_{机位}.mp4",
        "actions": ACTIONS,
        "correct_error": CORRECT_ERROR,
        "sides": SIDES,
        "cameras": CAMERAS,
    }
