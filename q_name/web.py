# -*- coding: utf-8 -*-
"""量化命名页：看一条，四机同步写成 动作_s编号_t序号_机位，并写对照表。"""

from __future__ import annotations

import mimetypes
import subprocess
from pathlib import Path
from urllib.parse import unquote

from flask import Flask, jsonify, render_template, request, send_file

from nq_name.rotate_media import (
    css_degrees_from_ccw,
    normalize_rotation_deg,
    probe_display_rotation,
)
from nq_name.web_namer import apply_rename, move_to_skip
from q_name.logic import (
    action_by_code,
    assert_take_free,
    build_rows,
    discover,
    list_videos,
    lookup_path,
    next_take,
    normalize_r_range,
    normalize_side,
    normalize_take,
    normalize_user,
    parse_coarse,
    read_lookup,
    replace_take_rows,
    schema_payload,
    slot_files,
    write_lookup,
    compose_stem,
)

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("video/x-m4v", ".m4v")

APP_DIR = Path(__file__).resolve().parent
app = Flask(
    __name__,
    template_folder=str(APP_DIR / "web_templates"),
    static_folder=str(APP_DIR / "web_static"),
    static_url_path="/static",
)

cfg = {
    "folder": "",
    "session_root": "",
    "watch_cam": "",
    "sync_enabled": True,
    "user": "s004",
}


from pick_path import clean_user_path, pick_folder


def apply_selected_path(raw: str) -> None:
    found = discover(clean_user_path(raw))
    cfg["folder"] = str(found["watch_folder"])
    cfg["session_root"] = str(found["session_root"])
    cfg["watch_cam"] = found["watch_cam"]


def camera_pairs() -> list[tuple[str, Path]]:
    if not cfg["folder"]:
        return []
    found = discover(cfg["folder"])
    cfg["session_root"] = str(found["session_root"])
    return found["cameras"]


def camera_payload() -> list[dict]:
    watching = Path(cfg["folder"]).resolve() if cfg["folder"] else None
    items = []
    for cam, folder in camera_pairs():
        names = list_videos(folder)
        unnamed = sum(1 for name in names if parse_coarse(Path(name).stem) is None)
        items.append({
            "cam": cam,
            "camera": cam,
            "folder": str(folder),
            "name": folder.name,
            "video_count": len(names),
            "unnamed_count": unnamed,
            "watching": watching is not None and folder.resolve() == watching,
        })
    return items


def _is_under(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def safe_video_path(filename: str) -> Path:
    folder = Path(cfg["folder"]).expanduser().resolve()
    name = Path(unquote(filename)).name
    path = (folder / name).resolve()
    if not _is_under(path, folder) or not path.is_file():
        raise FileNotFoundError(name)
    return path


def config_payload() -> dict:
    folder = cfg["folder"]
    videos = list_videos(Path(folder)) if folder else []
    unnamed_names = [name for name in videos if parse_coarse(Path(name).stem) is None]
    items = []
    for name in videos:
        parsed = parse_coarse(Path(name).stem)
        items.append({
            "filename": name,
            "named": parsed is not None,
            "parsed": parsed,
            "stamp": "",
            "unnamed_index": unnamed_names.index(name) if name in unnamed_names else None,
        })
    cameras = camera_payload()
    rows, _fields = read_lookup(lookup_path(Path(cfg["session_root"]))) if cfg["session_root"] else ([], [])
    watching = next((item for item in cameras if item["watching"]), None)
    return {
        "folder": folder,
        "session_root": cfg["session_root"],
        "lookup": str(lookup_path(Path(cfg["session_root"]))) if cfg["session_root"] else "",
        "user": cfg["user"],
        "video_count": len(videos),
        "videos": items,
        "sync_enabled": bool(cfg.get("sync_enabled")),
        "sync_available": len(cameras) > 1,
        "cameras": cameras,
        "watch_camera": watching["cam"] if watching else cfg.get("watch_cam") or "",
        "next_take": next_take(rows),
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/schema")
def get_schema():
    return jsonify(schema_payload())


@app.get("/api/config")
def get_config():
    return jsonify(config_payload())


@app.post("/api/config")
def set_config():
    data = request.get_json(force=True) or {}
    if data.get("folder"):
        folder = clean_user_path(data.get("folder") or "")
        if folder and not Path(folder).is_dir():
            return jsonify({"ok": False, "error": "视频文件夹不存在"}), 400
        apply_selected_path(folder)
    if "user" in data:
        try:
            cfg["user"] = normalize_user(str(data.get("user") or ""))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    if "sync_enabled" in data:
        cfg["sync_enabled"] = bool(data.get("sync_enabled"))
    return jsonify({"ok": True, "folder": cfg["folder"], "user": cfg["user"]})


@app.post("/api/browse/folder")
def browse_folder():
    try:
        path = pick_folder("选择受试者根目录（如 S004）或其中一个机位文件夹")
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    apply_selected_path(path.rstrip("/"))
    return jsonify({"ok": True, "path": cfg["folder"]})


@app.post("/api/watch-camera")
def watch_camera():
    data = request.get_json(force=True) or {}
    camera = (data.get("camera") or "").strip().lower()
    target = next((item for item in camera_payload() if item["cam"] == camera), None)
    if not target:
        return jsonify({"ok": False, "error": f"未找到机位 {camera}"}), 400
    cfg["folder"] = target["folder"]
    cfg["watch_cam"] = camera
    return jsonify({"ok": True, "folder": cfg["folder"], "camera": camera})


@app.get("/api/sync-preview")
def sync_preview():
    filename = (request.args.get("filename") or "").strip()
    if not cfg["folder"] or not filename:
        return jsonify({"rows": []})
    current = safe_video_path(filename)
    user = cfg["user"]
    action = (request.args.get("action") or "sq").strip().lower()
    take = normalize_take(request.args.get("take") or "1")
    rows = []
    stamps = []
    for item in slot_files(current, camera_pairs() or [((cfg.get("watch_cam") or "c0"), current.parent)]):
        cam = item["cam"] or (cfg.get("watch_cam") or "c0")
        proposed = f"{compose_stem(action, user, take, cam)}{(item['path'].suffix.lower() if item['path'] else '.mov')}"
        if item["stamp"]:
            stamps.append(item["stamp"])
        rows.append({
            "cam": cam,
            "filename": "" if item["missing"] else item["path"].name,
            "proposed": proposed,
            "missing": item["missing"],
            "stamp": item["stamp"],
            "size": item["size"],
        })
    mismatch = len({stamp for stamp in stamps if stamp}) > 1
    return jsonify({"rows": rows, "mismatch": mismatch})


@app.get("/api/video/<path:filename>")
def serve_video(filename: str):
    try:
        path = safe_video_path(filename)
    except FileNotFoundError:
        return "Not found", 404
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if path.suffix.lower() == ".mov" and mime == "application/octet-stream":
        mime = "video/quicktime"
    return send_file(path, mimetype=mime, conditional=True)


@app.get("/api/video/<path:filename>/orientation")
def video_orientation(filename: str):
    try:
        path = safe_video_path(filename)
        ccw = normalize_rotation_deg(probe_display_rotation(path))
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({
        "ok": True,
        "filename": path.name,
        "display_rotation_ccw": ccw,
        "css_degrees": css_degrees_from_ccw(ccw),
    })


@app.post("/api/open-native")
def open_native():
    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    try:
        path = safe_video_path(filename)
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return jsonify({"ok": True, "path": str(path)})


def _validated(data: dict) -> dict:
    action = action_by_code(str(data.get("action") or ""))
    if not action:
        raise ValueError("请选择动作")
    practice = (data.get("practice") or "").strip()
    if practice not in action["practices"]:
        raise ValueError("请选择该动作的目标做法")
    user = normalize_user(str(data.get("user") or cfg["user"]))
    take = normalize_take(data.get("take"))
    side = normalize_side(str(data.get("side") or ""), action)
    r_range = normalize_r_range(str(data.get("r_range") or ""))
    return {
        "action": action,
        "practice": practice,
        "user": user,
        "take": take,
        "side": side,
        "r_range": r_range,
    }


def _current_slots(filename: str) -> tuple[Path, list[dict]]:
    current = safe_video_path(filename)
    cameras = camera_pairs()
    if cameras:
        slots = slot_files(current, cameras)
    else:
        cam = (cfg.get("watch_cam") or "c0").lower()
        slots = slot_files(current, [(cam, current.parent)])
    return current, slots


@app.post("/api/rename")
def rename_videos():
    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename or not cfg["folder"]:
        return jsonify({"ok": False, "error": "还没选择视频"}), 400
    try:
        fields = _validated(data)
        current, slots = _current_slots(filename)
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    do_sync = bool(cfg.get("sync_enabled")) and len(camera_pairs()) > 1
    if not do_sync:
        slots = [item for item in slots if item["path"] and item["path"].resolve() == current.resolve()]

    present = [item for item in slots if not item["missing"] and item["path"] is not None]
    if not present:
        return jsonify({"ok": False, "error": "没有可改名的文件"}), 400

    session = Path(cfg["session_root"] or cfg["folder"])
    table = lookup_path(session)
    rows, csv_fields = read_lookup(table)
    slot_names = {item["path"].name for item in present}
    try:
        assert_take_free(rows, fields["take"], slot_names)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    desired = data.get("display_rotation_ccw")
    desired_ccw = None if desired is None else int(desired)
    renamed = []
    skipped = []
    file_rows = []
    for item in slots:
        if item["missing"] or item["path"] is None:
            skipped.append(item["cam"])
            continue
        cam = (item["cam"] or cfg.get("watch_cam") or "c0").lower()
        stem = compose_stem(fields["action"]["code"], fields["user"], fields["take"], cam)
        is_current = item["path"].resolve() == current.resolve()
        try:
            result = apply_rename(item["path"], stem, desired_ccw if is_current else None)
        except FileExistsError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 409
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        renamed.append(result["new_name"])
        file_rows.append((cam, item["path"].name, result["new_name"]))

    new_rows = build_rows(
        take=fields["take"],
        keep="是",
        user=fields["user"],
        action=fields["action"],
        side=fields["side"],
        practice=fields["practice"],
        r_range=fields["r_range"],
        files=file_rows,
    )
    write_lookup(table, replace_take_rows(rows, fields["take"], new_rows), csv_fields)
    cfg["user"] = fields["user"]
    return jsonify({
        "ok": True,
        "new_name": renamed[0] if renamed else "",
        "renamed": renamed,
        "synced": renamed[1:],
        "skipped": skipped,
        "lookup": str(table),
    })


@app.post("/api/skip")
def void_videos():
    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename or not cfg["folder"]:
        return jsonify({"ok": False, "error": "还没选择视频"}), 400
    try:
        current, slots = _current_slots(filename)
        take = normalize_take(data.get("take") or "1")
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    do_sync = bool(cfg.get("sync_enabled")) and len(camera_pairs()) > 1
    if not do_sync:
        slots = [item for item in slots if item["path"] and item["path"].resolve() == current.resolve()]

    present_names = {item["path"].name for item in slots if item["path"] is not None}
    session = Path(cfg["session_root"] or cfg["folder"])
    table = lookup_path(session)
    rows, csv_fields = read_lookup(table)
    try:
        assert_take_free(rows, take, present_names)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    moved = []
    file_rows = []
    for item in slots:
        if item["missing"] or item["path"] is None:
            continue
        dest = move_to_skip(item["path"])
        cam = (item["cam"] or cfg.get("watch_cam") or "c0").lower()
        moved.append(dest.name)
        file_rows.append((cam, item["path"].name, dest.name))

    action = action_by_code(str(data.get("action") or ""))
    practice = (data.get("practice") or "").strip()
    if action and practice not in action["practices"]:
        practice = ""
    side = "-"
    if action:
        try:
            side = normalize_side(str(data.get("side") or "-"), action)
        except ValueError:
            side = "-" if not action["sides"] else "L"
    user = cfg["user"]
    try:
        user = normalize_user(str(data.get("user") or cfg["user"]))
    except ValueError:
        pass
    r_range = ""
    try:
        if data.get("r_range"):
            r_range = normalize_r_range(str(data.get("r_range")))
    except ValueError:
        r_range = ""

    new_rows = build_rows(
        take=take,
        keep="否",
        user=user,
        action=action,
        side=side,
        practice=practice,
        r_range=r_range,
        files=file_rows,
    )
    write_lookup(table, replace_take_rows(rows, take, new_rows), csv_fields)
    return jsonify({"ok": True, "moved": moved, "missing": [item["cam"] for item in slots if item["missing"]], "lookup": str(table)})
