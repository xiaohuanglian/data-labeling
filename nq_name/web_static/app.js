/** 采集视频快速命名 */

const $ = (id) => document.getElementById(id);

const state = {
  schema: null,
  videos: [],
  index: 0,
  folder: "",
  cameras: [],
  syncAvailable: false,
  syncEnabled: true,
  watchCamera: "",
  syncPreview: null,
  form: {
    subject: "",
    action: "A01",
    side: "L",
    error_code: "E00",
    rep: "V01",
    camera: "C0",
  },
  /** QuickTime 式：预览旋转（逆时针角，与文件元数据一致）；确认命名时写入新文件 */
  fileRotationCcw: 0,
  desiredRotationCcw: 0,
  cacheBust: 0,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

async function post(path, body = {}) {
  return api(path, { method: "POST", body: JSON.stringify(body) });
}

function normalizeCcw(deg) {
  const x = ((((Number(deg) || 0) + 180) % 360) + 360) % 360 - 180;
  const candidates = [0, 90, 180, -90, -180];
  let best = candidates[0];
  let bestDist = Infinity;
  for (const c of candidates) {
    const d = Math.abs(c - x);
    if (d < bestDist) {
      best = c;
      bestDist = d;
    }
  }
  return best === -180 || best === 180 ? 180 : best;
}

function cssFromCcw(ccw) {
  return ((-normalizeCcw(ccw)) % 360 + 360) % 360;
}

function applyCssRotation(cssDegrees) {
  const player = $("player");
  const stage = $("videoStage");
  if (!player || !stage) return;

  const deg = ((Number(cssDegrees) || 0) % 360 + 360) % 360;
  const stageW = stage.clientWidth || 1;
  const stageH = stage.clientHeight || 1;
  const vw = player.videoWidth || 16;
  const vh = player.videoHeight || 9;
  const ratio = vw / Math.max(vh, 1);

  let layoutW;
  let layoutH;
  if (deg === 90 || deg === 270) {
    layoutH = Math.min(stageW, stageH / ratio);
    layoutW = ratio * layoutH;
  } else {
    layoutW = Math.min(stageW, stageH * ratio);
    layoutH = layoutW / ratio;
    if (layoutH > stageH) {
      layoutH = stageH;
      layoutW = layoutH * ratio;
    }
  }

  player.style.position = "absolute";
  player.style.top = "50%";
  player.style.left = "50%";
  player.style.width = `${Math.max(1, layoutW)}px`;
  player.style.height = `${Math.max(1, layoutH)}px`;
  player.style.maxWidth = "none";
  player.style.maxHeight = "none";
  player.style.objectFit = "contain";
  player.style.transformOrigin = "center center";
  player.style.transform = deg
    ? `translate(-50%, -50%) rotate(${deg}deg)`
    : "translate(-50%, -50%)";

  const hint = $("rotAngleHint");
  if (hint) {
    const changed = normalizeCcw(state.desiredRotationCcw) !== normalizeCcw(state.fileRotationCcw);
    hint.textContent = changed
      ? `预览已旋转（将随命名另存为新文件）· 显示 ${deg}°`
      : `未改方向 · 显示 ${deg}°（确认命名时仅改名）`;
  }
}

function bumpPreviewRotation(clockwiseDelta) {
  // QuickTime：向右=顺时针；元数据用逆时针，故 desired -= clockwise
  state.desiredRotationCcw = normalizeCcw(state.desiredRotationCcw - clockwiseDelta);
  applyCssRotation(cssFromCcw(state.desiredRotationCcw));
}

function resetPreviewRotation() {
  state.desiredRotationCcw = normalizeCcw(state.fileRotationCcw);
  applyCssRotation(cssFromCcw(state.desiredRotationCcw));
}

async function loadOrientationForCurrent() {
  const video = currentVideo();
  if (!video) return;
  try {
    const data = await api(
      `api/video/${encodeURIComponent(video.filename)}/orientation?t=${state.cacheBust || Date.now()}`,
    );
    state.fileRotationCcw = normalizeCcw(data.display_rotation_ccw || 0);
  } catch (_) {
    state.fileRotationCcw = 0;
  }
  state.desiredRotationCcw = state.fileRotationCcw;
  applyCssRotation(cssFromCcw(state.desiredRotationCcw));
}

function setStatus(msg, ok = true) {
  $("statusMsg").textContent = msg || "";
  $("statusMsg").style.color = ok ? "var(--success)" : "var(--danger)";
}

function normalizeSubject(raw) {
  const text = String(raw || "").trim().toUpperCase();
  if (!text) return "";
  if (/^P\d+$/.test(text)) return `P${text.slice(1).padStart(3, "0")}`;
  if (/^\d+$/.test(text)) return `P${text.padStart(3, "0")}`;
  return text;
}

function normalizeRep(raw) {
  const text = String(raw || "").trim().toUpperCase();
  if (!text) return "V01";
  if (/^V\d+$/.test(text)) return `V${String(parseInt(text.slice(1), 10)).padStart(2, "0")}`;
  if (/^\d+$/.test(text)) return `V${String(parseInt(text, 10)).padStart(2, "0")}`;
  return text;
}

function currentVideo() {
  return state.videos[state.index] || null;
}

function actionMeta(code) {
  return (state.schema?.actions || []).find((a) => a.code === code) || null;
}

function errorsForAction(actionCode) {
  const action = actionMeta(actionCode);
  const correct = state.schema?.correct_error || { code: "E00", name: "正确" };
  return [correct, ...((action && action.errors) || [])];
}

function renderChips(container, items, selected, onPick, opts = {}) {
  container.innerHTML = "";
  for (const item of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip" + (item.code === selected ? " active" : "");
    if (opts.showCode !== false) {
      btn.innerHTML = `${item.name}<span class="code">(${item.code})</span>`;
    } else {
      btn.textContent = item.name;
    }
    if (item.disabled) {
      btn.disabled = true;
      btn.style.opacity = "0.35";
    }
    btn.addEventListener("click", () => onPick(item.code));
    container.appendChild(btn);
  }
}

function renderActionChips() {
  const box = $("actionChips");
  box.innerHTML = "";
  for (const action of state.schema.actions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip" + (action.code === state.form.action ? " active" : "");
    btn.innerHTML = `${action.name}<span class="code">(${action.code})</span> · ${action.kind}`;
    btn.addEventListener("click", () => {
      state.form.action = action.code;
      const allowed = new Set(errorsForAction(action.code).map((e) => e.code));
      if (!allowed.has(state.form.error_code)) {
        state.form.error_code = "E00";
      }
      refreshFields();
    });
    box.appendChild(btn);
  }
}

function renderErrorChips() {
  renderChips($("errorChips"), errorsForAction(state.form.action), state.form.error_code, (code) => {
    state.form.error_code = code;
    refreshFields();
  });
}

function renderSideChips() {
  renderChips($("sideChips"), state.schema.sides, state.form.side, (code) => {
    state.form.side = code;
    refreshFields();
  });
}

function renderCameraChips() {
  const locked = Boolean(state.watchCamera);
  const items = (state.schema.cameras || []).map((item) => ({
    ...item,
    disabled: locked && item.code !== state.watchCamera,
  }));
  renderChips($("cameraChips"), items, state.form.camera, (code) => {
    if (locked && code !== state.watchCamera) return;
    state.form.camera = code;
    refreshFields();
  });
}

function syncInputsFromForm() {
  $("subjectInput").value = state.form.subject;
  $("repInput").value = state.form.rep;
}

function refreshFields() {
  renderActionChips();
  renderSideChips();
  renderErrorChips();
  renderCameraChips();
  syncInputsFromForm();
  updatePreview();
}

function composeStem(camera) {
  const subject = normalizeSubject(state.form.subject);
  const action = state.form.action;
  const side = state.form.side;
  const label = state.form.error_code;
  const rep = normalizeRep(state.form.rep);
  if (!subject || !action || !side || !label || !rep || !camera) return null;
  return `${subject}_${action}_${side}_${label}_${rep}_${camera}`;
}

function composeLocalPreview() {
  const video = currentVideo();
  const ext = video ? video.filename.slice(video.filename.lastIndexOf(".")) : ".mov";
  const stem = composeStem(state.form.camera);
  return stem ? `${stem}${ext.toLowerCase()}` : null;
}

function updatePreview() {
  const name = composeLocalPreview();
  const action = actionMeta(state.form.action);
  const err = errorsForAction(state.form.action).find((e) => e.code === state.form.error_code);
  $("namePreview").textContent = name || "—（请补全字段）";
  if (action && err) {
    $("previewHint").textContent =
      `解析：${action.name}(${action.code}) · ${state.form.side === "L" ? "左侧" : "右侧"} · ` +
      `${err.name}(${err.code}) → 标签写入 ${err.code}`;
  } else {
    $("previewHint").textContent = "";
  }
  renderSyncPreview();
}

function formatSize(bytes) {
  if (!bytes) return "";
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)}GB`;
  return `${Math.round(bytes / 1e6)}MB`;
}

function renderSyncPreview() {
  const box = $("syncPreview");
  if (!box) return;
  if (!state.syncEnabled || !state.syncAvailable) {
    box.hidden = true;
    box.innerHTML = "";
    box.classList.remove("warn");
    return;
  }
  const preview = state.syncPreview;
  const video = currentVideo();
  const lines = [];
  if (video && video.unnamed_index != null) {
    lines.push(`按未命名顺序第 ${video.unnamed_index + 1} 条对齐`);
  }
  if (preview && preview.peers) {
    for (const peer of preview.peers) {
      const stem = composeStem(peer.camera);
      const ext = peer.filename
        ? peer.filename.slice(peer.filename.lastIndexOf("."))
        : ".mov";
      const tag = peer.current ? "（当前）" : "";
      if (peer.missing) {
        lines.push(`${peer.camera}${tag}：找不到对应文件`);
      } else {
        const target = stem ? `${stem}${ext.toLowerCase()}` : "—";
        const stamp = peer.stamp ? ` · ${peer.stamp}` : "";
        const size = peer.size ? ` · ${formatSize(peer.size)}` : "";
        lines.push(`${peer.camera}${tag}：${peer.filename}${stamp}${size} → ${target}`);
      }
    }
    if (preview.stamp_warn) {
      lines.push("注意：各机位原片时间戳不一致，请先核对是否仍按顺序对齐。");
    }
    if (preview.size_warn) {
      lines.push("注意：各机位文件大小差很多，请先核对是否仍按顺序对齐。");
    }
  }
  box.hidden = lines.length === 0;
  box.textContent = lines.join("\n");
  box.classList.toggle("warn", Boolean(preview && (preview.stamp_warn || preview.size_warn)));
}

function applyParsed(parsed) {
  if (!parsed) return;
  state.form.subject = parsed.subject;
  state.form.action = parsed.action;
  state.form.side = parsed.side;
  state.form.error_code = parsed.error_code;
  state.form.rep = parsed.rep;
  state.form.camera = parsed.camera;
}

function showVideoError(msg) {
  const el = $("videoError");
  if (!el) return;
  if (msg) {
    el.hidden = false;
    el.textContent = msg;
  } else {
    el.hidden = true;
    el.textContent = "";
  }
}

function loadCurrentVideo() {
  const video = currentVideo();
  const player = $("player");
  showVideoError("");
  if (!video) {
    $("lblFilename").textContent = "文件夹内没有视频";
    player.removeAttribute("src");
    while (player.firstChild) player.removeChild(player.firstChild);
    player.load();
    $("progressChip").textContent = "0 / 0";
    return;
  }

  $("lblFilename").textContent = video.filename;
  $("lblFolder").textContent = state.folder;
  const unnamedTotal = state.videos.filter((v) => !v.named).length;
  const seq =
    video.unnamed_index != null
      ? `未命名 ${video.unnamed_index + 1}/${unnamedTotal}`
      : `${state.index + 1} / ${state.videos.length}`;
  $("progressChip").textContent = `${seq}` + (video.named ? " · 已符合命名" : " · 待命名");

  if (video.parsed) {
    applyParsed(video.parsed);
  } else {
    if ($("subjectInput").value.trim()) {
      state.form.subject = normalizeSubject($("subjectInput").value);
    }
    if (state.watchCamera) {
      state.form.camera = state.watchCamera;
    }
  }

  // 若旧机位不在列表中，回退到 C0
  const camCodes = new Set((state.schema?.cameras || []).map((c) => c.code));
  if (!camCodes.has(state.form.camera)) state.form.camera = "C0";

  refreshFields();

  // 与参考项目相同：直接设 video.src；cacheBust 避免旧缓存
  while (player.firstChild) player.removeChild(player.firstChild);
  const bust = state.cacheBust || Date.now();
  player.src = `api/video/${encodeURIComponent(video.filename)}?t=${bust}`;
  player.load();
  loadOrientationForCurrent().catch(() => {
    state.fileRotationCcw = 0;
    state.desiredRotationCcw = 0;
    applyCssRotation(0);
  });
  loadSyncPreview().catch(() => {
    state.syncPreview = null;
    renderSyncPreview();
  });
  setStatus("");
}

async function loadSyncPreview() {
  const video = currentVideo();
  if (!video || !state.syncAvailable) {
    state.syncPreview = null;
    renderSyncPreview();
    return;
  }
  state.syncPreview = await api(
    `api/sync-preview?filename=${encodeURIComponent(video.filename)}`,
  );
  renderSyncPreview();
}

function renderCameraSet(container, selected, onPick) {
  if (!container) return;
  container.innerHTML = "";
  for (const cam of state.cameras) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip" + (cam.camera === selected ? " active" : "");
    btn.textContent = `${cam.camera} · ${cam.unnamed_count}/${cam.video_count} 待命名`;
    btn.addEventListener("click", () => onPick(cam.camera));
    container.appendChild(btn);
  }
}

function renderSetupSync() {
  const card = $("setupSyncCard");
  const toggle = $("setupSyncToggle");
  const workToggle = $("workSyncToggle");
  const watchRow = $("watchRow");
  if (!state.syncAvailable || !state.cameras.length) {
    if (card) card.hidden = true;
    if (watchRow) watchRow.hidden = true;
    return;
  }
  if (card) card.hidden = false;
  if (watchRow) watchRow.hidden = false;
  if (toggle) toggle.checked = state.syncEnabled;
  if (workToggle) workToggle.checked = state.syncEnabled;
  renderCameraSet($("setupCameraChips"), state.watchCamera, (code) => {
    switchWatchCamera(code).catch((err) => {
      $("setupHint").textContent = err.message || String(err);
    });
  });
  renderCameraSet($("watchCameraChips"), state.watchCamera, (code) => {
    switchWatchCamera(code).catch((err) => setStatus(err.message || String(err), false));
  });
  const parts = state.cameras.map(
    (c) => `${c.name} ${c.unnamed_count}/${c.video_count}`,
  );
  if ($("setupSyncHint")) {
    $("setupSyncHint").textContent =
      `将按各机位「未命名列表」的同一序号对齐。当前主看 ${state.watchCamera || "—"}。` +
      ` ${parts.join(" · ")}`;
  }
}

async function switchWatchCamera(camera) {
  const keepIdx = currentVideo()?.unnamed_index;
  const keepName = currentVideo()?.filename;
  await post("api/watch-camera", { camera });
  await refreshConfig();
  let next = -1;
  if (keepIdx != null) {
    next = state.videos.findIndex((v) => v.unnamed_index === keepIdx);
  }
  if (next < 0 && keepName) {
    next = state.videos.findIndex((v) => v.filename === keepName);
  }
  if (next < 0) {
    next = state.videos.findIndex((v) => !v.named);
  }
  state.index = next >= 0 ? next : 0;
  if (!$("workspace").hidden) {
    loadCurrentVideo();
  }
}

async function refreshConfig() {
  const data = await api("api/config");
  state.folder = data.folder || "";
  state.videos = data.videos || [];
  state.cameras = data.cameras || [];
  state.syncAvailable = Boolean(data.sync_available);
  state.syncEnabled = data.sync_enabled !== false;
  state.watchCamera = data.watch_camera || "";
  if (state.index >= state.videos.length) state.index = Math.max(0, state.videos.length - 1);
  $("folderPath").value = state.folder || "";
  if (data.subject && !state.form.subject) {
    state.form.subject = data.subject;
  }
  if (state.watchCamera && !currentVideo()?.parsed) {
    state.form.camera = state.watchCamera;
  }
  const named = state.videos.filter((v) => v.named).length;
  $("setupHint").textContent = state.folder
    ? `已选择：${state.folder}（本机位 ${state.videos.length} 个视频，其中 ${named} 个已符合六段式命名）`
    : "请选择视频文件夹。";
  renderSetupSync();
}

async function startWorkspace() {
  if (!state.folder) {
    $("setupHint").textContent = "请先选择视频文件夹。";
    return;
  }
  await refreshConfig();
  if (!state.videos.length) {
    $("setupHint").textContent = "该文件夹内没有可识别的视频文件（优先支持 mov，也支持 mp4/m4v 等）。";
    return;
  }
  const firstTodo = state.videos.findIndex((v) => !v.named);
  state.index = firstTodo >= 0 ? firstTodo : 0;
  $("setupCard").hidden = true;
  $("workspace").hidden = false;
  loadCurrentVideo();
}

async function renameAndNext() {
  const video = currentVideo();
  if (!video) return;

  state.form.subject = normalizeSubject($("subjectInput").value);
  state.form.rep = normalizeRep($("repInput").value);
  refreshFields();

  try {
    const result = await post("api/rename", {
      filename: video.filename,
      subject: state.form.subject,
      action: state.form.action,
      side: state.form.side,
      error_code: state.form.error_code,
      rep: state.form.rep,
      camera: state.form.camera,
      display_rotation_ccw: state.desiredRotationCcw,
      sync_cameras: state.syncEnabled && state.syncAvailable,
    });

    const rotated =
      normalizeCcw(state.desiredRotationCcw) !== normalizeCcw(state.fileRotationCcw);
    const syncCount = (result.synced || []).length;
    const skipCount = (result.skipped || []).length;
    let extra = "";
    if (syncCount) extra += ` · 已同步 ${syncCount} 个机位`;
    if (skipCount) extra += ` · ${skipCount} 个机位未对齐`;
    setStatus(
      result.unchanged
        ? `文件未变：${result.new_name}${extra}`
        : rotated
          ? `已另存（命名+旋转）：${result.old_name} → ${result.new_name}${extra}`
          : `已命名：${result.old_name} → ${result.new_name}${extra}`,
      skipCount === 0,
    );

    // Rep 保持默认 V01，不自动递增
    state.form.rep = "V01";

    await refreshConfig();
    const renamedIdx = state.videos.findIndex((v) => v.filename === result.new_name);
    let nextIdx = -1;
    if (renamedIdx >= 0) {
      for (let i = renamedIdx + 1; i < state.videos.length; i += 1) {
        if (!state.videos[i].named) {
          nextIdx = i;
          break;
        }
      }
    }
    if (nextIdx < 0) {
      nextIdx = state.videos.findIndex((v) => !v.named);
    }
    state.index = nextIdx >= 0 ? nextIdx : Math.max(0, renamedIdx);
    loadCurrentVideo();
  } catch (err) {
    setStatus(err.message || String(err), false);
  }
}

function go(delta) {
  if (!state.videos.length) return;
  state.index = Math.max(0, Math.min(state.videos.length - 1, state.index + delta));
  loadCurrentVideo();
}

async function voidAndNext() {
  const video = currentVideo();
  if (!video) return;
  if (video.named) {
    skipNamed();
    return;
  }
  try {
    const result = await post("api/skip", {
      filename: video.filename,
      sync_cameras: state.syncEnabled && state.syncAvailable,
    });
    const n = (result.moved || []).length;
    const miss = (result.missing || []).length;
    let msg = `已作废 ${n} 个文件，移入「作废」文件夹`;
    if (miss) msg += ` · ${miss} 个机位未找到对应文件`;
    setStatus(msg, miss === 0);
    await refreshConfig();
    const firstTodo = state.videos.findIndex((v) => !v.named);
    state.index = firstTodo >= 0 ? firstTodo : Math.max(0, state.videos.length - 1);
    loadCurrentVideo();
  } catch (err) {
    setStatus(err.message || String(err), false);
  }
}

function skipNamed() {
  if (!state.videos.length) return;
  for (let i = 1; i <= state.videos.length; i += 1) {
    const idx = (state.index + i) % state.videos.length;
    if (!state.videos[idx].named) {
      state.index = idx;
      loadCurrentVideo();
      return;
    }
  }
  setStatus("所有视频均已符合六段式命名", true);
}

function bumpRep(delta) {
  const n = parseInt(normalizeRep($("repInput").value).replace(/\D/g, ""), 10) || 1;
  state.form.rep = `V${String(Math.max(1, n + delta)).padStart(2, "0")}`;
  $("repInput").value = state.form.rep;
  updatePreview();
}

function bindEvents() {
  $("btnBrowseFolder").addEventListener("click", async () => {
    try {
      const data = await post("api/browse/folder");
      $("folderPath").value = data.path || "";
      await refreshConfig();
    } catch (err) {
      $("setupHint").textContent = err.message || String(err);
    }
  });

  $("btnStart").addEventListener("click", () => {
    startWorkspace().catch((err) => {
      $("setupHint").textContent = err.message || String(err);
    });
  });

  $("btnBackSetup").addEventListener("click", async () => {
    $("workspace").hidden = true;
    $("setupCard").hidden = false;
    $("player").pause();
    await refreshConfig();
  });

  $("btnPrev").addEventListener("click", () => go(-1));
  $("btnNext").addEventListener("click", () => go(1));
  $("btnSkipNamed").addEventListener("click", skipNamed);
  $("btnVoid").addEventListener("click", () => {
    voidAndNext().catch((err) => setStatus(err.message || String(err), false));
  });
  $("setupSyncToggle").addEventListener("change", async (e) => {
    state.syncEnabled = e.target.checked;
    await post("api/config", { sync_enabled: state.syncEnabled });
    renderSetupSync();
    renderSyncPreview();
  });
  $("workSyncToggle").addEventListener("change", async (e) => {
    state.syncEnabled = e.target.checked;
    await post("api/config", { sync_enabled: state.syncEnabled });
    renderSetupSync();
    renderSyncPreview();
  });
  $("btnRename").addEventListener("click", () => {
    renameAndNext().catch((err) => setStatus(err.message || String(err), false));
  });
  $("btnRepMinus").addEventListener("click", () => bumpRep(-1));
  $("btnRepPlus").addEventListener("click", () => bumpRep(1));

  $("subjectInput").addEventListener("change", () => {
    state.form.subject = normalizeSubject($("subjectInput").value);
    $("subjectInput").value = state.form.subject;
    updatePreview();
  });
  $("subjectInput").addEventListener("input", updatePreview);

  $("repInput").addEventListener("change", () => {
    state.form.rep = normalizeRep($("repInput").value);
    $("repInput").value = state.form.rep;
    updatePreview();
  });
  $("repInput").addEventListener("input", updatePreview);

  $("player").addEventListener("error", () => {
    const video = currentVideo();
    showVideoError(
      video
        ? `浏览器无法解码 ${video.filename}（常见于 HEVC 的 .mov）。请点「系统播放器打开」，或用 Safari 打开本工具。`
        : "无法播放当前视频",
    );
  });
  $("player").addEventListener("loadeddata", () => showVideoError(""));

  $("btnOpenNative").addEventListener("click", async () => {
    const video = currentVideo();
    if (!video) return;
    try {
      await post("api/open-native", { filename: video.filename });
      setStatus(`已用系统播放器打开：${video.filename}`, true);
    } catch (err) {
      setStatus(err.message || String(err), false);
    }
  });

  $("btnRotLeft").addEventListener("click", () => {
    // QuickTime「向左旋转」= 逆时针 90 = 顺时针 -90
    bumpPreviewRotation(-90);
  });
  $("btnRotRight").addEventListener("click", () => {
    bumpPreviewRotation(90);
  });
  $("btnRotReset").addEventListener("click", () => {
    resetPreviewRotation();
  });
  $("player").addEventListener("loadedmetadata", () => {
    applyCssRotation(cssFromCcw(state.desiredRotationCcw));
  });
  window.addEventListener("resize", () => {
    applyCssRotation(cssFromCcw(state.desiredRotationCcw));
  });

  document.addEventListener("keydown", (e) => {
    if ($("workspace").hidden) return;
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") {
      if (e.key === "Enter") {
        e.preventDefault();
        renameAndNext().catch((err) => setStatus(err.message || String(err), false));
      }
      return;
    }
    if (e.key === " ") {
      e.preventDefault();
      const player = $("player");
      if (player.paused) player.play().catch(() => {});
      else player.pause();
    } else if (e.key === "Enter") {
      e.preventDefault();
      renameAndNext().catch((err) => setStatus(err.message || String(err), false));
    } else if (e.key === "x" || e.key === "X") {
      e.preventDefault();
      voidAndNext().catch((err) => setStatus(err.message || String(err), false));
    } else if (e.key === "ArrowLeft") {
      go(-1);
    } else if (e.key === "ArrowRight") {
      go(1);
    }
  });
}

async function boot() {
  bindEvents();
  state.schema = await api("api/schema");
  await refreshConfig();
  refreshFields();
}

boot().catch((err) => {
  $("setupHint").textContent = err.message || String(err);
});
