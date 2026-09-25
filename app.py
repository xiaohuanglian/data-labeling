#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据标注：四个独立功能挂在同一个本地网页上。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import run_simple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import compat_video
from nq_label.web_annotator import app as nq_label_app
from nq_name.web_namer import app as nq_name_app
from q_label.web_annotator import app as q_label_app
from q_name.web import app as q_name_app


class SlashFix:
    """挂载路径不带结尾斜杠时，把空 PATH_INFO 补成 /。"""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO", "") == "":
            environ["PATH_INFO"] = "/"
        return self.app(environ, start_response)


home = Flask(
    "home",
    template_folder=str(ROOT / "home" / "templates"),
    static_folder=str(ROOT / "home" / "static"),
    static_url_path="/home-static",
)


@home.get("/")
def index():
    return render_template("index.html")


@home.get("/compat/")
def compat_page():
    return render_template("compat.html")


@home.post("/api/compat/browse")
def compat_browse():
    try:
        path = compat_video.pick_folder()
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "path": path})


@home.post("/api/compat/scan")
def compat_scan():
    data = request.get_json(force=True) or {}
    folder = (data.get("folder") or "").strip()
    if not folder:
        return jsonify({"ok": False, "error": "先选文件夹"}), 400
    try:
        items = compat_video.scan_folder(folder)
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "items": items})


@home.post("/api/compat/convert")
def compat_convert():
    data = request.get_json(force=True) or {}
    file_path = (data.get("path") or "").strip()
    if not file_path:
        return jsonify({"ok": False, "error": "没有文件"}), 400
    try:
        result = compat_video.convert_file(file_path)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify(result)


application = DispatcherMiddleware(
    home,
    {
        "/nq-name": SlashFix(nq_name_app),
        "/nq-label": SlashFix(nq_label_app),
        "/q-name": SlashFix(q_name_app),
        "/q-label": SlashFix(q_label_app),
    },
)


def main() -> None:
    port = int(os.environ.get("PORT", "8780"))
    url = f"http://127.0.0.1:{port}"
    print(f"\n  数据标注: {url}")
    print("  1 非量化命名   /nq-name/")
    print("  2 非量化标注   /nq-label/")
    print("  3 量化命名     /q-name/")
    print("  4 量化标注     /q-label/")
    print("  iPhone 转码    /compat/\n")

    def _open() -> None:
        if sys.platform == "darwin":
            subprocess.run(["open", "-a", "Google Chrome", url], check=False)
        elif sys.platform == "win32":
            os.startfile(url)
        else:
            subprocess.run(["xdg-open", url], check=False)

    threading.Timer(0.6, _open).start()
    run_simple("127.0.0.1", port, application, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
