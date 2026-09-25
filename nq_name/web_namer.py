#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线下采集视频快速命名工具。"""

from __future__ import annotations

import mimetypes
import os
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import unquote

from flask import Flask, jsonify, render_template, request, send_file

# 确保常见视频类型能被正确识别（浏览器播放依赖 Content-Type）
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/x-m4v", ".m4v")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/x-msvideo", ".avi")

from nq_name.schema import (
    compose_stem,
    normalize_subject,
    parse_stem,
    schema_payload,
)
from nq_name.rotate_media import (
    css_degrees_from_ccw,
    export_with_display_rotation,
    normalize_rotation_deg,
    probe_display_rotation,
)

APP_DIR = Path(__file__).resolve().parent
# 采集现场以 .mov 为主，同时兼容其它常见格式
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}
SKIP_DIR_NAME = "作废"
RAW_STAMP_RE = re.compile(r"^A\d{3}_(\d{8})_C(\d+)", re.IGNORECASE)

app = Flask(
    __name__,
    template_folder=str(APP_DIR / "web_templates"),
    static_folder=str(APP_DIR / "web_static"),
    static_url_path="/static",
)

cfg = {
    "folder": "",
    "subject": "P001",
    "sync_enabled": True,
}

# P001-0度 / P001-90度 … → 受试者 + 机位
CAMERA_FOLDER_RE = re.compile(
    r"^(?P<subject>P\d+)-(?P<angle>0|90|180|270)度$",
    re.IGNORECASE,
)
ANGLE_TO_CAMERA = {"0": "C0", "90": "C90", "180": "C180", "270": "C270"}
CAMERA_TO_ANGLE = {v: k for k, v in ANGLE_TO_CAMERA.items()}
CLIP_NO_RE = re.compile(r"_C(\d+)", re.IGNORECASE)


from pick_path import clean_user_path, pick_folder


def clip_stamp(name: str) -> str:
    """原片时间戳，如 A001_08131835_C005 → 08131835。"""
    match = RAW_STAMP_RE.match(Path(name).name)
    return match.group(1) if match else ""


def clip_sort_key(name: str) -> tuple:
    """未命名原片按拍摄时间戳、再按相机片段号排序；已命名的排后面。"""
    parsed = parse_stem(Path(name).stem)
    if parsed is not None:
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
    return sorted(files, key=clip_sort_key)


def parse_camera_folder(path: Path) -> dict | None:
    match = CAMERA_FOLDER_RE.match(path.name)
    if not match:
        return None
    angle = match.group("angle")
    return {
        "subject": normalize_subject(match.group("subject")),
        "angle": angle,
        "camera": ANGLE_TO_CAMERA[angle],
        "folder": str(path.resolve()),
        "name": path.name,
    }


def discover_camera_set(path: Path) -> list[dict]:
    """选中受试者根目录或任一机位文件夹时，找出同组的 0/90/180/270。"""
    path = path.expanduser().resolve()
    if not path.is_dir():
        return []
    parsed = parse_camera_folder(path)
    parent = path.parent if parsed else path
    found: list[dict] = []
    if parent.is_dir():
        for child in parent.iterdir():
            info = parse_camera_folder(child)
            if info:
                found.append(info)
    found.sort(key=lambda c: int(c["angle"]))
    return found


def apply_selected_path(raw: str) -> None:
    """写入所选路径：若是受试者根目录则默认看 0° 机位。"""
    folder = clean_user_path(raw)
    if not folder:
        cfg["folder"] = ""
        return
    path = Path(folder).expanduser().resolve()
    cameras = discover_camera_set(path)
    parsed = parse_camera_folder(path)
    if parsed:
        cfg["folder"] = str(path)
        if parsed["subject"]:
            cfg["subject"] = parsed["subject"]
        return
    if cameras:
        preferred = next((c for c in cameras if c["camera"] == "C0"), cameras[0])
        cfg["folder"] = preferred["folder"]
        if preferred["subject"]:
            cfg["subject"] = preferred["subject"]
        return
    cfg["folder"] = str(path)


def camera_set_payload() -> list[dict]:
    folder = cfg["folder"]
    if not folder:
        return []
    cameras = discover_camera_set(Path(folder))
    items = []
    for info in cameras:
        names = list_videos(Path(info["folder"]))
        unnamed = sum(1 for n in names if parse_stem(Path(n).stem) is None)
        items.append(
            {
                **info,
                "video_count": len(names),
                "unnamed_count": unnamed,
                "watching": Path(info["folder"]).resolve() == Path(folder).resolve(),
            }
        )
    return items


def unnamed_paths(folder: Path) -> list[Path]:
    return [
        folder / name
        for name in list_videos(folder)
        if parse_stem(Path(name).stem) is None
    ]


def sibling_for_unnamed(old_path: Path, camera_info: dict) -> Path | None:
    """当前未命名文件在本机位未命名列表中的第 N 条，对应到另一机位的第 N 条。"""
    current_unnamed = unnamed_paths(old_path.parent)
    try:
        index = next(i for i, p in enumerate(current_unnamed) if p.name == old_path.name)
    except StopIteration:
        return None
    other = unnamed_paths(Path(camera_info["folder"]))
    if index >= len(other):
        return None
    return other[index]


def slot_files(old_path: Path, parsed_old: dict | None, unnamed_index: int | None) -> list[dict]:
    """当前条在各机位对应的文件（含正在看的机位）。"""
    cameras = camera_set_payload()
    rows = []
    for info in cameras:
        folder = Path(info["folder"])
        is_current = folder.resolve() == old_path.parent.resolve()
        sibling = old_path if is_current else None
        if sibling is None and parsed_old:
            sibling = sibling_for_named(parsed_old, info)
        if sibling is None and unnamed_index is not None:
            others = unnamed_paths(folder)
            if unnamed_index < len(others):
                sibling = others[unnamed_index]
        if sibling is None:
            rows.append(
                {
                    "camera": info["camera"],
                    "name": info["name"],
                    "folder": info["folder"],
                    "filename": "",
                    "path": None,
                    "missing": True,
                    "named": False,
                    "current": is_current,
                    "size": 0,
                    "stamp": "",
                }
            )
            continue
        rows.append(
            {
                "camera": info["camera"],
                "name": info["name"],
                "folder": info["folder"],
                "filename": sibling.name,
                "path": sibling,
                "missing": False,
                "named": parse_stem(sibling.stem) is not None,
                "current": is_current,
                "size": sibling.stat().st_size if sibling.is_file() else 0,
                "stamp": clip_stamp(sibling.name),
            }
        )
    return rows


def unnamed_index_of(path: Path) -> int | None:
    try:
        return next(i for i, p in enumerate(unnamed_paths(path.parent)) if p.name == path.name)
    except StopIteration:
        return None


def move_to_skip(path: Path) -> Path:
    dest_dir = path.parent / SKIP_DIR_NAME
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        dest = dest_dir / f"{path.stem}__skip{path.suffix}"
        n = 2
        while dest.exists():
            dest = dest_dir / f"{path.stem}__skip{n}{path.suffix}"
            n += 1
    path.rename(dest)
    return dest


def sibling_for_named(parsed: dict, camera_info: dict) -> Path | None:
    """已命名文件：按前五段（受试者/动作/侧别/标签/Rep）找另一机位的同条。"""
    folder = Path(camera_info["folder"])
    for name in list_videos(folder):
        other = parse_stem(Path(name).stem)
        if not other:
            continue
        if (
            other["subject"] == parsed["subject"]
            and other["action"] == parsed["action"]
            and other["side"] == parsed["side"]
            and other["error_code"] == parsed["error_code"]
            and other["rep"] == parsed["rep"]
            and other["camera"] == camera_info["camera"]
        ):
            return folder / name
    return None


def apply_rename(
    old_path: Path,
    stem: str,
    desired_ccw: int | None,
) -> dict:
    new_name = f"{stem}{old_path.suffix.lower()}"
    new_path = old_path.with_name(new_name)

    if desired_ccw is None:
        try:
            desired_ccw = normalize_rotation_deg(probe_display_rotation(old_path))
        except Exception:
            desired_ccw = 0
    else:
        desired_ccw = normalize_rotation_deg(desired_ccw)

    try:
        current_ccw = normalize_rotation_deg(probe_display_rotation(old_path))
    except Exception:
        current_ccw = 0

    same_path = new_path.resolve() == old_path.resolve()
    need_orient = desired_ccw != current_ccw

    if same_path and not need_orient:
        return {
            "ok": True,
            "unchanged": True,
            "old_name": old_path.name,
            "new_name": new_name,
            "folder": str(old_path.parent),
            "display_rotation_ccw": desired_ccw,
            "css_degrees": css_degrees_from_ccw(desired_ccw),
        }

    if same_path and need_orient:
        export_with_display_rotation(old_path, old_path, desired_ccw)
        return {
            "ok": True,
            "unchanged": False,
            "old_name": old_path.name,
            "new_name": new_name,
            "folder": str(old_path.parent),
            "display_rotation_ccw": desired_ccw,
            "css_degrees": css_degrees_from_ccw(desired_ccw),
        }

    if new_path.exists():
        raise FileExistsError(f"目标文件已存在：{new_name}")

    if need_orient:
        export_with_display_rotation(old_path, new_path, desired_ccw)
        old_path.unlink(missing_ok=True)
    else:
        old_path.rename(new_path)

    return {
        "ok": True,
        "unchanged": False,
        "old_name": old_path.name,
        "new_name": new_name,
        "folder": str(new_path.parent),
        "display_rotation_ccw": desired_ccw,
        "css_degrees": css_degrees_from_ccw(desired_ccw),
    }


def _is_under(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def safe_video_path(filename: str) -> Path:
    folder = Path(cfg["folder"]).expanduser().resolve()
    filename = unquote(filename)
    # 只允许单层文件名，防止路径穿越
    name = Path(filename).name
    path = (folder / name).resolve()
    if not _is_under(path, folder):
        raise PermissionError("Forbidden")
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/schema")
def get_schema():
    return jsonify(schema_payload())


@app.get("/api/config")
def get_config():
    folder = cfg["folder"]
    videos = list_videos(Path(folder)) if folder else []
    items = []
    unnamed_names = [n for n in videos if parse_stem(Path(n).stem) is None]
    for name in videos:
        parsed = parse_stem(Path(name).stem)
        items.append(
            {
                "filename": name,
                "named": parsed is not None,
                "parsed": parsed,
                "stamp": clip_stamp(name),
                "unnamed_index": unnamed_names.index(name) if name in unnamed_names else None,
            }
        )
    cameras = camera_set_payload()
    watching = next((c for c in cameras if c["watching"]), None)
    return jsonify(
        {
            "folder": folder,
            "subject": cfg["subject"],
            "video_count": len(videos),
            "videos": items,
            "sync_enabled": bool(cfg.get("sync_enabled")),
            "sync_available": len(cameras) > 1,
            "cameras": cameras,
            "watch_camera": watching["camera"] if watching else "",
        }
    )


@app.post("/api/config")
def set_config():
    data = request.get_json(force=True) or {}
    if "folder" in data:
        folder = clean_user_path(data.get("folder") or "")
        if folder and not Path(folder).is_dir():
            return jsonify({"ok": False, "error": "视频文件夹不存在"}), 400
        apply_selected_path(folder)
    if "subject" in data:
        cfg["subject"] = normalize_subject(data.get("subject") or "") or cfg["subject"]
    if "sync_enabled" in data:
        cfg["sync_enabled"] = bool(data.get("sync_enabled"))
    return jsonify({"ok": True, "subject": cfg["subject"], "folder": cfg["folder"]})


@app.post("/api/browse/folder")
def browse_folder():
    try:
        path = pick_folder("选择受试者文件夹（如 P001）或其中一个机位文件夹")
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    apply_selected_path(path.rstrip("/"))
    return jsonify({"ok": True, "path": cfg["folder"]})


@app.post("/api/watch-camera")
def watch_camera():
    data = request.get_json(force=True) or {}
    camera = (data.get("camera") or "").strip().upper()
    cameras = camera_set_payload()
    target = next((c for c in cameras if c["camera"] == camera), None)
    if not target:
        return jsonify({"ok": False, "error": f"未找到机位 {camera}"}), 400
    cfg["folder"] = target["folder"]
    return jsonify({"ok": True, "folder": cfg["folder"], "camera": camera})


@app.get("/api/video/<path:filename>")
def serve_video(filename: str):
    try:
        path = safe_video_path(filename)
    except PermissionError:
        return "Forbidden", 403
    except FileNotFoundError:
        return "Not found", 404
    # 与「视频处理-序列-lishi」一致：按扩展名猜测 MIME + Range，浏览器内联播放
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if path.suffix.lower() == ".mov" and mime == "application/octet-stream":
        mime = "video/quicktime"
    return send_file(path, mimetype=mime, conditional=True)


@app.post("/api/open-native")
def open_native():
    """用 macOS 默认应用（通常 QuickTime）打开当前视频，避免浏览器播不了 mov。"""
    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "缺少文件名"}), 400
    if not cfg["folder"]:
        return jsonify({"ok": False, "error": "尚未选择视频文件夹"}), 400
    try:
        path = safe_video_path(filename)
    except PermissionError:
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True, "path": str(path)})


@app.get("/api/video/<path:filename>/orientation")
def video_orientation(filename: str):
    try:
        path = safe_video_path(filename)
        ccw = normalize_rotation_deg(probe_display_rotation(path))
    except PermissionError:
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify(
        {
            "ok": True,
            "filename": path.name,
            "display_rotation_ccw": ccw,
            "css_degrees": css_degrees_from_ccw(ccw),
        }
    )


@app.post("/api/rename")
def rename_video():
    """
    按六段式命名另存为新文件。
    若带 display_rotation_ccw，则按 QuickTime 方式只改方向元数据后写出新文件，并删除原文件。
    开启 sync_cameras 时，按「未命名列表的同一序号」同步改其他机位（只改名，不改旋转）。
    """
    data = request.get_json(force=True) or {}
    old_name = (data.get("filename") or "").strip()
    if not old_name:
        return jsonify({"ok": False, "error": "缺少原文件名"}), 400
    if not cfg["folder"]:
        return jsonify({"ok": False, "error": "尚未选择视频文件夹"}), 400

    try:
        old_path = safe_video_path(old_name)
        subject = data.get("subject") or cfg["subject"]
        action = data.get("action")
        side = data.get("side")
        error_code = data.get("error_code")
        rep = data.get("rep")
        camera = data.get("camera")
        stem = compose_stem(
            subject=subject,
            action=action,
            side=side,
            error_code=error_code,
            rep=rep,
            camera=camera,
        )
    except (PermissionError, FileNotFoundError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if "display_rotation_ccw" in data and data.get("display_rotation_ccw") is not None:
        try:
            desired_ccw = normalize_rotation_deg(float(data.get("display_rotation_ccw")))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "旋转角度无效"}), 400
    else:
        desired_ccw = None

    sync_flag = data.get("sync_cameras")
    do_sync = cfg.get("sync_enabled", True) if sync_flag is None else bool(sync_flag)
    cameras = camera_set_payload()
    watching = next((c for c in cameras if c["watching"]), None)
    if watching and not camera:
        camera = watching["camera"]

    parsed_old = parse_stem(Path(old_name).stem)
    unnamed_index = unnamed_index_of(old_path)
    slot = slot_files(old_path, parsed_old, unnamed_index)

    try:
        result = apply_rename(old_path, stem, desired_ccw)
    except FileExistsError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    synced = []
    skipped = []
    if do_sync and len(cameras) > 1:
        for row in slot:
            if row["current"] or row["missing"] or row["path"] is None:
                if (not row["current"]) and row["missing"]:
                    skipped.append({"camera": row["camera"], "reason": "找不到对应序号的文件"})
                continue
            try:
                other_stem = compose_stem(
                    subject=subject,
                    action=action,
                    side=side,
                    error_code=error_code,
                    rep=rep,
                    camera=row["camera"],
                )
                synced.append(apply_rename(row["path"], other_stem, None))
            except FileExistsError as exc:
                skipped.append({"camera": row["camera"], "reason": str(exc)})
            except Exception as exc:
                skipped.append({"camera": row["camera"], "reason": str(exc)})

    cfg["subject"] = normalize_subject(subject)
    return jsonify(
        {
            "ok": True,
            "unchanged": result["unchanged"],
            "old_name": result["old_name"],
            "new_name": result["new_name"],
            "display_rotation_ccw": result["display_rotation_ccw"],
            "css_degrees": result["css_degrees"],
            "synced": synced,
            "skipped": skipped,
        }
    )


@app.get("/api/sync-preview")
def sync_preview():
    """当前文件在各机位对应的第 N 条（未命名按序号，已命名按前五段）。"""
    filename = (request.args.get("filename") or "").strip()
    if not filename or not cfg["folder"]:
        return jsonify({"ok": True, "peers": [], "unnamed_index": None})
    try:
        old_path = safe_video_path(filename)
    except (PermissionError, FileNotFoundError):
        return jsonify({"ok": True, "peers": [], "unnamed_index": None})

    parsed_old = parse_stem(Path(filename).stem)
    unnamed_index = unnamed_index_of(old_path)
    rows = slot_files(old_path, parsed_old, unnamed_index)
    peers = [{k: v for k, v in row.items() if k != "path"} for row in rows]
    sizes = [p["size"] for p in peers if p["size"]]
    stamps = {p["stamp"] for p in peers if p["stamp"]}
    size_warn = bool(sizes) and min(sizes) > 0 and max(sizes) / min(sizes) > 3
    stamp_warn = len(stamps) > 1
    return jsonify(
        {
            "ok": True,
            "unnamed_index": unnamed_index,
            "size_warn": size_warn,
            "stamp_warn": stamp_warn,
            "peers": peers,
        }
    )


@app.post("/api/skip")
def skip_slot():
    """本条作废：移入各机位文件夹下的「作废」子目录，同步开启时四个机位同一序号一起移走。"""
    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "缺少原文件名"}), 400
    if not cfg["folder"]:
        return jsonify({"ok": False, "error": "尚未选择视频文件夹"}), 400
    try:
        old_path = safe_video_path(filename)
    except (PermissionError, FileNotFoundError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    parsed_old = parse_stem(Path(filename).stem)
    unnamed_index = unnamed_index_of(old_path)
    sync_flag = data.get("sync_cameras")
    do_sync = cfg.get("sync_enabled", True) if sync_flag is None else bool(sync_flag)
    rows = slot_files(old_path, parsed_old, unnamed_index)
    if not do_sync:
        rows = [row for row in rows if row["current"]]

    moved = []
    missing = []
    for row in rows:
        if row["missing"] or row["path"] is None:
            missing.append({"camera": row["camera"], "reason": "找不到对应序号的文件"})
            continue
        dest = move_to_skip(row["path"])
        moved.append(
            {
                "camera": row["camera"],
                "old_name": row["filename"],
                "dest": str(dest),
            }
        )
    return jsonify({"ok": True, "moved": moved, "missing": missing})


@app.post("/api/preview-name")
def preview_name():
    data = request.get_json(force=True) or {}
    ext = (data.get("ext") or ".mp4").lower()
    if not ext.startswith("."):
        ext = f".{ext}"
    try:
        stem = compose_stem(
            subject=data.get("subject") or cfg["subject"],
            action=data.get("action"),
            side=data.get("side"),
            error_code=data.get("error_code"),
            rep=data.get("rep"),
            camera=data.get("camera"),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc), "filename": ""})
    return jsonify({"ok": True, "filename": f"{stem}{ext}"})


def open_browser(port: int) -> None:
    url = f"http://127.0.0.1:{port}"

    def _open() -> None:
        # mov（尤其 iPhone/HEVC）在 Chrome 里经常无法播放，macOS 优先用 Safari
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", "Safari", url], check=False)
        else:
            webbrowser.open(url)

    threading.Timer(0.6, _open).start()


def main() -> None:
    port = int(os.environ.get("PORT", "8770"))
    open_browser(port)
    print(f"视频快速命名工具: http://127.0.0.1:{port}")
    print("提示：视频多为 .mov，已优先用 Safari 打开；若仍黑屏可点「系统播放器打开」。")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
