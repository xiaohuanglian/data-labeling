/** Rep 起止帧标注 — 每个 rep 标开始+结束，不抽中间帧 */

const $ = (id) => document.getElementById(id);

const state = {
  videos: [],
  index: 0,
  playing: false,
  seeking: false,
  captures: [],
  segments: [],
  annotatedMap: {},
  currentCapture: null,
  videoScale: 1,
  baseVideoW: 0,
  baseVideoH: 0,
  fps: 30,
  startSec: null,
  startFrame: null,
  endSec: null,
  endFrame: null,
  marking: false,
  root: "",
  folder: "",
  folderName: "",
  actionFolders: [],
};

function pad(n, w = 2) {
  return String(n).padStart(w, "0");
}

function formatHms(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "--:--:--.---";
  const totalMs = Math.round(sec * 1000);
  const ms = totalMs % 1000;
  const totalSec = Math.floor(totalMs / 1000);
  const s = totalSec % 60;
  const m = Math.floor(totalSec / 60) % 60;
  const h = Math.floor(totalSec / 3600);
  return `${pad(h)}:${pad(m)}:${pad(s)}.${pad(ms, 3)}`;
}

function formatPeak(sec) {
  return Number.isFinite(sec) && sec >= 0 ? sec.toFixed(3) : "—";
}

function currentFrameFromTime(sec) {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  return String(Math.max(0, Math.round(sec * state.fps)));
}

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

function setStatus(msg, ok = true) {
  $("statusMsg").textContent = msg;
  $("statusMsg").style.color = ok ? "var(--success)" : "var(--danger)";
}

function currentFilename() {
  return state.videos[state.index] || "";
}

function clearCaptureInfo() {
  for (const [id, val] of [
    ["infoVideo", currentFilename() || "—"],
    ["infoUser", "—"],
    ["infoAction", "—"],
    ["infoTimestamp", "—"],
    ["infoFrame", "—"],
    ["infoEndSec", "—"],
    ["infoEndFrame", "—"],
    ["infoTag", "—"],
    ["infoLabel", "—"],
  ]) {
    $(id).textContent = val;
  }
  state.currentCapture = null;
}

function showCaptureInfo(row) {
  $("infoVideo").textContent = row["来源视频"] || currentFilename() || "—";
  $("infoUser").textContent = row["用户编号"] || "—";
  $("infoAction").textContent = row["动作序号"] || "—";
  $("infoTimestamp").textContent = row["动作起始秒"] || row["时间戳秒"] || "—";
  $("infoFrame").textContent = row["帧号"] || "—";
  $("infoEndSec").textContent = row["动作结束秒"] || "—";
  $("infoEndFrame").textContent = row["结束帧号"] || "—";
  $("infoTag").textContent = row["标签"] || "—";
  $("infoLabel").textContent = row["标注结果"] || "—";
}

function updateTimeDisplay(sec) {
  $("lblHms").textContent = formatHms(sec);
  $("lblSec").textContent = formatPeak(sec);
  $("lblFrame").textContent = currentFrameFromTime(sec);
}

function syncFromVideo() {
  const v = $("video");
  if (!v.duration) return;
  $("seekBar").value = Math.round((v.currentTime / v.duration) * 1000);
  updateTimeDisplay(v.currentTime);
}

function updateMarkerDisplay() {
  if (state.startSec != null) {
    $("lblStart").textContent = `${formatPeak(state.startSec)}s · 帧 ${state.startFrame}`;
  } else {
    $("lblStart").textContent = "未标记";
  }
  if (state.endSec != null) {
    $("lblEnd").textContent = `${formatPeak(state.endSec)}s · 帧 ${state.endFrame}`;
  } else {
    $("lblEnd").textContent = "未标记";
  }
}

function clearMarkers() {
  state.startSec = null;
  state.startFrame = null;
  state.endSec = null;
  state.endFrame = null;
  updateMarkerDisplay();
}

function updateRepProgress() {
  const n = state.segments.length;
  const hint = $("repProgressHint");
  const name = currentFilename();
  if (hint) hint.textContent = `${takeLabel(name) || "本视频"}已标 ${n} 个 rep`;
  const badge = $("segmentCount");
  if (badge) badge.textContent = String(n);
}

function currentVideoTime() {
  const v = $("video");
  if (!v.src || !Number.isFinite(v.currentTime)) return null;
  return v.currentTime;
}

function markStart() {
  const v = $("video");
  const sec = currentVideoTime();
  if (sec == null) return;
  if (state.playing) v.pause();
  state.startSec = sec;
  state.startFrame = parseInt(currentFrameFromTime(sec), 10);
  updateMarkerDisplay();
  setStatus(`已标记开始: ${formatPeak(state.startSec)}s · 帧 ${state.startFrame}`);
}

async function markEnd() {
  const v = $("video");
  const sec = currentVideoTime();
  if (sec == null) return;
  if (state.startSec == null) {
    setStatus("请先按 [ 标记开始", false);
    return;
  }
  if (state.playing) v.pause();
  state.endSec = sec;
  state.endFrame = parseInt(currentFrameFromTime(sec), 10);
  updateMarkerDisplay();
  await saveRep();
}

async function saveRep() {
  if (state.startSec == null || state.endSec == null) {
    setStatus("请先标记开始 [ 和结束 ]", false);
    return false;
  }
  if (state.marking) return false;
  const name = currentFilename();
  if (!name) return false;

  state.marking = true;
  try {
    const res = await post("api/mark-rep", {
      filename: name,
      start_sec: state.startSec,
      end_sec: state.endSec,
    });
    state.segments = res.segments || [];
    if (res.captures?.length) {
      state.currentCapture = res.captures[res.captures.length - 1];
      showCaptureInfo(state.currentCapture.row);
    }
    $("lblOutputDir").textContent = res.output_dir ? `输出: ${res.output_dir}` : "";
    clearMarkers();
    await refreshCaptures();
    setStatus(
      `已写入 Rep #${res.action_seq} · 开始帧 ${res.start_frame} → 结束帧 ${res.end_frame}。可继续标下一段，点「下一个视频」才算这组完成`,
    );
    return true;
  } catch (e) {
    setStatus(e.message, false);
    alert(e.message);
    return false;
  } finally {
    state.marking = false;
  }
}

function takeLabel(filename) {
  const parts = (filename || "").replace(/\.[^.]+$/, "").split("_");
  if (parts.length < 5) return filename || "";
  const side = parts[2] === "R" ? "右侧" : "左侧";
  const err = (parts[3] || "").toUpperCase() === "E00" ? "正确" : "错误";
  return `${side}${err}`;
}

function folderLabeledTakeCount() {
  const folder = (state.actionFolders || []).find((item) => item.path === state.folder);
  if (folder) return folder.done_take_count || 0;
  const keys = new Set();
  for (const name of state.videos || []) {
    if (isVideoAnnotated(name)) keys.add(name.split("_").slice(0, 5).join("_").toLowerCase());
  }
  return keys.size;
}

function renderSegmentsList() {
  const list = $("segmentsList");
  list.innerHTML = "";
  updateRepProgress();

  if (state.segments.length === 0) {
    const empty = document.createElement("p");
    empty.className = "hint";
    const name = currentFilename();
    const folderCount = folderLabeledTakeCount();
    if (folderCount > 0) {
      empty.textContent = `当前是「${takeLabel(name)}」，这组还没标。本动作文件夹已有 ${folderCount} 组标完，可在上方「切换视频」里选 ✓ 已标注 查看。`;
    } else {
      empty.textContent = "尚未标注。每个 rep：先按 [ 标开始，再按 ] 标结束。";
    }
    list.appendChild(empty);
    return;
  }

  for (const seg of state.segments) {
    const item = document.createElement("div");
    item.className = "segment-item correct";

    const info = document.createElement("div");
    info.className = "segment-info";
    info.innerHTML = `
      <strong>Rep #${seg.action_seq}</strong>
      <span class="seg-label">${seg.label || "—"}</span>
      <span class="seg-range">开始 ${seg.start_sec}s · 帧 ${seg.start_frame || "—"} → 结束 ${seg.end_sec || "—"}s · 帧 ${seg.end_frame || "—"}</span>
    `;

    const actions = document.createElement("div");
    actions.className = "segment-actions";

    const jump = document.createElement("button");
    jump.type = "button";
    jump.className = "btn ghost sm";
    jump.textContent = "跳转";
    jump.onclick = () => {
      const cap = state.captures.find((c) => String(c.action_seq) === String(seg.action_seq));
      if (cap) selectCapture(cap);
      else seekVideoTo(seg.start_sec);
    };

    const del = document.createElement("button");
    del.type = "button";
    del.className = "btn danger outline sm";
    del.textContent = "删除";
    del.onclick = () => deleteSegment(seg.action_seq);

    actions.appendChild(jump);
    actions.appendChild(del);
    item.appendChild(info);
    item.appendChild(actions);
    list.appendChild(item);
  }
}

function isVideoAtEnd(v) {
  return v.ended || (Number.isFinite(v.duration) && v.duration > 0 && v.currentTime >= v.duration - 0.05);
}

function whenVideoReady(v, fn) {
  if (v.readyState >= 2) {
    fn();
    return;
  }
  const onReady = () => {
    v.removeEventListener("loadedmetadata", onReady);
    v.removeEventListener("canplay", onReady);
    fn();
  };
  v.addEventListener("loadedmetadata", onReady);
  v.addEventListener("canplay", onReady);
}

function togglePlay() {
  const v = $("video");
  if (!v.src) return;
  if (!v.paused && !v.ended) {
    v.pause();
    return;
  }
  whenVideoReady(v, () => {
    if (isVideoAtEnd(v)) v.currentTime = 0;
    const p = v.play();
    if (p?.catch) p.catch(() => setStatus("播放失败", false));
  });
}

function seekVideoTo(timestampSec) {
  const v = $("video");
  const sec = parseFloat(timestampSec);
  if (!Number.isFinite(sec) || sec < 0 || !v.src) return;
  if (!v.paused) v.pause();

  const applySeek = () => {
    const duration = v.duration || 0;
    v.currentTime = duration > 0 ? Math.min(sec, Math.max(0, duration - 0.001)) : sec;
    syncFromVideo();
    if (duration > 0) $("seekBar").value = Math.round((v.currentTime / duration) * 1000);
  };

  if (v.readyState >= 1 && Number.isFinite(v.duration)) applySeek();
  else whenVideoReady(v, applySeek);
}

function selectCapture(cap) {
  state.currentCapture = cap;
  showCaptureInfo(cap.row);
  $("btnDeleteCapture").disabled = !state.currentCapture;
  const ts = cap.timestamp || cap.row?.["时间戳秒"] || "";
  if (ts !== "") {
    seekVideoTo(ts);
    setStatus(`已选中: Rep #${cap.action_seq} · 开始帧 ${cap.frame_number} → 结束帧 ${cap.end_frame || "—"}`);
  }
}

function isVideoAnnotated(filename) {
  return !!state.annotatedMap[filename]?.annotated;
}

function setVideoAnnotatedStatus(filename, segmentCount, annotated) {
  if (!filename) return;
  const prev = state.annotatedMap[filename] || {};
  const count = Math.max(0, Number(segmentCount) || 0);
  state.annotatedMap[filename] = {
    annotated: annotated == null ? !!prev.annotated : !!annotated,
    segment_count: count,
  };
}

function findUnannotatedIndex(fromIndex, direction) {
  let i = fromIndex + direction;
  while (i >= 0 && i < state.videos.length) {
    if (!isVideoAnnotated(state.videos[i])) return i;
    i += direction;
  }
  return -1;
}

function updateNavButtons() {
  const prevBtn = $("btnPrevVideo");
  const nextBtn = $("btnNextVideo");
  if (!prevBtn || !nextBtn) return;
  if (state.videos.length === 0) {
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    return;
  }
  const later = findUnannotatedIndex(state.index, 1);
  const currentDone = isVideoAnnotated(currentFilename());
  prevBtn.disabled = findUnannotatedIndex(state.index, -1) < 0 && !neighborActionFolder(-1);
  nextBtn.disabled = later < 0 && currentDone && !neighborActionFolder(1);
}

async function refreshVideosStatus() {
  try {
    const res = await api("api/videos/status");
    const map = {};
    for (const item of res.videos || []) {
      map[item.filename] = {
        annotated: !!item.annotated,
        segment_count: item.segment_count || 0,
      };
    }
    state.annotatedMap = map;
    if (res.action_folders) {
      state.actionFolders = res.action_folders;
      state.folder = res.folder || state.folder;
      state.folderName = res.folder_name || state.folderName;
      if (res.root != null) state.root = res.root;
      renderActionPicker();
    }
  } catch (_) {
    state.annotatedMap = {};
  }
  renderVideoPicker();
  updateNavButtons();
}

async function refreshCaptures() {
  const name = currentFilename();
  if (!name) return;
  try {
    const res = await api(`api/video/${encodeURIComponent(name)}/captures`);
    state.captures = res.captures || [];
    state.segments = res.segments || [];
    $("lblOutputDir").textContent = res.output_dir ? `输出: ${res.output_dir}` : "";
    renderSegmentsList();
    renderVideoPicker();
    updateNavButtons();
    updateRepProgress();
    $("btnDeleteCapture").disabled = !state.currentCapture;
  } catch (_) {
    state.captures = [];
    state.segments = [];
    renderSegmentsList();
    renderVideoPicker();
    updateNavButtons();
    updateRepProgress();
    $("btnDeleteCapture").disabled = true;
  }
}

function renderVideoPicker() {
  const sel = $("videoPicker");
  if (!sel) return;
  sel.innerHTML = "";
  if (state.videos.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "— 无视频 —";
    sel.appendChild(opt);
    sel.disabled = true;
    return;
  }
  state.videos.forEach((name, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    const annotated = isVideoAnnotated(name);
    const prefix = annotated ? "✓ 已标注 " : "";
    opt.textContent = `${prefix}${i + 1}. ${name}`;
    if (annotated) {
      opt.classList.add("annotated");
      opt.dataset.annotated = "1";
    }
    sel.appendChild(opt);
  });
  sel.value = String(state.index);
  sel.disabled = false;
  sel.classList.toggle("has-annotated", Object.values(state.annotatedMap).some((v) => v?.annotated));
}

async function loadVideoAt(idx) {
  if (idx < 0 || idx >= state.videos.length) {
    setStatus("全部视频已处理完毕！", true);
    $("progressChip").textContent = "已完成";
    return;
  }

  state.index = idx;
  const name = currentFilename();
  const v = $("video");

  $("lblFilename").textContent = state.folderName ? `${state.folderName} · ${name}` : name;
  $("progressChip").textContent = progressChipText(idx);
  $("videoPlaceholder").hidden = true;
  $("btnPlay").textContent = "播放";
  renderVideoPicker();
  updateNavButtons();

  state.playing = false;
  v.pause();
  v.src = `api/video/${encodeURIComponent(name)}`;
  v.load();

  clearCaptureInfo();
  clearMarkers();
  $("infoVideo").textContent = name;

  const meta = await api(`api/video/${encodeURIComponent(name)}/meta`);
  state.fps = meta.fps || 30;
  $("lblOutputDir").textContent = meta.output_dir ? `输出: ${meta.output_dir}` : "";

  await refreshCaptures();
  setStatus(`已加载: ${name} · [ 开始 · ] 结束并写入`);
  focusVideoPanel();
}

function stepTime(delta) {
  const v = $("video");
  if (!v.src) return;
  if (!v.paused) v.pause();
  whenVideoReady(v, () => {
    v.currentTime = Math.max(0, Math.min(v.duration || 0, v.currentTime + delta));
    syncFromVideo();
  });
}

function applyLoadedConfig(cfg) {
  state.root = cfg.root || "";
  state.folder = cfg.folder || "";
  state.folderName = cfg.folder_name || "";
  state.actionFolders = cfg.action_folders || [];
  state.videos = cfg.videos || [];
  $("folderPath").value = cfg.root || cfg.folder || $("folderPath").value || "";
  const nActions = state.actionFolders.length;
  if (nActions) {
    const doneCount = state.actionFolders.filter((f) => f.done).length;
    $("setupHint").textContent =
      `已扫描：${nActions} 个动作文件夹 · 已完成 ${doneCount} · 当前 ${cfg.folder_name || ""} · ${cfg.video_count || 0} 个视频 · CSV 写在各小文件夹`;
  } else {
    $("setupHint").textContent =
      `已扫描：${cfg.video_count || 0} 个视频 · 每个动作文件夹一份 CSV`;
  }
  renderActionPicker();
}

function neighborActionFolder(direction) {
  const list = state.actionFolders || [];
  if (!list.length || !state.folder) return null;
  const cur = list.findIndex((item) => item.path === state.folder);
  if (cur < 0) return null;
  let i = cur + direction;
  while (i >= 0 && i < list.length) {
    if (!list[i].done) return list[i];
    i += direction;
  }
  return null;
}

function progressChipText(idx) {
  const videoPart = `第 ${idx + 1} / ${state.videos.length} 个视频`;
  const list = state.actionFolders || [];
  if (!list.length) return videoPart;
  const cur = Math.max(0, list.findIndex((item) => item.path === state.folder));
  return `${state.folderName || "动作"} · ${cur + 1}/${list.length} · ${videoPart}`;
}

function renderActionPicker() {
  const row = $("actionPickerRow");
  const sel = $("actionPicker");
  if (!row || !sel) return;
  const list = state.actionFolders || [];
  if (!list.length) {
    row.hidden = true;
    sel.innerHTML = "";
    return;
  }
  row.hidden = false;
  sel.innerHTML = "";
  list.forEach((item, i) => {
    const opt = document.createElement("option");
    opt.value = item.path;
    const mark = item.done ? "✓ " : "";
    opt.textContent = `${mark}${i + 1}. ${item.name}（${item.done_take_count}/${item.take_count}）`;
    if (item.done) opt.classList.add("done");
    sel.appendChild(opt);
  });
  sel.value = state.folder;
}

async function switchToActionFolder(path, preferLast = false) {
  const cfg = await post("api/switch-folder", { folder: path });
  applyLoadedConfig(cfg);
  await refreshVideosStatus();
  if (preferLast) {
    let last = -1;
    for (let i = 0; i < state.videos.length; i += 1) {
      if (!isVideoAnnotated(state.videos[i])) last = i;
    }
    await loadVideoAt(last >= 0 ? last : Math.max(0, state.videos.length - 1));
    return;
  }
  const first = state.videos.findIndex((name) => !isVideoAnnotated(name));
  await loadVideoAt(first >= 0 ? first : 0);
}

async function applyConfig() {
  const cfg = await post("api/config", {
    folder: $("folderPath").value.trim(),
  });
  applyLoadedConfig(cfg);
  return cfg;
}

async function browseFolder() {
  const res = await post("api/browse/folder");
  applyLoadedConfig(res);
}

async function startAnnotating() {
  const cfg = await applyConfig();
  if (!cfg.folder) { alert("请先选择视频文件夹"); return; }
  if (cfg.video_count === 0) { alert("视频文件夹中没有视频文件"); return; }

  $("setupCard").hidden = true;
  $("workspace").hidden = false;
  applyLoadedConfig(cfg);
  await refreshVideosStatus();
  const first = state.videos.findIndex((n) => !isVideoAnnotated(n));
  await loadVideoAt(first >= 0 ? first : 0);
  focusVideoPanel();
}

async function goPrevVideo() {
  const idx = findUnannotatedIndex(state.index, -1);
  if (idx >= 0) {
    await loadVideoAt(idx);
    return;
  }
  const prev = neighborActionFolder(-1);
  if (prev) {
    await switchToActionFolder(prev.path, true);
    setStatus(`已回到 ${prev.name}`);
    return;
  }
  setStatus("没有上一个未标注视频");
}

async function goNextVideo() {
  const name = currentFilename();
  if (!name) return;
  const res = await post("api/complete-take", { filename: name });
  if (res.action_folders) state.actionFolders = res.action_folders;
  await refreshVideosStatus();
  const synced = res.synced_count || 1;
  if (res.next_folder) {
    await switchToActionFolder(res.next_folder);
    setStatus(`本组已完成 · 进入 ${res.next_folder_name}`);
    return;
  }
  const idx = findUnannotatedIndex(state.index, 1);
  if (idx >= 0) {
    await loadVideoAt(idx);
    setStatus(`本组已完成 · 已跳过同组 ${synced} 个机位`);
    return;
  }
  renderVideoPicker();
  renderActionPicker();
  updateNavButtons();
  setStatus("本组已完成 · 当前动作已全部标完");
  $("progressChip").textContent = state.actionFolders.length ? `${state.folderName} · 已完成` : "未标注已处理完";
}

async function deleteSegment(actionSeq) {
  if (!confirm(`确认删除 Rep #${actionSeq}？同步机位会一起删。`)) return;
  try {
    const res = await post("api/delete-segment", {
      filename: currentFilename(),
      action_seq: String(actionSeq),
    });
    await refreshCaptures();
    await refreshVideosStatus();
    if (String(state.currentCapture?.action_seq) === String(actionSeq)) {
      clearCaptureInfo();
    }
    setStatus(`已删除 Rep #${actionSeq}`);
  } catch (e) {
    alert(e.message);
  }
}

async function deleteCapture(cap) {
  if (!cap?.action_seq) return;
  await deleteSegment(cap.action_seq);
}

function focusVideoPanel() {
  $("videoPanel")?.focus({ preventScroll: true });
}

function handleGlobalKeydown(e) {
  if ($("workspace").hidden) return;
  if (e.target.matches("input, textarea, select") || e.target.isContentEditable) return;

  if (e.code === "Space") {
    e.preventDefault();
    togglePlay();
  } else if (e.code === "BracketLeft") {
    e.preventDefault();
    markStart();
  } else if (e.code === "BracketRight") {
    e.preventDefault();
    markEnd();
  } else if (e.code === "ArrowLeft") {
    e.preventDefault();
    stepTime(-1 / state.fps);
  } else if (e.code === "ArrowRight") {
    e.preventDefault();
    stepTime(1 / state.fps);
  }
}

function initVideoScale() {
  const handle = $("videoResizeHandle");
  const wrap = $("videoWrap");
  const v = $("video");
  if (!handle || !wrap || !v) return;

  const saved = parseFloat(localStorage.getItem("seqVideoScale") || "1");
  state.videoScale = Number.isFinite(saved) ? saved : 1;

  const updateBaseSize = () => {
    if (!v.videoWidth || !v.videoHeight) return;
    const column = wrap.closest(".left-column") || wrap.parentElement;
    const availW = Math.max((column?.clientWidth || 640) - 48, 200);
    const maxH = Math.min(window.innerHeight * 0.45, 400);
    const aspect = v.videoWidth / v.videoHeight;
    let w, h;
    if (availW / maxH > aspect) { h = maxH; w = h * aspect; }
    else { w = availW; h = w / aspect; }
    state.baseVideoW = w;
    state.baseVideoH = h;
    applyVideoScale(state.videoScale);
  };

  const applyVideoScale = (scale) => {
    state.videoScale = Math.max(0.25, Math.min(3, scale));
    if (state.baseVideoW > 0) {
      const w = state.baseVideoW * state.videoScale;
      const h = state.baseVideoH * state.videoScale;
      v.style.width = `${w}px`;
      v.style.height = `${h}px`;
      wrap.style.width = `${w + 24}px`;
      wrap.style.height = `${h + 24}px`;
    }
    $("videoScaleLabel").textContent = `缩放 ${Math.round(state.videoScale * 100)}%`;
    localStorage.setItem("seqVideoScale", String(state.videoScale));
  };

  window.applyVideoScale = applyVideoScale;
  window.updateVideoBaseSize = updateBaseSize;
  updateBaseSize();
  applyVideoScale(state.videoScale);

  let startY = 0, startScale = 1, dragging = false;
  const onMove = (e) => { if (dragging) applyVideoScale(startScale + (e.clientY - startY) / 180); };
  const onUp = () => {
    dragging = false;
    document.body.style.cursor = "";
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
  };
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    dragging = true;
    startY = e.clientY;
    startScale = state.videoScale;
    document.body.style.cursor = "ns-resize";
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
  v.addEventListener("loadedmetadata", updateBaseSize);
  window.addEventListener("resize", updateBaseSize);
}

function bindEvents() {
  const v = $("video");

  $("btnBrowseFolder").onclick = () => browseFolder().catch((e) => {
    const msg = String(e.message || "");
    if (msg.includes("已取消")) return;
    alert(msg.includes("无法打开系统文件夹选择器")
      ? msg
      : "无法打开系统文件夹选择器。请把路径直接贴进输入框，例如 /Users/mac/Downloads/P007");
  });
  $("btnStart").onclick = () => startAnnotating().catch((e) => alert(e.message));
  $("btnPlay").onclick = togglePlay;
  $("btnMarkStart").onclick = () => markStart();
  $("btnMarkEnd").onclick = () => markEnd();
  $("btnClearMarkers").onclick = clearMarkers;
  $("btnDeleteCapture").onclick = () => state.currentCapture && deleteCapture(state.currentCapture);

  $("btnBackSetup").onclick = () => {
    $("workspace").hidden = true;
    $("setupCard").hidden = false;
    v.pause();
  };
  $("btnPrevVideo").onclick = () => goPrevVideo().catch((e) => alert(e.message));
  $("btnNextVideo").onclick = () => goNextVideo().catch((e) => alert(e.message));

  $("actionPicker")?.addEventListener("change", (e) => {
    const path = e.target.value;
    if (path && path !== state.folder) {
      switchToActionFolder(path).catch((err) => alert(err.message));
    }
  });

  $("videoPicker")?.addEventListener("change", (e) => {
    const idx = parseInt(e.target.value, 10);
    if (Number.isFinite(idx) && idx >= 0 && idx < state.videos.length && idx !== state.index) {
      loadVideoAt(idx);
    }
  });

  $("videoPanel").addEventListener("click", focusVideoPanel);

  v.addEventListener("play", () => { state.playing = true; $("btnPlay").textContent = "暂停"; });
  v.addEventListener("pause", () => { state.playing = false; $("btnPlay").textContent = "播放"; syncFromVideo(); });
  v.addEventListener("ended", () => { state.playing = false; $("btnPlay").textContent = "播放"; syncFromVideo(); });
  v.addEventListener("timeupdate", () => { if (!state.seeking) syncFromVideo(); });
  v.addEventListener("loadedmetadata", () => {
    $("seekBar").value = 0;
    syncFromVideo();
    window.updateVideoBaseSize?.();
  });

  $("seekBar").addEventListener("input", () => {
    state.seeking = true;
    if (v.duration) {
      v.currentTime = ($("seekBar").value / 1000) * v.duration;
      updateTimeDisplay(v.currentTime);
    }
  });
  $("seekBar").addEventListener("change", () => { state.seeking = false; v.pause(); });

  document.addEventListener("keydown", handleGlobalKeydown, true);
}

async function init() {
  bindEvents();
  initVideoScale();
  try {
    const cfg = await api("api/config");
    applyLoadedConfig(cfg);
  } catch (_) {}
}

init();
