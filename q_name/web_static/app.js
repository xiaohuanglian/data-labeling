/** 量化粗命名 */

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
  nextTake: 1,
  form: {
    user: "s004",
    action: "sq",
    side: "-",
    practice: "标准平行蹲",
    take: 1,
    rRange: "",
  },
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
  player.style.transformOrigin = "center center";
  player.style.transform = deg ? `translate(-50%, -50%) rotate(${deg}deg)` : "translate(-50%, -50%)";
  const hint = $("rotAngleHint");
  if (hint) {
    const changed = normalizeCcw(state.desiredRotationCcw) !== normalizeCcw(state.fileRotationCcw);
    hint.textContent = changed ? `预览已旋转，确认时另存 · 显示 ${deg}°` : "未改方向，确认时只改名";
  }
}

function currentVideo() {
  return state.videos[state.index] || null;
}

function actionMeta(code) {
  return (state.schema?.actions || []).find((item) => item.code === code) || null;
}

function setStatus(msg, ok = true) {
  $("statusMsg").textContent = msg || "";
  $("statusMsg").style.color = ok ? "var(--success)" : "var(--danger)";
}

function showVideoError(msg) {
  const el = $("videoError");
  el.hidden = !msg;
  el.textContent = msg || "";
}

function normalizeUser(raw) {
  const text = String(raw || "").trim().toLowerCase();
  if (!text) return "";
  if (/^s\d+$/.test(text)) return `s${String(parseInt(text.slice(1), 10)).padStart(3, "0")}`;
  if (/^\d+$/.test(text)) return `s${String(parseInt(text, 10)).padStart(3, "0")}`;
  return text;
}

function normalizeTake(raw) {
  const text = String(raw || "").trim().toLowerCase().replace(/^t/, "");
  const n = parseInt(text, 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}

function renderChips(container, items, selected, onPick) {
  container.innerHTML = "";
  for (const item of items) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip" + (item.code === selected ? " active" : "");
    btn.textContent = item.name;
    btn.addEventListener("click", () => onPick(item.code));
    container.appendChild(btn);
  }
}

function renderActions() {
  const box = $("actionChips");
  box.innerHTML = "";
  for (const action of state.schema.actions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip" + (action.code === state.form.action ? " active" : "");
    btn.innerHTML = `${action.name}<span class="code">(${action.code})</span> · ${action.kind}`;
    btn.addEventListener("click", () => {
      state.form.action = action.code;
      if (!action.sides) state.form.side = "-";
      else if (state.form.side === "-") state.form.side = "L";
      if (!action.practices.includes(state.form.practice)) state.form.practice = action.practices[0];
      renderFields();
    });
    box.appendChild(btn);
  }
}

function renderFields() {
  const action = actionMeta(state.form.action);
  renderActions();
  const sides = action && !action.sides
    ? [{ code: "-", name: "无" }]
    : (state.schema.sides || []).filter((item) => item.code !== "-");
  renderChips($("sideChips"), sides, state.form.side, (code) => {
    state.form.side = code;
    renderFields();
  });
  const practices = (action?.practices || []).map((name) => ({ code: name, name }));
  renderChips($("practiceChips"), practices, state.form.practice, (code) => {
    state.form.practice = code;
    renderFields();
  });
  $("practiceHint").textContent = action?.r_hint || "";
  $("userInput").value = state.form.user;
  $("takeInput").value = `t${String(state.form.take).padStart(2, "0")}`;
  $("rInput").value = state.form.rRange;
  updatePreview();
}

function proposedName() {
  const user = normalizeUser($("userInput").value);
  const take = normalizeTake($("takeInput").value);
  const cam = state.watchCamera || "c0";
  if (!user || !state.form.action) return "—";
  return `${state.form.action}_${user}_t${String(take).padStart(2, "0")}_${cam}`;
}

function updatePreview() {
  const action = actionMeta(state.form.action);
  $("namePreview").textContent = proposedName();
  const sideName = state.form.side === "L" ? "左" : state.form.side === "R" ? "右" : "无";
  $("previewHint").textContent = action
    ? `${action.name} · ${sideName} · ${state.form.practice} · 对照表 r：${$("rInput").value || "未填"}`
    : "";
  renderSyncPreview();
}

async function renderSyncPreview() {
  const box = $("syncPreview");
  const video = currentVideo();
  if (!video || !state.syncAvailable) {
    box.hidden = true;
    return;
  }
  const take = normalizeTake($("takeInput").value);
  const action = state.form.action;
  let data;
  try {
    data = await api(
      `api/sync-preview?filename=${encodeURIComponent(video.filename)}&action=${encodeURIComponent(action)}&take=${take}`,
    );
  } catch (err) {
    box.hidden = false;
    box.textContent = err.message || String(err);
    return;
  }
  box.hidden = false;
  box.innerHTML = (data.rows || []).map((row) => {
    const flag = row.missing ? "缺文件" : row.filename;
    return `<div>${row.cam}：${flag} → ${row.proposed}</div>`;
  }).join("");
  if (data.mismatch) {
    box.innerHTML += `<div class="danger-text">四机时间戳不一致，确认前先看一眼。</div>`;
  }
}

function renderCameraChips(container, onPick) {
  container.innerHTML = "";
  for (const cam of state.cameras) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip" + (cam.watching ? " active" : "");
    btn.textContent = `${cam.name} · ${cam.unnamed_count} 条未命名`;
    if (onPick) btn.addEventListener("click", () => onPick(cam.cam));
    container.appendChild(btn);
  }
}

function renderSetupSync() {
  const card = $("setupSyncCard");
  card.hidden = !state.syncAvailable;
  $("watchRow").hidden = !state.syncAvailable;
  $("setupSyncToggle").checked = state.syncEnabled;
  $("workSyncToggle").checked = state.syncEnabled;
  renderCameraChips($("setupCameraChips"));
  renderCameraChips($("watchCameraChips"), (cam) => {
    post("api/watch-camera", { camera: cam }).then(async () => {
      await refreshConfig();
      const first = state.videos.findIndex((item) => !item.named);
      state.index = first >= 0 ? first : 0;
      loadCurrentVideo();
    }).catch((err) => setStatus(err.message, false));
  });
  $("setupSyncHint").textContent = state.folder
    ? `对照表：${state.lookup || ""}`
    : "";
}

async function refreshConfig() {
  const data = await api("api/config");
  state.folder = data.folder || "";
  state.lookup = data.lookup || "";
  state.videos = data.videos || [];
  state.cameras = data.cameras || [];
  state.syncAvailable = Boolean(data.sync_available);
  state.syncEnabled = data.sync_enabled !== false;
  state.watchCamera = data.watch_camera || "";
  state.nextTake = data.next_take || 1;
  if (data.user) state.form.user = data.user;
  if (state.index >= state.videos.length) state.index = Math.max(0, state.videos.length - 1);
  $("folderPath").value = state.folder || "";
  const named = state.videos.filter((item) => item.named).length;
  $("setupHint").textContent = state.folder
    ? `已选择：${state.folder}（${state.videos.length} 个视频，${named} 个已粗命名）`
    : "请选择视频文件夹。";
  $("progressChip").textContent = state.folder ? `${named}/${state.videos.length} 已命名` : "等待选择视频文件夹";
  renderSetupSync();
}

function applyParsed(video) {
  const parsed = video?.parsed;
  if (!parsed) {
    state.form.take = state.nextTake;
    return;
  }
  state.form.user = parsed.user;
  state.form.action = parsed.action;
  state.form.take = parsed.take;
  const action = actionMeta(parsed.action);
  if (action && !action.practices.includes(state.form.practice)) {
    state.form.practice = action.practices[0];
  }
  if (action && !action.sides) state.form.side = "-";
}

async function loadCurrentVideo() {
  const video = currentVideo();
  if (!video) {
    $("lblFilename").textContent = "没有视频";
    return;
  }
  applyParsed(video);
  renderFields();
  $("lblFilename").textContent = video.filename;
  $("lblFolder").textContent = state.folder;
  $("progressChip").textContent = `${state.index + 1}/${state.videos.length}`;
  const player = $("player");
  player.src = `api/video/${encodeURIComponent(video.filename)}?t=${state.cacheBust || Date.now()}`;
  showVideoError("");
  try {
    const data = await api(`api/video/${encodeURIComponent(video.filename)}/orientation?t=${Date.now()}`);
    state.fileRotationCcw = normalizeCcw(data.display_rotation_ccw || 0);
  } catch (_) {
    state.fileRotationCcw = 0;
  }
  state.desiredRotationCcw = state.fileRotationCcw;
  applyCssRotation(cssFromCcw(state.desiredRotationCcw));
}

async function startWorkspace() {
  if (!state.folder) {
    $("setupHint").textContent = "请先选择视频文件夹。";
    return;
  }
  await refreshConfig();
  if (!state.videos.length) {
    $("setupHint").textContent = "该文件夹里没有视频。";
    return;
  }
  const first = state.videos.findIndex((item) => !item.named);
  state.index = first >= 0 ? first : 0;
  $("setupCard").hidden = true;
  $("workspace").hidden = false;
  await loadCurrentVideo();
}

function formBody(filename) {
  return {
    filename,
    user: normalizeUser($("userInput").value),
    action: state.form.action,
    side: state.form.side,
    practice: state.form.practice,
    take: normalizeTake($("takeInput").value),
    r_range: $("rInput").value.trim(),
    display_rotation_ccw: state.desiredRotationCcw,
  };
}

async function renameAndNext() {
  const video = currentVideo();
  if (!video) return;
  state.form.user = normalizeUser($("userInput").value);
  state.form.take = normalizeTake($("takeInput").value);
  state.form.rRange = $("rInput").value.trim();
  try {
    const result = await post("api/rename", formBody(video.filename));
    const extra = (result.synced || []).length ? ` · 已同步 ${result.synced.length} 个机位` : "";
    const miss = (result.skipped || []).length ? ` · ${result.skipped.length} 个机位缺文件` : "";
    setStatus(`已命名：${result.new_name}${extra}${miss}`, !(result.skipped || []).length);
    await refreshConfig();
    const idx = state.videos.findIndex((item) => item.filename === result.new_name);
    let next = -1;
    for (let i = Math.max(0, idx) + 1; i < state.videos.length; i += 1) {
      if (!state.videos[i].named) {
        next = i;
        break;
      }
    }
    if (next < 0) next = state.videos.findIndex((item) => !item.named);
    state.index = next >= 0 ? next : Math.max(0, idx);
    state.form.take = state.nextTake;
    await loadCurrentVideo();
  } catch (err) {
    setStatus(err.message || String(err), false);
  }
}

async function voidAndNext() {
  const video = currentVideo();
  if (!video) return;
  try {
    const result = await post("api/skip", formBody(video.filename));
    setStatus(`已作废 ${(result.moved || []).length} 个文件`, true);
    await refreshConfig();
    const first = state.videos.findIndex((item) => !item.named);
    state.index = first >= 0 ? first : 0;
    state.form.take = state.nextTake;
    await loadCurrentVideo();
  } catch (err) {
    setStatus(err.message || String(err), false);
  }
}

function go(delta) {
  if (!state.videos.length) return;
  state.index = Math.max(0, Math.min(state.videos.length - 1, state.index + delta));
  loadCurrentVideo();
}

function skipNamed() {
  for (let i = 1; i <= state.videos.length; i += 1) {
    const idx = (state.index + i) % state.videos.length;
    if (!state.videos[idx].named) {
      state.index = idx;
      loadCurrentVideo();
      return;
    }
  }
  setStatus("这一机位都已粗命名", true);
}

function bumpTake(delta) {
  state.form.take = Math.max(1, normalizeTake($("takeInput").value) + delta);
  $("takeInput").value = `t${String(state.form.take).padStart(2, "0")}`;
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
    voidAndNext().catch((err) => setStatus(err.message, false));
  });
  $("btnRename").addEventListener("click", () => {
    renameAndNext().catch((err) => setStatus(err.message, false));
  });
  $("btnTakeMinus").addEventListener("click", () => bumpTake(-1));
  $("btnTakePlus").addEventListener("click", () => bumpTake(1));
  $("setupSyncToggle").addEventListener("change", async (e) => {
    state.syncEnabled = e.target.checked;
    await post("api/config", { sync_enabled: state.syncEnabled });
  });
  $("workSyncToggle").addEventListener("change", async (e) => {
    state.syncEnabled = e.target.checked;
    await post("api/config", { sync_enabled: state.syncEnabled });
  });
  $("userInput").addEventListener("input", updatePreview);
  $("takeInput").addEventListener("input", updatePreview);
  $("rInput").addEventListener("input", () => {
    state.form.rRange = $("rInput").value.trim();
    updatePreview();
  });
  $("btnRotLeft").addEventListener("click", () => {
    state.desiredRotationCcw = normalizeCcw(state.desiredRotationCcw - -90);
    applyCssRotation(cssFromCcw(state.desiredRotationCcw));
  });
  $("btnRotRight").addEventListener("click", () => {
    state.desiredRotationCcw = normalizeCcw(state.desiredRotationCcw - 90);
    applyCssRotation(cssFromCcw(state.desiredRotationCcw));
  });
  $("btnRotReset").addEventListener("click", () => {
    state.desiredRotationCcw = state.fileRotationCcw;
    applyCssRotation(cssFromCcw(state.desiredRotationCcw));
  });
  $("btnOpenNative").addEventListener("click", async () => {
    const video = currentVideo();
    if (!video) return;
    try {
      await post("api/open-native", { filename: video.filename });
      setStatus(`已用系统播放器打开：${video.filename}`, true);
    } catch (err) {
      setStatus(err.message, false);
    }
  });
  $("player").addEventListener("error", () => {
    const video = currentVideo();
    showVideoError(video ? `浏览器无法解码 ${video.filename}。请点「系统播放器打开」，或用 Safari。` : "无法播放");
  });
  $("player").addEventListener("loadeddata", () => showVideoError(""));
  document.addEventListener("keydown", (e) => {
    if ($("workspace").hidden) return;
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") {
      if (e.key === "Enter") {
        e.preventDefault();
        renameAndNext();
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
      renameAndNext();
    } else if (e.key === "x" || e.key === "X") {
      e.preventDefault();
      voidAndNext();
    } else if (e.key === "ArrowLeft") {
      go(-1);
    } else if (e.key === "ArrowRight") {
      go(1);
    }
  });
}

async function main() {
  state.schema = await api("api/schema");
  const first = state.schema.actions[0];
  state.form.action = first.code;
  state.form.practice = first.practices[0];
  state.form.side = first.sides ? "L" : "-";
  renderFields();
  bindEvents();
  await refreshConfig();
}

main().catch((err) => {
  $("setupHint").textContent = err.message || String(err);
});
