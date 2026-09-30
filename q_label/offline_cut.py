#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线下采集：按 checklist 切分动态动作，并写出 labels.csv。"""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

STATIC_ACTIONS = {"sw", "qs", "ss", "sp"}
DYNAMIC_ACTIONS = {"sq", "bs", "kl", "gb", "sb", "sl"}

ACTION_CN = {
    "sq": "深蹲",
    "sw": "燕式平衡",
    "bs": "保加利亚蹲",
    "qs": "股四头肌拉伸",
    "ss": "单脚站立",
    "kl": "跪姿抬腿",
    "gb": "臀桥",
    "sb": "单腿臀桥",
    "sl": "侧抬腿",
    "sp": "侧平板支撑",
}

DEFAULT_CAM = {
    "ss": "c0",
    "sp": "c180",
}

PRACTICE_TO_RANGE = {
    "标准平行蹲": "标准",
    "蹲太浅（屈髋不足）": "屈髋不足",
    "过度前俯（屈髋过度）": "屈髋过度",
    "蹲过深（屈膝过度）": "屈膝过度",
    "后腿接近水平": "标准",
    "明显低于水平": "不足",
    "前腿约90°": "标准",
    "前腿约 90°": "标准",
    "屈膝不足": "不足",
    "脚跟拉近臀部": "标准",
    "脚明显离地": "标准",
    "脚几乎没离地": "不足",
    "大腿接近水平": "标准",
    "明显偏低": "不足",
    "标准": "标准",
    "臀太低": "不足",
    "顶过高": "过头",
    "抬到30-45°": "标准",
    "抬到 30–45°": "标准",
    "抬到30–45°": "标准",
    "抬得很低": "不足",
    "骨盆成一条线": "标准",
    "骨盆下沉": "不足",
    "标准（脚跟拉近臀部）": "标准",
    "真不足（脚跟到小腿中段）": "不足",
    "太低（≤25°）": "不足",
    "标准（30–45°）": "标准",
    "太高（>50°）": "过头",
}

LABEL_HEADERS = [
    "文件名称", "动作", "用户编号", "来源", "身高cm", "躯干长cm",
    "支撑/发力侧", "正式峰值时间秒", "幅度判断", "目标做法",
    "峰值关键高度cm", "抖动等级", "抖动类型", "试探次数",
    "测量方法", "测量可信度", "是否有效", "备注",
]

CAMS = ("c0", "c90", "c180", "c270")


def find_ffmpeg() -> str:
    for candidate in ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        path = shutil.which(candidate) if "/" not in candidate else candidate
        if path and Path(path).is_file():
            return str(path)
    raise RuntimeError("未找到 ffmpeg。请先执行：brew install ffmpeg")


def parse_r_range(text: str) -> tuple[int, int]:
    raw = (text or "").strip().lower().replace(" ", "")
    nums = []
    n = ""
    for ch in raw:
        if ch.isdigit():
            n += ch
        elif n:
            nums.append(int(n))
            n = ""
    if n:
        nums.append(int(n))
    if len(nums) >= 2:
        return nums[0], nums[1] - nums[0] + 1
    if len(nums) == 1:
        return nums[0], 1
    return 1, 1


def range_judgement(practice: str) -> str:
    return PRACTICE_TO_RANGE.get((practice or "").strip(), practice or "")


def side_label(side: str) -> str:
    raw = (side or "").strip()
    if raw in {"-", "—", ""}:
        return "-"
    if raw.upper() == "L" or raw == "左":
        return "左"
    if raw.upper() == "R" or raw == "右":
        return "右"
    return raw


CAM_DIR_NAMES = {"c0", "c90", "c180", "c270", "作废"}
ORIGINALS_DIR_NAME = "现场原视频"
PROCESSED_DIR_NAME = "处理后视频"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi"}
SKIP_VIDEO_DIRS = {"处理后视频", "clips", "iphone原片", "作废"}
_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def originals_root(session_root: Path) -> Path:
    nested = session_root / ORIGINALS_DIR_NAME
    return nested if nested.is_dir() else session_root


def processed_dir(session_root: Path) -> Path:
    new = session_root / PROCESSED_DIR_NAME
    old = session_root / "clips"
    if new.is_dir():
        return new
    if old.is_dir():
        return old
    return new


def _xlsx_column(ref: str) -> int:
    index = 0
    for ch in ref:
        if not ch.isalpha():
            break
        index = index * 26 + (ord(ch.upper()) - 64)
    return max(0, index - 1)


def _read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as book:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            root = ET.fromstring(book.read("xl/sharedStrings.xml"))
            for item in root.findall("m:si", _XLSX_NS):
                shared.append("".join(node.text or "" for node in item.findall(".//m:t", _XLSX_NS)))
        sheet_name = next((name for name in book.namelist() if name.startswith("xl/worksheets/sheet")), "")
        if not sheet_name:
            raise RuntimeError(f"{path.name} 里没有工作表")
        sheet = ET.fromstring(book.read(sheet_name))
    grid: list[list[str]] = []
    for row in sheet.findall("m:sheetData/m:row", _XLSX_NS):
        values: list[str] = []
        for cell in row.findall("m:c", _XLSX_NS):
            index = _xlsx_column(cell.get("r") or "")
            while len(values) < index:
                values.append("")
            kind = cell.get("t")
            if kind == "inlineStr":
                inline = cell.find("m:is", _XLSX_NS)
                text = "".join(node.text or "" for node in inline.findall(".//m:t", _XLSX_NS)) if inline is not None else ""
            else:
                node = cell.find("m:v", _XLSX_NS)
                raw = "" if node is None or node.text is None else node.text
                text = shared[int(raw)] if kind == "s" and raw.isdigit() and int(raw) < len(shared) else raw
            values.append(text)
        grid.append(values)
    if not grid:
        return []
    headers = [(cell or "").strip() for cell in grid[0]]
    rows = []
    for values in grid[1:]:
        if not any((cell or "").strip() for cell in values):
            continue
        rows.append({headers[i]: (values[i] if i < len(values) else "").strip() for i in range(len(headers)) if headers[i]})
    return rows


def _headers_ok(rows: list[dict[str, str]]) -> bool:
    if not rows:
        return False
    return any("现场序号" in (key or "") for key in rows[0])


def read_lookup_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".xlsx":
        rows = _read_xlsx_rows(path)
        if rows and not _headers_ok(rows):
            raise RuntimeError(f"{path.name} 里没有「现场序号」这一列")
        return rows
    raw = path.read_bytes()
    chosen: list[dict[str, str]] | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        lines = text.splitlines()
        if lines and lines[0].lower().startswith("sep="):
            text = "\n".join(lines[1:])
        rows = [{key: (value or "").strip() for key, value in row.items() if key} for row in csv.DictReader(io.StringIO(text))]
        chosen = rows
        if _headers_ok(rows) or (not rows and "现场序号" in text):
            return rows
    if chosen is not None and chosen and not _headers_ok(chosen):
        raise RuntimeError(f"{path.name} 里没有「现场序号」这一列。请用命名工具写出的对照表，不要改掉表头。")
    return chosen or []


def find_lookup(folder: Path) -> Path | None:
    current = Path(folder)
    for _ in range(4):
        csv_hit = current / "对照表.csv"
        if csv_hit.is_file():
            return csv_hit
        xlsx_hit = current / "对照表.xlsx"
        if xlsx_hit.is_file():
            return xlsx_hit
        if current.parent == current:
            break
        current = current.parent
    return None


def resolve_cam_session(folder: Path) -> tuple[Path, Path, str]:
    """所选文件夹就是工作目录。对照表可以在这一层，也可以在往上三级里。"""
    current = Path(folder).expanduser().resolve()
    if not current.is_dir():
        raise RuntimeError(f"找不到文件夹：{folder}")
    lookup = find_lookup(current)
    if lookup is None:
        raise RuntimeError(
            f"在 {current} 以及它的上三级里都没有找到对照表.csv。"
            "视频和对照表放在一起即可，不需要按机位拆文件夹。"
        )
    cam = current.name if current.name in {"c0", "c90", "c180", "c270"} else ""
    return lookup.parent, current, cam


def resolve_session_root(folder: Path) -> Path:
    root, _, _ = resolve_cam_session(folder)
    return root


def load_takes(folder: Path) -> list[dict]:
    root, selected, folder_cam = resolve_cam_session(folder)
    lookup = find_lookup(selected) or (root / "对照表.csv")

    grouped: dict[int, dict] = {}
    files: dict[int, dict[str, str]] = defaultdict(dict)
    for row in read_lookup_rows(lookup):
            keep = (row.get("保留") or "").strip()
            try:
                take = int(row.get("现场序号") or 0)
            except ValueError:
                continue
            if take <= 0:
                continue
            cam = (row.get("机位") or "").strip()
            name = (row.get("新文件名") or "").strip()
            if cam and name:
                files[take][cam] = name
            if take in grouped:
                continue
            action = (row.get("动作代码") or "").strip().lower()
            r0, count = parse_r_range(row.get("切分后建议r") or "")
            kind = "static" if action in STATIC_ACTIONS else "dynamic"
            grouped[take] = {
                "take": take,
                "keep": keep == "是",
                "action": action,
                "action_cn": row.get("动作") or ACTION_CN.get(action, action),
                "side": side_label(row.get("侧") or ""),
                "practice": (row.get("目标做法") or "").strip(),
                "range_judgement": range_judgement(row.get("目标做法") or ""),
                "r_start": r0,
                "rep_count": count,
                "kind": kind,
                "review_cam": "",
            }

    takes = []
    for take in sorted(grouped):
        item = grouped[take]
        item["files"] = files.get(take, {})
        review = ""
        if folder_cam and item["files"].get(folder_cam):
            review = folder_cam
        else:
            for cam_name in CAMS:
                if item["files"].get(cam_name):
                    review = cam_name
                    break
        item["review_cam"] = review
        name = item["files"].get(review) or ""
        item["present"] = bool(name and locate_video(folder, name, selected_only=bool(folder_cam)))
        takes.append(item)
    return takes


def _skipped_video(path: Path, base: Path) -> bool:
    try:
        relative = path.relative_to(base)
    except ValueError:
        return False
    return any(part in SKIP_VIDEO_DIRS for part in relative.parts[:-1])


def _same_video(path: Path, filename: str) -> bool:
    wanted = Path(filename)
    if path.name.casefold() == wanted.name.casefold():
        return True
    return path.stem.casefold() == wanted.stem.casefold() and path.suffix.lower() in VIDEO_EXTS


def locate_video(folder: Path, filename: str, *, selected_only: bool = False) -> Path | None:
    if not filename:
        return None
    root, selected, _ = resolve_cam_session(folder)
    bases: list[Path] = []
    for base in ((selected,) if selected_only else (selected, root, originals_root(root))):
        if base not in bases:
            bases.append(base)
    fuzzy: Path | None = None
    for base in bases:
        if not base.is_dir():
            continue
        direct = base / filename
        if direct.is_file() and not _skipped_video(direct, base):
            return direct
        for path in base.rglob("*"):
            if not path.is_file() or path.name.startswith(".") or _skipped_video(path, base):
                continue
            if path.name.casefold() == Path(filename).name.casefold():
                return path
            if fuzzy is None and _same_video(path, filename):
                fuzzy = path
    return fuzzy


def resolve_video(folder: Path, filename: str) -> Path:
    if not filename:
        raise RuntimeError("文件名为空")
    found = locate_video(folder, filename)
    if found is None:
        raise RuntimeError(f"找不到视频：{filename}")
    return found


def progress_path(output: Path) -> Path:
    return output / ".offline-progress.json"


def read_progress(output: Path) -> dict:
    path = progress_path(output)
    if not path.is_file():
        return {"done": [], "marks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"done": [], "marks": {}}
    if not isinstance(data, dict):
        return {"done": [], "marks": {}}
    data.setdefault("done", [])
    data.setdefault("marks", {})
    return data


def progress_key(take: int, cam: str) -> str:
    return f"{int(take)}:{(cam or '').strip().lower()}"


def is_take_done(done: list, take: int, cam: str) -> bool:
    """完成按机位记。旧数据只有现场序号时，表示当时四机一起切过。"""
    keys = {str(item) for item in done}
    prefix = f"{int(take)}:"
    if any(key.startswith(prefix) for key in keys):
        return progress_key(take, cam) in keys
    return str(int(take)) in keys


def marks_for(marks: dict, take: int, cam: str, sync: bool = False) -> dict:
    if not isinstance(marks, dict):
        return {}
    if sync:
        shared = marks.get(str(int(take)))
        return shared if isinstance(shared, dict) else {}
    keyed = marks.get(progress_key(take, cam))
    if isinstance(keyed, dict):
        return keyed
    prefix = f"{int(take)}:"
    if any(str(key).startswith(prefix) for key in marks):
        return {}
    legacy = marks.get(str(int(take)))
    return legacy if isinstance(legacy, dict) else {}


def write_progress(output: Path, data: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    progress_path(output).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def labels_path(output: Path) -> Path:
    return output / "labels.csv"


def ensure_labels_csv(output: Path) -> Path:
    path = labels_path(output)
    output.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LABEL_HEADERS)
            writer.writeheader()
    return path


def upsert_label_rows(output: Path, new_rows: list[dict[str, str]]) -> None:
    path = ensure_labels_csv(output)
    existing = []
    if path.is_file():
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            existing = list(csv.DictReader(f))
    names = {row.get("文件名称") for row in new_rows}
    kept = [row for row in existing if row.get("文件名称") not in names]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LABEL_HEADERS)
        writer.writeheader()
        for row in kept + new_rows:
            writer.writerow({key: row.get(key, "") for key in LABEL_HEADERS})


def run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "ffmpeg 失败").strip()
        raise RuntimeError(err[-800:])


def cut_clip(src: Path, dst: Path, start: float, end: float) -> None:
    ffmpeg = find_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.05, float(end) - float(start))
    preroll = min(float(start), 1.0)
    run_ffmpeg([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{float(start) - preroll:.3f}",
        "-i", str(src),
        "-ss", f"{preroll:.3f}",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-an",
        "-movflags", "+faststart",
        str(dst),
    ])


def remux_whole(src: Path, dst: Path) -> None:
    ffmpeg = find_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_ffmpeg([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-c", "copy",
            "-movflags", "+faststart",
            str(dst),
        ])
    except RuntimeError:
        run_ffmpeg([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-an",
            "-movflags", "+faststart",
            str(dst),
        ])


def build_segments(starts: list[float], ends: list[float], peaks: list[float], duration: float) -> list[dict]:
    """每一段用自己的起始和结束，不延伸到下一段或视频末尾。"""
    starts = [float(x) for x in starts]
    ends = [float(x) for x in ends]
    peaks = [float(x) for x in peaks]
    if not starts or not (len(starts) == len(ends) == len(peaks)):
        raise RuntimeError(
            f"每一段都要有起始、结束和峰值。现在起始 {len(starts)}、结束 {len(ends)}、峰值 {len(peaks)}"
        )
    limit = float(duration) if duration and float(duration) > 0 else None
    segs = []
    for i, (start, end, peak) in enumerate(zip(starts, ends, peaks), start=1):
        if end <= start:
            raise RuntimeError(f"第 {i} 段结束要晚于起始：{start:.3f} → {end:.3f}")
        if end - start < 0.05:
            raise RuntimeError(f"第 {i} 段太短：{start:.3f} → {end:.3f}")
        if limit is not None and (start < -0.05 or end > limit + 0.05):
            raise RuntimeError(f"第 {i} 段超出视频长度")
        if peak < start - 0.05 or peak > end + 0.05:
            raise RuntimeError(f"第 {i} 段的峰值要落在起始和结束之间")
        segs.append({
            "start": start,
            "end": end,
            "peak_src": peak,
            "peak_rel": round(max(0.0, peak - start), 3),
        })
    return segs


def export_take(
    folder: Path,
    output: Path,
    take_info: dict,
    starts: list[float],
    ends: list[float],
    peaks: list[float],
    duration: float,
    user: dict,
    cam: str,
    sync: bool = False,
) -> dict:
    if not take_info.get("keep"):
        raise RuntimeError("该条已标记作废，不导出")

    segs = build_segments(starts, ends, peaks, duration)
    user_id = (user.get("user_id") or "s004").strip().lower()
    action = take_info["action"]
    r0 = int(take_info["r_start"])
    clips_dir = processed_dir(output)
    written = []
    label_rows = []
    missing = []

    review = (cam or take_info.get("review_cam") or "").strip().lower()
    if sync:
        targets = [name for name in CAMS if take_info["files"].get(name)]
        if not targets:
            raise RuntimeError("没有可切的视频")
    else:
        if review not in CAMS:
            raise RuntimeError("这条在对照表里没有机位")
        if not take_info["files"].get(review):
            raise RuntimeError(f"{review} 没有这条视频")
        targets = [review]

    cut_cams = []
    for review_cam in targets:
        filename = take_info["files"].get(review_cam)
        try:
            src = resolve_video(folder, filename)
        except RuntimeError:
            if sync:
                missing.append(review_cam)
                continue
            raise
        cut_cams.append(review_cam)
        for idx, seg in enumerate(segs):
            r_no = r0 + idx
            out_name = f"{action}_{user_id}_r{r_no:03d}_{review_cam}.mp4"
            dst = clips_dir / out_name
            cut_clip(src, dst, seg["start"], seg["end"])
            written.append(out_name)
            label_rows.append({
                "文件名称": out_name,
                "动作": take_info["action_cn"],
                "用户编号": user_id,
                "来源": "线下",
                "身高cm": user.get("height_cm", ""),
                "躯干长cm": user.get("torso_cm", ""),
                "支撑/发力侧": take_info["side"],
                "正式峰值时间秒": f"{seg['peak_rel']:.3f}",
                "幅度判断": take_info["range_judgement"],
                "目标做法": take_info["practice"],
                "峰值关键高度cm": "",
                "抖动等级": user.get("shake_level", "0"),
                "抖动类型": user.get("shake_type", "无"),
                "试探次数": user.get("probe_count", "0"),
                "测量方法": user.get("measurement_method", "ruler"),
                "测量可信度": user.get("measurement_confidence", "高"),
                "是否有效": user.get("is_valid", "是"),
                "备注": f"t{take_info['take']:02d};src_peak={seg['peak_src']:.3f};review={review_cam}",
            })

    upsert_label_rows(output, label_rows)
    progress = read_progress(output)
    done = {str(item) for item in progress.get("done", [])}
    for name in cut_cams:
        done.add(progress_key(take_info["take"], name))
    progress["done"] = sorted(done, key=lambda item: (int(str(item).split(":", 1)[0] or 0), str(item)))
    progress.setdefault("marks", {})
    mark_key = str(int(take_info["take"])) if sync else progress_key(take_info["take"], review)
    progress["marks"][mark_key] = {
        "starts": starts,
        "ends": ends,
        "peaks": peaks,
        "clips": written,
    }
    write_progress(output, progress)
    if not written:
        raise RuntimeError(f"{review} 没有切出视频")
    return {
        "clips": written,
        "rows": len(label_rows),
        "segments": segs,
        "missing": missing,
    }
