#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rep 起止帧标注 — 每个 rep 标开始/结束，CSV 与视频同目录，不抽帧。"""

from __future__ import annotations

import csv
import os
import re
import webbrowser
from pathlib import Path
from urllib.parse import unquote

import cv2
from flask import Flask, jsonify, render_template, request, send_file

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "web_templates"
STATIC_DIR = APP_DIR / "web_static"

CSV_HEADERS = [
    "序号",
    "片段名",
    "来源视频",
    "用户编号",
    "动作序号",
    "段内序号",
    "时间戳秒",
    "帧号",
    "动作起始秒",
    "动作结束秒",
    "结束帧号",
    "标签",
    "标注结果",
]

LABEL_CHOICES = ["正确", "错误"]
STYLE_CHOICES = ["刻意", "自然"]

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}

app = Flask(
    __name__,
    template_folder=str(TEMPLATE_DIR),
    static_folder=str(STATIC_DIR),
    static_url_path="/static",
)

cfg = {
    "folder": "",
    "root": "",
    "user_id": "",
}


def normalize_user_id(raw: str) -> str:
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("s"):
        return raw
    if raw.isdigit():
        return f"s{raw.zfill(3)}"
    return raw


from pick_path import clean_user_path, pick_folder


NAME_RE = re.compile(
    r"^(?P<subject>P\d+)_"
    r"(?P<action>A\d{2})_"
    r"(?P<side>[LR])_"
    r"(?P<label>E\d{2})",
    re.IGNORECASE,
)
def video_stem(filename: str) -> str:
    return Path(filename).stem


def take_key(filename: str) -> str:
    """六段式前五段：受试者_动作_侧别_标签_Rep。机位不同则 key 相同。"""
    parts = video_stem(filename).split("_")
    if len(parts) < 6:
        return video_stem(filename).casefold()
    return "_".join(parts[:5]).casefold()


def parse_video_name(filename: str) -> dict[str, str]:
    """从六段式文件名解析受试者、标签代号、正确/错误。"""
    match = NAME_RE.match(video_stem(filename))
    if not match:
        return {
            "subject": (cfg.get("user_id") or "").strip(),
            "error_code": "",
            "label_text": "",
        }
    code = match.group("label").upper()
    return {
        "subject": match.group("subject").upper(),
        "error_code": code,
        "label_text": "正确" if code == "E00" else "错误",
    }


def folder_subject(folder: Path) -> str:
    """从视频文件名或上级目录推断受试者编号，如 P002。"""
    if folder.is_dir():
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in VIDEO_EXTS:
                subject = parse_video_name(path.name).get("subject") or ""
                if subject:
                    return subject
    parent = folder.parent.name if folder.parent else ""
    if re.match(r"^P\d+$", parent, re.IGNORECASE):
        return parent.upper()
    uid = (cfg.get("user_id") or "").strip()
    if re.match(r"^P\d+$", uid, re.IGNORECASE):
        return uid.upper()
    return ""


def labels_csv_filename(folder: Path) -> str:
    """CSV 名：受试者-动作文件夹_labels.csv，如 P002-A10-鸟狗式_labels.csv。"""
    action_part = folder.name
    subject = folder_subject(folder)
    if not subject:
        return f"{action_part}_labels.csv"
    prefix = f"{subject}-"
    if action_part.upper().startswith(prefix.upper()) or action_part.upper().startswith(f"{subject}_"):
        return f"{action_part}_labels.csv"
    return f"{subject}-{action_part}_labels.csv"


def labels_csv_path_for(folder: Path) -> Path:
    """一个动作文件夹一份 CSV。旧的「文件夹名_labels.csv」会自动改名为带受试者编号。"""
    folder = Path(folder)
    new_path = folder / labels_csv_filename(folder)
    old_path = folder / f"{folder.name}_labels.csv"
    if old_path.exists() and old_path.resolve() != new_path.resolve() and not new_path.exists():
        old_path.rename(new_path)
    return new_path


def labels_csv_path(_filename: str = "") -> Path:
    return labels_csv_path_for(Path(cfg["folder"]))


def load_completed_takes_for(folder: Path) -> set[str]:
    """CSV 里已有来源视频的镜头组视为已完成，不再另写 json。"""
    rows = read_csv_rows(labels_csv_path_for(folder))
    takes = {
        take_key(row.get("来源视频") or "")
        for row in rows
        if (row.get("来源视频") or "").strip()
    }
    takes.discard("")
    return takes


def load_completed_takes() -> set[str]:
    if not cfg.get("folder"):
        return set()
    return load_completed_takes_for(Path(cfg["folder"]))


def take_has_labels(filename: str) -> bool:
    return take_key(filename) in load_completed_takes()


def get_video_fps(video_path: Path) -> float:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 30.0
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        return fps if fps > 0 else 30.0
    finally:
        cap.release()


def get_video_frame_count(video_path: Path) -> int:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0
    try:
        return int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    finally:
        cap.release()


def timestamp_to_frame(timestamp_sec: float, fps: float) -> int:
    fps = fps if fps > 0 else 30.0
    return max(0, int(round(timestamp_sec * fps)))


def frame_to_timestamp(frame: int, fps: float) -> float:
    fps = fps if fps > 0 else 30.0
    return frame / fps


def normalize_row(row: dict[str, str]) -> dict[str, str]:
    source = (row.get("来源视频") or row.get("MP4文件名称") or "").strip()
    timestamp = (row.get("时间戳秒") or row.get("正式最高点时间秒") or "").strip()
    parsed = parse_video_name(source)
    result = {
        "序号": (row.get("序号") or "").strip(),
        "片段名": (row.get("片段名") or row.get("图片文件名") or "").strip(),
        "来源视频": source,
        "用户编号": (row.get("用户编号") or "").strip() or parsed["subject"],
        "动作序号": (row.get("动作序号") or "").strip(),
        "段内序号": (row.get("段内序号") or "").strip(),
        "时间戳秒": timestamp,
        "帧号": (row.get("帧号") or "").strip(),
        "动作起始秒": (row.get("动作起始秒") or "").strip(),
        "动作结束秒": (row.get("动作结束秒") or "").strip(),
        "结束帧号": (row.get("结束帧号") or "").strip(),
        "标签": (row.get("标签") or "").strip() or parsed["error_code"],
        "标注结果": (row.get("标注结果") or "").strip() or parsed["label_text"],
    }
    return result


def read_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []
        return [normalize_row(dict(row)) for row in reader]


def write_csv_rows(rows: list[dict[str, str]], csv_path: Path) -> None:
    if not rows:
        if csv_path.exists():
            csv_path.unlink()
        return
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for i, row in enumerate(rows, start=1):
            out = {h: row.get(h, "") for h in CSV_HEADERS}
            out["序号"] = str(i)
            writer.writerow(out)


def row_matches_video(row: dict[str, str], filename: str) -> bool:
    """前五段相同即同一组镜头：CSV 只保留其中一个机位，其余机位也算已标注。"""
    source = (row.get("来源视频") or "").strip()
    if not source:
        return False
    return take_key(source) == take_key(filename)


def rows_for_video(filename: str, rows: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    all_rows = rows if rows is not None else read_csv_rows(labels_csv_path(filename))
    return [r for r in all_rows if row_matches_video(r, filename)]


def action_seq_int(row: dict[str, str]) -> int:
    raw = (row.get("动作序号") or "").strip()
    return int(raw) if raw.isdigit() else 0


def sibling_video_names(filename: str) -> list[str]:
    """前五段相同的视频（同步录制的不同机位）。"""
    folder = Path(cfg.get("folder") or "")
    if not folder.is_dir():
        return [filename]
    key = take_key(filename)
    names: list[str] = []
    for path in folder.iterdir():
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
            continue
        if take_key(path.name) == key:
            names.append(path.name)
    return sorted(names, key=str.lower) or [filename]


def next_action_seq(filename: str) -> int:
    seqs = [action_seq_int(r) for r in rows_for_video(filename)]
    return (max(seqs) if seqs else 0) + 1


def segment_display_name(action_seq: int, style: str) -> str:
    style = style if style in STYLE_CHOICES else "自然"
    return f"a{action_seq:03d}_{style}"


def build_rep_row(
    filename: str,
    action_seq: int,
    start_sec: float,
    start_frame: int,
    end_sec: float,
    end_frame: int,
    stem: str,
) -> dict[str, str]:
    parsed = parse_video_name(filename)
    start_str = f"{start_sec:.3f}"
    end_str = f"{end_sec:.3f}"
    return {
        "序号": str(action_seq),
        "片段名": f"{stem}_rep{action_seq:02d}",
        "来源视频": filename,
        "用户编号": parsed["subject"] or (cfg.get("user_id") or ""),
        "动作序号": str(action_seq),
        "段内序号": "1",
        "时间戳秒": start_str,
        "帧号": str(start_frame),
        "动作起始秒": start_str,
        "动作结束秒": end_str,
        "结束帧号": str(end_frame),
        "标签": parsed["error_code"],
        "标注结果": parsed["label_text"],
    }


def group_video_segments(filename: str, rows: list[dict[str, str]]) -> list[tuple[int, str, list[dict[str, str]]]]:
    video_rows = rows_for_video(filename, rows)
    by_seq: dict[int, list[dict[str, str]]] = {}
    for row in video_rows:
        seq = action_seq_int(row)
        if seq <= 0:
            continue
        by_seq.setdefault(seq, []).append(row)

    segments: list[tuple[int, str, list[dict[str, str]]]] = []
    for seq in sorted(by_seq):
        seg_rows = sorted(
            by_seq[seq],
            key=lambda r: int(r["段内序号"]) if str(r.get("段内序号", "")).isdigit() else 0,
        )
        style = (seg_rows[0].get("动作类型") or "").strip()
        if style not in STYLE_CHOICES:
            style = "自然"
        segments.append((seq, style, seg_rows))
    return segments


def renumber_video_action_seqs(filename: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """将指定视频的动作序号重排为 1..N。"""
    segments = group_video_segments(filename, rows)
    mapping: dict[int, int] = {
        old_seq: new_seq for new_seq, (old_seq, _, _) in enumerate(segments, start=1)
    }
    if not mapping:
        return rows

    result: list[dict[str, str]] = []
    for row in rows:
        if not row_matches_video(row, filename):
            result.append(row)
            continue
        old_seq = action_seq_int(row)
        new_seq = mapping.get(old_seq, old_seq)
        updated = dict(row)
        updated["动作序号"] = str(new_seq)
        style = (updated.get("动作类型") or "").strip()
        if style not in STYLE_CHOICES:
            updated["动作类型"] = "自然"
        result.append(updated)
    return result


def segment_meta_from_rows(
    action_seq: int,
    style: str,
    rows: list[dict[str, str]],
    csv_path: Path,
) -> dict:
    first = rows[0] if rows else {}
    return {
        "action_seq": str(action_seq),
        "style": style,
        "segment_dir": segment_display_name(action_seq, style),
        "label": first.get("标注结果", ""),
        "start_sec": first.get("动作起始秒", ""),
        "end_sec": first.get("动作结束秒", ""),
        "start_frame": first.get("帧号", ""),
        "end_frame": first.get("结束帧号", ""),
        "frame_count": 1,
        "output_dir": str(csv_path),
    }


def rows_to_captures(action_seq: int, style: str, rows: list[dict[str, str]]) -> list[dict]:
    seg_name = segment_display_name(action_seq, style)
    captures = []
    for row in rows:
        img = (row.get("片段名") or row.get("图片文件名") or "").strip() or f"rep_{action_seq}"
        captures.append({
            "seq": row.get("序号", ""),
            "action_seq": row.get("动作序号", str(action_seq)),
            "segment_idx": row.get("段内序号", ""),
            "style": row.get("动作类型", style),
            "segment_dir": seg_name,
            "image_name": img,
            "timestamp": row.get("时间戳秒", ""),
            "frame_number": row.get("帧号", ""),
            "end_sec": row.get("动作结束秒", ""),
            "end_frame": row.get("结束帧号", ""),
            "row": {k: row.get(k, "") for k in CSV_HEADERS},
        })
    return captures


def load_video_segments_and_captures(filename: str) -> tuple[list[dict], list[dict], Path]:
    csv_path = labels_csv_path(filename)
    all_rows = read_csv_rows(csv_path)
    segments_meta: list[dict] = []
    captures: list[dict] = []
    for action_seq, style, seg_rows in group_video_segments(filename, all_rows):
        segments_meta.append(segment_meta_from_rows(action_seq, style, seg_rows, csv_path))
        captures.extend(rows_to_captures(action_seq, style, seg_rows))
    captures.sort(key=lambda c: (
        int(c["action_seq"]) if str(c["action_seq"]).isdigit() else 0,
        int(c["segment_idx"]) if str(c["segment_idx"]).isdigit() else 0,
    ))
    return segments_meta, captures, csv_path


def list_videos(folder: Path) -> list[str]:
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(
        [p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS],
        key=str.lower,
    )


def action_folder_sort_key(folder: Path) -> tuple:
    match = re.match(r"A(\d+)", folder.name, re.IGNORECASE)
    num = int(match.group(1)) if match else 999
    return (num, folder.name.casefold())


def discover_action_folders(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    found = [
        path for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(".") and list_videos(path)
    ]
    return sorted(found, key=action_folder_sort_key)


def folder_progress(folder: Path) -> dict:
    folder = Path(folder)
    videos = list_videos(folder)
    takes = {take_key(name) for name in videos}
    done = load_completed_takes_for(folder) if videos else set()
    remaining = takes - done
    return {
        "name": folder.name,
        "path": str(folder),
        "video_count": len(videos),
        "take_count": len(takes),
        "done_take_count": len(takes & done),
        "done": bool(takes) and not remaining,
    }


def first_incomplete_folder(folders: list[Path]) -> Path | None:
    for folder in folders:
        if not folder_progress(folder)["done"]:
            return folder
    return folders[0] if folders else None


def next_incomplete_folder(current: Path, direction: int = 1) -> Path | None:
    root = cfg.get("root") or ""
    if not root:
        return None
    folders = discover_action_folders(Path(root))
    if not folders:
        return None
    current_res = Path(current).resolve()
    try:
        idx = next(i for i, folder in enumerate(folders) if folder.resolve() == current_res)
    except StopIteration:
        idx = -1 if direction > 0 else len(folders)
    i = idx + direction
    while 0 <= i < len(folders):
        if not folder_progress(folders[i])["done"]:
            return folders[i]
        i += direction
    return None


def apply_selected_path(raw: str) -> None:
    path = Path(clean_user_path(raw))
    if not path.is_dir():
        raise ValueError("视频文件夹不存在")
    videos_here = list_videos(path)
    action_subs = discover_action_folders(path)
    if action_subs and not videos_here:
        cfg["root"] = str(path)
        current = first_incomplete_folder(action_subs)
        cfg["folder"] = str(current) if current else str(path)
        return
    cfg["root"] = ""
    cfg["folder"] = str(path)


def config_payload() -> dict:
    folder = cfg.get("folder") or ""
    root = cfg.get("root") or ""
    videos = list_videos(Path(folder)) if folder else []
    action_folders = []
    if root:
        action_folders = [folder_progress(path) for path in discover_action_folders(Path(root))]
    current_name = Path(folder).name if folder else ""
    return {
        "folder": folder,
        "root": root,
        "folder_name": current_name,
        "user_id": cfg.get("user_id") or "",
        "video_count": len(videos),
        "videos": videos,
        "action_folders": action_folders,
    }


@app.route("/")
def index():
    return render_template(
        "index.html",
        headers=CSV_HEADERS,
        label_choices=LABEL_CHOICES,
        style_choices=STYLE_CHOICES,
    )


def video_annotation_status(filename: str, completed: set[str] | None = None, rows: list[dict[str, str]] | None = None) -> dict:
    """点过「下一个视频」的镜头组才算已标注。"""
    if not cfg["folder"]:
        return {
            "filename": filename,
            "annotated": False,
            "segment_count": 0,
        }
    done = completed if completed is not None else load_completed_takes()
    all_rows = rows if rows is not None else read_csv_rows(labels_csv_path(filename))
    segments = group_video_segments(filename, all_rows)
    return {
        "filename": filename,
        "annotated": take_key(filename) in done,
        "segment_count": len(segments),
    }


@app.get("/api/config")
def get_config():
    return jsonify(config_payload())


@app.get("/api/videos/status")
def videos_status():
    """返回当前文件夹内每个视频是否已标注及动作段数量。"""
    folder = cfg["folder"]
    if not folder or not Path(folder).is_dir():
        return jsonify({"ok": True, "videos": []})
    videos = list_videos(Path(folder))
    completed = load_completed_takes()
    all_rows = read_csv_rows(labels_csv_path(""))
    return jsonify({
        "ok": True,
        "videos": [video_annotation_status(name, completed, all_rows) for name in videos],
        "action_folders": config_payload().get("action_folders") or [],
        "folder": folder,
        "folder_name": Path(folder).name,
        "root": cfg.get("root") or "",
    })


@app.post("/api/config")
def set_config():
    data = request.get_json(force=True) or {}
    if "folder" in data:
        folder = clean_user_path(data["folder"])
        if folder:
            try:
                apply_selected_path(folder)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        else:
            cfg["folder"] = ""
            cfg["root"] = ""
    if "user_id" in data:
        cfg["user_id"] = normalize_user_id(data["user_id"])
    return jsonify({"ok": True, **config_payload()})


@app.post("/api/switch-folder")
def switch_folder():
    if not cfg.get("root"):
        return jsonify({"ok": False, "error": "当前不是受试者大文件夹"}), 400
    data = request.get_json(force=True) or {}
    raw = (data.get("folder") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "缺少动作文件夹"}), 400
    root = Path(cfg["root"]).resolve()
    target = Path(raw)
    if not target.is_absolute():
        target = root / raw
    target = target.resolve()
    allowed = {path.resolve() for path in discover_action_folders(root)}
    if target not in allowed:
        return jsonify({"ok": False, "error": "动作文件夹不在当前受试者目录下"}), 400
    cfg["folder"] = str(target)
    return jsonify({"ok": True, **config_payload()})


@app.post("/api/browse/folder")
def browse_folder():
    try:
        path = pick_folder("选择受试者文件夹（如 P002）或单个动作文件夹")
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    try:
        apply_selected_path(path)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    payload = config_payload()
    return jsonify({"ok": True, "path": payload.get("root") or payload.get("folder") or path, **payload})


@app.post("/api/browse/output")
def browse_output():
    try:
        path = pick_folder("选择标注输出根目录")
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    Path(path).mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True, "path": path})


@app.get("/api/video/<path:filename>")
def serve_video(filename: str):
    folder = Path(cfg["folder"])
    filename = unquote(filename)
    path = (folder / filename).resolve()
    if not str(path).startswith(str(folder.resolve())):
        return "Forbidden", 403
    if not path.is_file():
        return "Not found", 404
    return send_file(path, conditional=True)


@app.get("/api/video/<path:filename>/captures")
def list_captures(filename: str):
    if not cfg["folder"]:
        return jsonify({"ok": False, "error": "请先选择视频文件夹"}), 400
    filename = unquote(filename)
    segments, captures, csv_path = load_video_segments_and_captures(filename)
    return jsonify({
        "ok": True,
        "output_dir": str(csv_path),
        "segments": segments,
        "captures": captures,
    })


@app.get("/api/video/<path:filename>/meta")
def video_meta(filename: str):
    filename = unquote(filename)
    stem = video_stem(filename)
    folder = Path(cfg["folder"])
    video_path = (folder / filename).resolve()
    fps = get_video_fps(video_path) if video_path.is_file() else 30.0
    frame_count = get_video_frame_count(video_path) if video_path.is_file() else 0
    return jsonify({
        "ok": True,
        "stem": stem,
        "fps": fps,
        "frame_count": frame_count,
        "output_dir": str(labels_csv_path(filename)) if cfg["folder"] else "",
    })


@app.post("/api/mark-rep")
def mark_rep():
    """写入一个 Rep 的起止帧，不抽中间帧。"""
    if not cfg["folder"]:
        return jsonify({"ok": False, "error": "请先选择视频文件夹"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    try:
        start_sec = float(data.get("start_sec", 0))
        end_sec = float(data.get("end_sec", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "起止时间无效"}), 400
    if start_sec < 0 or end_sec < 0:
        return jsonify({"ok": False, "error": "起止时间无效"}), 400
    if abs(end_sec - start_sec) < 1e-6:
        return jsonify({"ok": False, "error": "起始点和结束点不能相同"}), 400
    if start_sec > end_sec:
        start_sec, end_sec = end_sec, start_sec

    folder = Path(cfg["folder"])
    video_path = (folder / filename).resolve()
    if not filename or not str(video_path).startswith(str(folder.resolve())) or not video_path.is_file():
        return jsonify({"ok": False, "error": "视频不存在"}), 400

    fps = get_video_fps(video_path)
    total_frames = get_video_frame_count(video_path)
    max_frame = max(total_frames - 1, 0)
    start_frame = min(timestamp_to_frame(start_sec, fps), max_frame)
    end_frame = min(timestamp_to_frame(end_sec, fps), max_frame)
    if start_frame > end_frame:
        start_frame, end_frame = end_frame, start_frame
    if start_frame == end_frame:
        return jsonify({"ok": False, "error": "起始帧和结束帧不能相同"}), 400
    start_sec = frame_to_timestamp(start_frame, fps)
    end_sec = frame_to_timestamp(end_frame, fps)

    action_seq = next_action_seq(filename)
    csv_path = labels_csv_path(filename)
    all_rows = read_csv_rows(csv_path)
    new_row = build_rep_row(
        filename=filename,
        action_seq=action_seq,
        start_sec=start_sec,
        start_frame=start_frame,
        end_sec=end_sec,
        end_frame=end_frame,
        stem=video_stem(filename),
    )
    all_rows.append(new_row)
    write_csv_rows(all_rows, csv_path)

    siblings = sibling_video_names(filename)
    segments, _all_captures, csv_path = load_video_segments_and_captures(filename)
    captures = rows_to_captures(action_seq, "", [new_row])
    return jsonify({
        "ok": True,
        "action_seq": str(action_seq),
        "start_frame": start_frame,
        "end_frame": end_frame,
        "start_sec": f"{start_sec:.3f}",
        "end_sec": f"{end_sec:.3f}",
        "synced_count": len(siblings),
        "synced_videos": siblings,
        "output_dir": str(csv_path),
        "csv_name": csv_path.name,
        "captures": captures,
        "segments": segments,
    })


@app.post("/api/complete-take")
def complete_take():
    """点「下一个视频」时把当前镜头组标为完成，同组其余机位一并跳过。"""
    if not cfg["folder"]:
        return jsonify({"ok": False, "error": "请先选择视频文件夹"}), 400
    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "缺少视频"}), 400
    if not take_has_labels(filename):
        return jsonify({"ok": False, "error": "请先至少标注一个 rep，再点下一个视频"}), 400
    siblings = sibling_video_names(filename)
    current_folder = Path(cfg["folder"])
    progress = folder_progress(current_folder)
    nxt = next_incomplete_folder(current_folder, 1) if progress["done"] else None
    return jsonify({
        "ok": True,
        "filename": filename,
        "take_key": take_key(filename),
        "synced_count": len(siblings),
        "synced_videos": siblings,
        "folder_done": progress["done"],
        "next_folder": str(nxt) if nxt else "",
        "next_folder_name": nxt.name if nxt else "",
        "action_folders": config_payload().get("action_folders") or [],
    })


@app.post("/api/update-segment-label")
def update_segment_label():
    if not cfg["folder"]:
        return jsonify({"ok": False, "error": "请先选择视频文件夹"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    action_seq = (data.get("action_seq") or "").strip()
    label = (data.get("label") or "").strip()

    if not filename or not action_seq.isdigit():
        return jsonify({"ok": False, "error": "缺少视频或动作序号"}), 400
    if label not in LABEL_CHOICES:
        return jsonify({"ok": False, "error": "标注结果只能是：正确 或 错误"}), 400

    target_seq = int(action_seq)
    csv_path = labels_csv_path(filename)
    all_rows = read_csv_rows(csv_path)
    found = False
    for row in all_rows:
        if row_matches_video(row, filename) and action_seq_int(row) == target_seq:
            row["标注结果"] = label
            found = True
    if not found:
        return jsonify({"ok": False, "error": "该动作段没有标注记录"}), 404

    write_csv_rows(all_rows, csv_path)
    segments, captures, csv_path = load_video_segments_and_captures(filename)
    return jsonify({
        "ok": True,
        "action_seq": action_seq,
        "label": label,
        "output_dir": str(csv_path),
        "segments": segments,
        "captures": captures,
    })


@app.post("/api/delete-segment")
def delete_segment():
    if not cfg["folder"]:
        return jsonify({"ok": False, "error": "请先选择视频文件夹"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    action_seq = (data.get("action_seq") or "").strip()
    if not filename or not action_seq.isdigit():
        return jsonify({"ok": False, "error": "缺少视频或动作序号"}), 400

    target_seq = int(action_seq)
    csv_path = labels_csv_path(filename)
    all_rows = read_csv_rows(csv_path)
    kept: list[dict[str, str]] = []
    deleted_count = 0
    for row in all_rows:
        if row_matches_video(row, filename) and action_seq_int(row) == target_seq:
            deleted_count += 1
            continue
        kept.append(row)

    if deleted_count == 0:
        return jsonify({"ok": False, "error": "动作段不存在"}), 404

    kept = renumber_video_action_seqs(filename, kept)
    write_csv_rows(kept, csv_path)

    segments, captures, csv_path = load_video_segments_and_captures(filename)
    return jsonify({
        "ok": True,
        "deleted_count": deleted_count,
        "output_dir": str(csv_path),
        "segments": segments,
        "captures": captures,
    })


@app.post("/api/delete-capture")
def delete_capture():
    if not cfg["folder"]:
        return jsonify({"ok": False, "error": "请先选择视频文件夹"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    image_name = (data.get("image_name") or "").strip()
    action_seq = (data.get("action_seq") or "").strip()
    if not filename or not image_name:
        return jsonify({"ok": False, "error": "缺少视频或片段名"}), 400

    target_seq = int(action_seq) if action_seq.isdigit() else None
    csv_path = labels_csv_path(filename)
    all_rows = read_csv_rows(csv_path)
    kept: list[dict[str, str]] = []
    deleted = False
    deleted_action_seq: int | None = None

    for row in all_rows:
        if not row_matches_video(row, filename):
            kept.append(row)
            continue
        if (row.get("片段名") or row.get("图片文件名") or "").strip() != image_name:
            kept.append(row)
            continue
        if target_seq is not None and action_seq_int(row) != target_seq:
            kept.append(row)
            continue
        deleted = True
        deleted_action_seq = action_seq_int(row)

    if not deleted:
        return jsonify({"ok": False, "error": "未找到对应采样记录"}), 404

    # 若该动作段已无剩余行，重排该视频动作序号
    if deleted_action_seq is not None:
        still_has = any(
            row_matches_video(r, filename) and action_seq_int(r) == deleted_action_seq
            for r in kept
        )
        if not still_has:
            kept = renumber_video_action_seqs(filename, kept)

    write_csv_rows(kept, csv_path)
    segments, captures, csv_path = load_video_segments_and_captures(filename)
    return jsonify({
        "ok": True,
        "output_dir": str(csv_path),
        "segments": segments,
        "captures": captures,
    })


def open_browser(port: int) -> None:
    url = f"http://127.0.0.1:{port}"
    try:
        webbrowser.open(url)
    except Exception:
        print(f"请手动打开: {url}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8766"))
    print(f"\n  Rep 起止帧标注工具（Web 版）")
    print(f"  前五段相同的机位会同步写入同一组起止帧")
    print(f"  浏览器访问: http://127.0.0.1:{port}\n")
    open_browser(port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
