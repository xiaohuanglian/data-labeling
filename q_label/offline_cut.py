#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线下采集：按 checklist 切分动态动作，并写出 labels.csv。"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
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


def resolve_cam_session(folder: Path) -> tuple[Path, Path, str]:
    """必须选机位文件夹（如 S004/现场原视频/c90）。对照表在会话根目录，只读。"""
    current = Path(folder).expanduser().resolve()
    if current.name not in {"c0", "c90", "c180", "c270"}:
        raise RuntimeError(
            "请选择某一个机位文件夹，例如 D:\\采集\\S004\\现场原视频\\c90。"
            "对照表.csv 在 S004 根目录，只用来读视频信息，不会写入。"
        )
    root = current.parent
    if not (root / "对照表.csv").is_file() and (root.parent / "对照表.csv").is_file():
        root = root.parent
    lookup = root / "对照表.csv"
    if not lookup.is_file():
        raise RuntimeError(f"找不到对照表.csv（只读，不会改它）：{lookup}")
    return root, current, current.name


def resolve_session_root(folder: Path) -> Path:
    root, _, _ = resolve_cam_session(folder)
    return root


def load_takes(folder: Path) -> list[dict]:
    root = resolve_session_root(folder)
    csv_path = root / "对照表.csv"

    grouped: dict[int, dict] = {}
    files: dict[int, dict[str, str]] = defaultdict(dict)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
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
                "rep_count": 1 if kind == "static" else count,
                "kind": kind,
                "review_cam": "",
            }

    takes = []
    for take in sorted(grouped):
        item = grouped[take]
        item["files"] = files.get(take, {})
        if item["keep"] and len(item["files"]) < 4:
            missing = [c for c in CAMS if c not in item["files"]]
            item["note"] = f"缺机位: {','.join(missing)}"
        takes.append(item)
    return takes


def resolve_video(folder: Path, filename: str) -> Path:
    if not filename:
        raise RuntimeError("文件名为空")
    root, cam_dir, _ = resolve_cam_session(folder)
    src_root = originals_root(root)
    # 先在所选机位里找，再兜底到其它机位/作废（只读，不改对照表）
    candidates = [cam_dir / filename]
    for cam in CAMS:
        candidates.append(src_root / cam / filename)
        candidates.append(root / cam / filename)
    candidates.append(src_root / "作废" / filename)
    candidates.append(root / "作废" / filename)
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path
    raise RuntimeError(f"找不到视频：{filename}")


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


def build_segments(kind: str, starts: list[float], peaks: list[float], duration: float) -> list[dict]:
    if kind == "static":
        if len(peaks) != 1:
            raise RuntimeError("静态动作只标 1 个正式峰值时间")
        peak = float(peaks[0])
        if peak < 0 or peak > duration + 0.05:
            raise RuntimeError("峰值时间超出视频长度")
        return [{"start": 0.0, "end": duration, "peak_src": peak, "peak_rel": round(peak, 3)}]

    if len(starts) != len(peaks) or not starts:
        raise RuntimeError(f"动态动作需要成对的起始点和峰值，当前起始 {len(starts)}、峰值 {len(peaks)}")
    ordered = sorted(float(x) for x in starts)
    segs = []
    unused = sorted(float(x) for x in peaks)
    for i, start in enumerate(ordered):
        end = ordered[i + 1] if i + 1 < len(ordered) else duration
        if end - start < 0.25:
            raise RuntimeError(f"第 {i + 1} 段太短：{start:.3f} → {end:.3f}")
        match = None
        for peak in unused:
            if start - 0.02 <= peak < end:
                match = peak
                break
        if match is None:
            raise RuntimeError(f"第 {i + 1} 段（{start:.3f}–{end:.3f}）没有峰值")
        unused.remove(match)
        segs.append({
            "start": start,
            "end": end,
            "peak_src": match,
            "peak_rel": round(match - start, 3),
        })
    if unused:
        raise RuntimeError("有峰值落在任何起始段之外")
    return segs


def export_take(
    folder: Path,
    output: Path,
    take_info: dict,
    starts: list[float],
    peaks: list[float],
    duration: float,
    user: dict,
    cam: str,
) -> dict:
    if not take_info.get("keep"):
        raise RuntimeError("该条已标记作废，不导出")

    kind = take_info["kind"]
    expected = int(take_info["rep_count"])
    if kind == "dynamic" and len(starts) != expected:
        raise RuntimeError(f"这条应标 {expected} 个起始点，现在 {len(starts)} 个")
    if kind == "static" and len(peaks) != 1:
        raise RuntimeError("静态动作只需 1 个峰值")
    if kind == "dynamic" and len(peaks) != expected:
        raise RuntimeError(f"这条应标 {expected} 个峰值，现在 {len(peaks)} 个")

    segs = build_segments(kind, starts, peaks, duration)
    user_id = (user.get("user_id") or "s004").strip().lower()
    action = take_info["action"]
    r0 = int(take_info["r_start"])
    clips_dir = processed_dir(output)
    written = []
    label_rows = []
    missing = []

    # 四个机位同步录制：只标一个视角，同一组时间点切四机
    for cam_name in CAMS:
        filename = take_info["files"].get(cam_name)
        if not filename:
            missing.append(cam_name)
            continue
        src = resolve_video(folder, filename)
        for idx, seg in enumerate(segs):
            r_no = r0 + idx
            out_name = f"{action}_{user_id}_r{r_no:03d}_{cam_name}.mp4"
            dst = clips_dir / out_name
            if kind == "static":
                remux_whole(src, dst)
            else:
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
                "备注": f"t{take_info['take']:02d};src_peak={seg['peak_src']:.3f};review={cam}",
            })

    upsert_label_rows(output, label_rows)
    progress = read_progress(output)
    done = set(str(x) for x in progress.get("done", []))
    done.add(str(take_info["take"]))
    progress["done"] = sorted(done, key=lambda x: int(x))
    progress.setdefault("marks", {})
    progress["marks"][str(take_info["take"])] = {
        "starts": starts,
        "peaks": peaks,
        "clips": written,
    }
    write_progress(output, progress)
    if not written:
        raise RuntimeError("四个机位都没有可切的源视频")
    return {
        "clips": written,
        "rows": len(label_rows),
        "segments": segs,
        "missing": missing,
    }
