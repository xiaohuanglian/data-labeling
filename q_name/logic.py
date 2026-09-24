# -*- coding: utf-8 -*-
"""量化粗命名：动作_s编号_t序号_机位。做法只进对照表，不进文件名。"""

from __future__ import annotations

import csv
import re
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}
SKIP_DIR_NAME = "作废"
LOOKUP_NAME = "对照表.csv"

LOOKUP_HEADERS = [
    "现场序号",
    "保留",
    "用户编号",
    "动作",
    "动作代码",
    "侧",
    "目标做法",
    "切分后建议r",
    "机位",
    "原文件名",
    "新文件名",
]

CAM_ORDER = ("c0", "c90", "c180", "c270")
CAM_ALIAS = {
    "c0": "c0",
    "0度": "c0",
    "c90": "c90",
    "90度": "c90",
    "c180": "c180",
    "180度": "c180",
    "c270": "c270",
    "270度": "c270",
}

RAW_STAMP_RE = re.compile(r"^A\d{3}_(\d{8})_C(\d+)", re.IGNORECASE)
CLIP_NO_RE = re.compile(r"_C(\d+)", re.IGNORECASE)
COARSE_RE = re.compile(
    r"^(?P<action>[a-z]{2})_s(?P<user>\d+)_t(?P<take>\d+)_(?P<cam>c(?:0|90|180|270))$",
    re.IGNORECASE,
)
R_RANGE_RE = re.compile(r"^r(\d+)(?:-r(\d+))?$")

# 260825 全套动作；qs / sl 的档位按 260922 补充指导。
ACTIONS: list[dict] = [
    {
        "code": "sq",
        "name": "深蹲",
        "kind": "动态",
        "sides": False,
        "practices": ["标准平行蹲", "蹲太浅（屈髋不足）", "过度前俯（屈髋过度）", "蹲过深（屈膝过度）"],
        "r_hint": "一条里有多次时填范围，例如 r001-r005。一条一次就填 r001。",
    },
    {
        "code": "sw",
        "name": "燕式平衡",
        "kind": "静态",
        "sides": True,
        "practices": ["后腿接近水平", "明显低于水平"],
        "r_hint": "260922 补采：首批已用 r001/r006/r011/r016。本次从 r017 连续编到 r032，一条一个 r。",
    },
    {
        "code": "bs",
        "name": "保加利亚蹲",
        "kind": "动态",
        "sides": True,
        "practices": ["前腿约90°", "屈膝不足"],
        "r_hint": "一条里有多次时填范围，例如 r001-r005。一条一次就填 r001。",
    },
    {
        "code": "qs",
        "name": "股四头肌拉伸",
        "kind": "静态",
        "sides": True,
        "practices": ["标准（脚跟拉近臀部）", "真不足（脚跟到小腿中段）"],
        "r_hint": "260922：真不足要夸张，脚跟只收到小腿中段。补采从 r017 起，标准 6 条、真不足 10 条。",
    },
    {
        "code": "ss",
        "name": "单脚站立",
        "kind": "静态",
        "sides": True,
        "practices": ["脚明显离地", "脚几乎没离地"],
        "r_hint": "量化的是抬脚高度。260922 补采从 r017 连续编到 r032，一条一个 r。",
    },
    {
        "code": "kl",
        "name": "跪姿抬腿",
        "kind": "动态",
        "sides": True,
        "practices": ["大腿接近水平", "明显偏低"],
        "r_hint": "一条里有多次时填范围，例如 r001-r005。一条一次就填 r001。",
    },
    {
        "code": "gb",
        "name": "臀桥",
        "kind": "动态",
        "sides": False,
        "practices": ["标准", "臀太低", "顶过高"],
        "r_hint": "无左右侧。一条里有多次时填范围，例如 r001-r007。",
    },
    {
        "code": "sb",
        "name": "单腿臀桥",
        "kind": "动态",
        "sides": True,
        "practices": ["标准", "臀太低"],
        "r_hint": "一条里有多次时填范围，例如 r001-r005。一条一次就填 r001。",
    },
    {
        "code": "sl",
        "name": "侧抬腿",
        "kind": "动态",
        "sides": True,
        "practices": ["太低（≤25°）", "标准（30–45°）", "太高（>50°）"],
        "r_hint": "260922：太低 ≤25°，标准 30–45°。太高本次不补采。补采从 r021 编到 r040。",
    },
    {
        "code": "sp",
        "name": "侧平板支撑",
        "kind": "静态",
        "sides": True,
        "practices": ["骨盆成一条线", "骨盆下沉"],
        "r_hint": "背面朝屏幕。260922 补采从 r017 连续编到 r032，一条一个 r。",
    },
]


def action_by_code(code: str) -> dict | None:
    key = (code or "").strip().lower()
    for item in ACTIONS:
        if item["code"] == key:
            return item
    return None


def schema_payload() -> dict:
    return {
        "actions": ACTIONS,
        "cameras": [{"code": cam, "name": cam} for cam in CAM_ORDER],
        "sides": [
            {"code": "L", "name": "左"},
            {"code": "R", "name": "右"},
            {"code": "-", "name": "无"},
        ],
        "formula": "{动作}_s{用户}_t{序号}_{机位}",
    }


def normalize_user(raw: str) -> str:
    text = (raw or "").strip().lower()
    if not text:
        raise ValueError("请填写用户编号")
    if text.startswith("s") and text[1:].isdigit():
        return f"s{int(text[1:]):03d}"
    if text.isdigit():
        return f"s{int(text):03d}"
    raise ValueError("用户编号写成 s004 或 4")


def normalize_take(raw: str | int) -> int:
    if isinstance(raw, int):
        number = raw
    else:
        text = (raw or "").strip().lower()
        if text.startswith("t"):
            text = text[1:]
        if not text.isdigit():
            raise ValueError("现场序号写成 1 或 t01")
        number = int(text)
    if number <= 0:
        raise ValueError("现场序号从 1 开始")
    return number


def normalize_r_range(raw: str) -> str:
    text = (raw or "").strip().lower().replace("–", "-").replace("—", "-").replace(" ", "")
    match = R_RANGE_RE.fullmatch(text)
    if not match:
        raise ValueError("切分后建议 r 写成 r017 或 r001-r005，不写入文件名")
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if start <= 0 or end < start:
        raise ValueError("切分后建议 r 范围无效")
    if start == end:
        return f"r{start:03d}"
    return f"r{start:03d}-r{end:03d}"


def normalize_side(raw: str, action: dict) -> str:
    side = (raw or "").strip().upper()
    if side in {"无", "NONE", "-"}:
        side = "-"
    if side in {"左"}:
        side = "L"
    if side in {"右"}:
        side = "R"
    if not action["sides"]:
        return "-"
    if side not in {"L", "R"}:
        raise ValueError("这个动作要选左或右")
    return side


def side_label(side: str) -> str:
    return {"L": "左", "R": "右"}.get(side, "-")


def compose_stem(action: str, user: str, take: int, cam: str) -> str:
    return f"{action}_{user}_t{take:02d}_{cam}"


def parse_coarse(stem: str) -> dict | None:
    match = COARSE_RE.fullmatch((stem or "").strip())
    if not match:
        return None
    return {
        "action": match.group("action").lower(),
        "user": f"s{int(match.group('user')):03d}",
        "take": int(match.group("take")),
        "cam": match.group("cam").lower(),
    }


def clip_stamp(name: str) -> str:
    match = RAW_STAMP_RE.match(Path(name).name)
    return match.group(1) if match else ""


def sort_key(name: str) -> tuple:
    if parse_coarse(Path(name).stem) is not None:
        return (1, name.casefold())
    stamp = clip_stamp(name)
    clip = CLIP_NO_RE.search(name)
    clip_no = int(clip.group(1)) if clip else 10**9
    return (0, stamp, clip_no, name.casefold())


def list_videos(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    files = [
        p.name
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")
    ]
    return sorted(files, key=sort_key)


def cam_code_from_dirname(name: str) -> str | None:
    match = re.fullmatch(
        r"(?:.*[-_])?(c270|c180|c90|c0|270度|180度|90度|0度)",
        name,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return CAM_ALIAS[match.group(1).lower()]


def _scan_cams(parent: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    if not parent.is_dir():
        return found
    for child in parent.iterdir():
        if not child.is_dir() or child.name == SKIP_DIR_NAME:
            continue
        cam = cam_code_from_dirname(child.name)
        if cam:
            found.append((cam, child))
    found.sort(key=lambda item: CAM_ORDER.index(item[0]))
    return found


def session_root_for(camera_parent: Path) -> Path:
    if camera_parent.name == "现场原视频":
        return camera_parent.parent
    return camera_parent


def discover(raw: str) -> dict:
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("视频文件夹不存在")

    selected_cam = cam_code_from_dirname(path.name)
    if selected_cam:
        camera_parent = path.parent
        cameras = _scan_cams(camera_parent)
        watch_cam = selected_cam
        watch_folder = path
    else:
        cameras = _scan_cams(path)
        camera_parent = path
        if not cameras:
            nested = path / "现场原视频"
            nested_cams = _scan_cams(nested)
            if nested_cams:
                cameras = nested_cams
                camera_parent = nested
        if cameras:
            codes = {cam for cam, _folder in cameras}
            watch_cam = "c0" if "c0" in codes else cameras[0][0]
            watch_folder = next(folder for cam, folder in cameras if cam == watch_cam)
        else:
            watch_cam = ""
            watch_folder = path

    session_root = session_root_for(camera_parent) if cameras else path
    return {
        "session_root": session_root,
        "camera_parent": camera_parent if cameras else path,
        "cameras": cameras,
        "watch_folder": watch_folder if watch_folder.is_dir() else path,
        "watch_cam": watch_cam or "",
        "sync_available": len(cameras) > 1,
    }


def lookup_path(session_root: Path) -> Path:
    return session_root / LOOKUP_NAME


def read_lookup(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.is_file():
        return [], list(LOOKUP_HEADERS)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = [{key: (value or "") for key, value in row.items()} for row in reader]
    for header in LOOKUP_HEADERS:
        if header not in fields:
            fields.append(header)
    return rows, fields


def write_lookup(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def next_take(rows: list[dict[str, str]]) -> int:
    best = 0
    for row in rows:
        try:
            best = max(best, int(row.get("现场序号") or 0))
        except ValueError:
            continue
    return best + 1


def unnamed_paths(folder: Path) -> list[Path]:
    return [folder / name for name in list_videos(folder) if parse_coarse(Path(name).stem) is None]


def slot_files(current: Path, cameras: list[tuple[str, Path]]) -> list[dict]:
    """按未命名序号对齐四机；已粗命名的按 t 序号对齐。"""
    parsed = parse_coarse(current.stem)
    try:
        index = next(i for i, path in enumerate(unnamed_paths(current.parent)) if path.name == current.name)
    except StopIteration:
        index = None
    rows = []
    groups = cameras or [("", current.parent)]
    for cam, folder in groups:
        sibling = None
        if folder.resolve() == current.parent.resolve():
            sibling = current
        elif parsed:
            for name in list_videos(folder):
                other = parse_coarse(Path(name).stem)
                if other and other["action"] == parsed["action"] and other["user"] == parsed["user"] and other["take"] == parsed["take"] and other["cam"] == cam:
                    sibling = folder / name
                    break
        elif index is not None:
            others = unnamed_paths(folder)
            if index < len(others):
                sibling = others[index]
        if sibling is None or not sibling.is_file():
            rows.append({"cam": cam, "folder": folder, "path": None, "missing": True, "stamp": "", "size": 0})
            continue
        rows.append({
            "cam": cam or (parse_coarse(sibling.stem) or {}).get("cam", ""),
            "folder": folder,
            "path": sibling,
            "missing": False,
            "stamp": clip_stamp(sibling.name),
            "size": sibling.stat().st_size,
        })
    return rows


def assert_take_free(rows: list[dict[str, str]], take: int, slot_names: set[str]) -> None:
    for row in rows:
        try:
            row_take = int(row.get("现场序号") or 0)
        except ValueError:
            continue
        if row_take != take or (row.get("保留") or "") != "是":
            continue
        name = (row.get("新文件名") or "").strip()
        if name and name not in slot_names:
            raise ValueError(f"现场序号 t{take:02d} 已用于其他文件 {name}")


def replace_take_rows(
    rows: list[dict[str, str]],
    take: int,
    new_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    kept = []
    for row in rows:
        try:
            row_take = int(row.get("现场序号") or 0)
        except ValueError:
            kept.append(row)
            continue
        if row_take != take:
            kept.append(row)
    kept.extend(new_rows)
    kept.sort(key=lambda row: (int(row.get("现场序号") or 0), CAM_ORDER.index(row.get("机位")) if row.get("机位") in CAM_ORDER else 99))
    return kept


def build_rows(
    *,
    take: int,
    keep: str,
    user: str,
    action: dict | None,
    side: str,
    practice: str,
    r_range: str,
    files: list[tuple[str, str, str]],
) -> list[dict[str, str]]:
    """files: (cam, original_name, new_name) in camera order."""
    rows = []
    for cam, original_name, new_name in files:
        rows.append({
            "现场序号": str(take),
            "保留": keep,
            "用户编号": user,
            "动作": action["name"] if action else "",
            "动作代码": action["code"] if action else "",
            "侧": side_label(side) if action else "",
            "目标做法": practice,
            "切分后建议r": r_range,
            "机位": cam,
            "原文件名": original_name,
            "新文件名": new_name,
        })
    return rows
