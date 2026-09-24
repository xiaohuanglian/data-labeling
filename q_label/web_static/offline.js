/** 线下切分标注 */

const API = "api/offline";
const $ = (id) => document.getElementById(id);

const state = {
  takes: [],
  index: 0,
  cam: "",
  duration: 0,
  playing: false,
  seeking: false,
  starts: [],
  peaks: [],
  history: [],
  jobs: {},
  pollTimer: null,
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

function currentTime() {
  const v = $("video");
  return Number.isFinite(v.currentTime) ? v.currentTime : 0;
}

async function api(path, opts = {}) {
  const res = await fetch(`${API}${path}`, {
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

function currentTake() {
  return state.takes[state.index] || null;
}

function nextPrompt() {
  const take = currentTake();
  if (!take) return "—";
  if (take.kind === "static") {
    return state.peaks.length ? "已标峰值，可导出" : "标 1 个正式峰值 P";
  }
  const need = take.rep_count;
  if (state.starts.length < need) {
    return `Rep ${state.starts.length + 1} 起始点 I`;
  }
  if (state.peaks.length < need) {
    return `Rep ${state.peaks.length + 1} 峰值 P`;
  }
  return "已齐，可导出";
}

function renderReps() {
  const take = currentTake();
  const box = $("repList");
  box.innerHTML = "";
  if (!take) return;
  const n = take.kind === "static" ? 1 : take.rep_count;
  $("markCount").textContent = `${state.starts.length + state.peaks.length} / ${take.kind === "static" ? 1 : n * 2}`;
  $("lblNextMark").textContent = nextPrompt();
  $("lblPrompt").textContent = nextPrompt();

  for (let i = 0; i < n; i += 1) {
    const start = take.kind === "static" ? 0 : state.starts[i];
    const peak = state.peaks[i];
    const row = document.createElement("div");
    row.className = "segment-row";
    const r = take.r_start + i;
    row.innerHTML = `
      <strong>r${String(r).padStart(3, "0")}</strong>
      <span>起始 ${start == null ? "—" : start.toFixed(3)}</span>
      <span>峰值 ${peak == null ? "—" : peak.toFixed(3)}</span>
    `;
    box.appendChild(row);
  }
}

function fillTakeInfo() {
  const take = currentTake();
  if (!take) return;
  $("kindBadge").textContent = take.kind === "static" ? "静态 · 不切" : "动态 · 要切";
  $("infoAction").textContent = `${take.action_cn} (${take.action})`;
  $("infoSide").textContent = take.side || "-";
  $("infoPractice").textContent = take.practice || "—";
  $("infoRange").textContent = take.range_judgement || "—";
  const last = take.r_start + take.rep_count - 1;
  $("infoR").textContent = `r${String(take.r_start).padStart(3, "0")}–r${String(last).padStart(3, "0")}`;
  $("infoNeed").textContent = take.kind === "static" ? "1 个峰值" : `${take.rep_count} 起始 + ${take.rep_count} 峰值`;
  $("lblTakeMeta").textContent = `t${String(take.take).padStart(2, "0")} · ${take.keep ? "有效" : "作废"}`;
  $("btnMarkStart").disabled = take.kind === "static";
}

function userPayload() {
  return {
    user_id: $("userId").value.trim(),
    height_cm: $("heightCm").value.trim(),
    torso_cm: $("torsoCm").value.trim(),
    shake_level: $("shakeLevel").value,
    shake_type: $("shakeType").value,
    probe_count: $("probeCount").value.trim(),
    measurement_method: $("measureMethod").value,
    measurement_confidence: $("measureConf").value,
    is_valid: $("isValid").value,
  };
}

async function saveDraft() {
  const take = currentTake();
  if (!take) return;
  try {
    await post("/marks", {
      take: take.take,
      starts: state.starts,
      peaks: state.peaks,
    });
  } catch (_) {
    /* 草稿失败不打断操作 */
  }
}

function markStart() {
  const take = currentTake();
  if (!take || take.kind === "static") return;
  if (state.starts.length >= take.rep_count) {
    setStatus(`起始点已经 ${take.rep_count} 个`, false);
    return;
  }
  const t = currentTime();
  state.starts.push(t);
  state.starts.sort((a, b) => a - b);
  state.history.push({ type: "start", value: t });
  renderReps();
  saveDraft();
  setStatus(`起始点 ${t.toFixed(3)}s`);
}

function markPeak() {
  const take = currentTake();
  if (!take) return;
  const need = take.kind === "static" ? 1 : take.rep_count;
  if (take.kind === "dynamic" && state.starts.length < state.peaks.length + 1) {
    setStatus("先标这个 rep 的起始点", false);
    return;
  }
  if (state.peaks.length >= need) {
    setStatus("峰值已经齐了", false);
    return;
  }
  const t = currentTime();
  state.peaks.push(t);
  state.history.push({ type: "peak", value: t });
  renderReps();
  saveDraft();
  setStatus(`峰值 ${t.toFixed(3)}s`);
}

function undoMark() {
  const last = state.history.pop();
  if (!last) return;
  const list = last.type === "start" ? state.starts : state.peaks;
  const idx = list.lastIndexOf(last.value);
  if (idx >= 0) list.splice(idx, 1);
  renderReps();
  saveDraft();
  setStatus("已撤销");
}

async function loadTake(index) {
  if (index < 0 || index >= state.takes.length) return;
  state.index = index;
  const take = currentTake();
  $("takePicker").value = String(index);
  state.cam = state.cam || take.review_cam;
  if ($("lblCam")) $("lblCam").textContent = `预览 ${state.cam} · 导出同步四机`;
  const draft = take.marks || {};
  state.starts = Array.isArray(draft.starts) ? draft.starts.map(Number) : [];
  state.peaks = Array.isArray(draft.peaks) ? draft.peaks.map(Number) : [];
  state.history = [];
  fillTakeInfo();
  renderReps();
  await loadVideo();
}

async function loadVideo() {
  const take = currentTake();
  if (!take) return;
  const filename = take.files[state.cam];
  if (!filename) {
    setStatus(`${state.cam} 没有文件`, false);
    return;
  }
  const video = $("video");
  video.src = `${API}/video/${take.take}/${state.cam}?t=${Date.now()}`;
  $("videoPlaceholder").hidden = true;
  $("lblFilename").textContent = filename;
  try {
    const meta = await api(`/video/${take.take}/${state.cam}/meta`);
    state.duration = Number(meta.duration) || 0;
  } catch (err) {
    setStatus(err.message, false);
  }
}

function tick() {
  const t = currentTime();
  $("lblHms").textContent = formatHms(t);
  $("lblSec").textContent = t.toFixed(3);
  if (!state.seeking && state.duration > 0) {
    $("seekBar").value = String(Math.round((t / state.duration) * 1000));
  }
}

function jobOf(takeNo) {
  return state.jobs[String(takeNo)] || null;
}

function applyJobs(jobs) {
  const map = {};
  (jobs || []).forEach((job) => {
    map[String(job.take)] = job;
  });
  state.jobs = map;
  state.takes.forEach((take) => {
    const job = map[String(take.take)];
    if (!job) return;
    if (job.status === "done") {
      take.done = true;
      take.queued = false;
    } else if (job.status === "error") {
      take.done = false;
      take.queued = false;
    } else {
      take.queued = true;
    }
  });
  refreshTakePicker();
  const failed = (jobs || []).filter((j) => j.status === "error");
  const running = (jobs || []).filter((j) => j.status === "queued" || j.status === "running");
  if (failed.length) {
    const last = failed[failed.length - 1];
    setStatus(`t${String(last.take).padStart(2, "0")} 剪切失败：${last.error}`, false);
  } else if (running.length) {
    const names = running.map((j) => `t${String(j.take).padStart(2, "0")}`).join("、");
    setStatus(`后台剪切 ${names}，可以继续标下一条`);
  }
  if (!running.length && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
    const lastDone = (jobs || []).filter((j) => j.status === "done").pop();
    if (lastDone && lastDone.result) {
      const miss = (lastDone.result.missing || []).join("、");
      setStatus(
        miss
          ? `后台已完成，写出 ${lastDone.result.rows} 行；缺 ${miss}`
          : `后台已完成，写出 ${lastDone.result.rows} 行、${(lastDone.result.clips || []).length} 个文件`
      );
    }
  }
}

function startJobPoll() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(async () => {
    try {
      const data = await api("/jobs");
      applyJobs(data.jobs || []);
    } catch (_) {
      /* 轮询失败不打断标注 */
    }
  }, 2000);
}

async function exportTake() {
  const take = currentTake();
  if (!take) return;
  const existing = jobOf(take.take);
  if (existing && (existing.status === "queued" || existing.status === "running")) {
    setStatus(`t${String(take.take).padStart(2, "0")} 已在后台剪切`, false);
    return;
  }
  try {
    await post("/export", {
      take: take.take,
      starts: state.starts,
      peaks: state.peaks,
      duration: state.duration,
      user: userPayload(),
    });
    take.queued = true;
    take.done = false;
    refreshTakePicker();
    setStatus(`t${String(take.take).padStart(2, "0")} 已交后台切四机，可直接标下一条`);
    startJobPoll();
    const next = state.takes.findIndex((t, i) => i > state.index && t.keep && !t.done && !t.queued);
    if (next >= 0) await loadTake(next);
  } catch (err) {
    setStatus(err.message, false);
  }
}

function refreshTakePicker() {
  const sel = $("takePicker");
  sel.innerHTML = "";
  state.takes.forEach((take, i) => {
    const opt = document.createElement("option");
    opt.value = String(i);
    const flag = take.done ? "✓ " : take.queued ? "切… " : take.keep ? "" : "作废 ";
    opt.textContent = `${flag}t${String(take.take).padStart(2, "0")} ${take.action_cn} ${take.practice}`;
    sel.appendChild(opt);
  });
  sel.value = String(state.index);
  const pending = state.takes.filter((t) => t.keep && !t.done && !t.queued).length;
  const cutting = state.takes.filter((t) => t.queued).length;
  const total = state.takes.filter((t) => t.keep).length;
  $("progressChip").textContent = cutting
    ? `待标 ${pending} / 有效 ${total} · 后台切 ${cutting}`
    : `待处理 ${pending} / 有效 ${total}`;
}

async function startWorkspace() {
  const cfg = await post("/config", {
    folder: $("folderPath").value,
    output: $("folderPath").value,
    user: userPayload(),
  });
  state.takes = cfg.takes || [];
  state.cam = cfg.cam || state.cam;
  if (!state.takes.length) throw new Error("对照表里没有条目");
  const first = state.takes.findIndex((t) => t.keep && !t.done);
  $("setupCard").hidden = true;
  $("workspace").hidden = false;
  refreshTakePicker();
  try {
    const jobs = await api("/jobs");
    applyJobs(jobs.jobs || []);
    if ((jobs.jobs || []).some((j) => j.status === "queued" || j.status === "running")) {
      startJobPoll();
    }
  } catch (_) {
    /* 无后台任务 */
  }
  await loadTake(first >= 0 ? first : 0);
}

function bind() {
  $("btnBrowseFolder").onclick = async () => {
    try {
      const data = await post("/browse/folder");
      $("folderPath").value = data.path;
      state.cam = data.cam || state.cam;
      $("setupHint").textContent = data.hint || "已选择预览机位。导出时四机同步切，标注表写在上一级。";
    } catch (err) {
      $("setupHint").textContent = err.message;
    }
  };
  $("btnStart").onclick = async () => {
    try {
      await startWorkspace();
    } catch (err) {
      $("setupHint").textContent = err.message;
    }
  };
  $("btnBackSetup").onclick = () => {
    $("workspace").hidden = true;
    $("setupCard").hidden = false;
  };
  $("btnPlay").onclick = () => {
    const v = $("video");
    if (v.paused) v.play();
    else v.pause();
  };
  $("video").addEventListener("play", () => {
    $("btnPlay").textContent = "暂停";
  });
  $("video").addEventListener("pause", () => {
    $("btnPlay").textContent = "播放";
  });
  $("video").addEventListener("timeupdate", tick);
  $("seekBar").addEventListener("input", () => {
    state.seeking = true;
    if (state.duration > 0) {
      $("video").currentTime = (Number($("seekBar").value) / 1000) * state.duration;
    }
  });
  $("seekBar").addEventListener("change", () => {
    state.seeking = false;
  });
  $("btnMarkStart").onclick = markStart;
  $("btnMarkPeak").onclick = markPeak;
  $("btnUndo").onclick = undoMark;
  $("btnExport").onclick = exportTake;
  $("btnPrevTake").onclick = () => {
    for (let i = state.index - 1; i >= 0; i -= 1) {
      if (state.takes[i].keep) {
        loadTake(i);
        return;
      }
    }
  };
  $("btnNextTake").onclick = () => {
    for (let i = state.index + 1; i < state.takes.length; i += 1) {
      if (state.takes[i].keep) {
        loadTake(i);
        return;
      }
    }
  };
  $("takePicker").onchange = () => loadTake(Number($("takePicker").value));

  document.addEventListener("keydown", (e) => {
    if ($("workspace").hidden) return;
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
    if (e.code === "Space") {
      e.preventDefault();
      $("btnPlay").click();
    } else if (e.key === "i" || e.key === "I" || e.key === "[") {
      e.preventDefault();
      markStart();
    } else if (e.key === "p" || e.key === "P") {
      e.preventDefault();
      markPeak();
    } else if (e.key === "Backspace") {
      e.preventDefault();
      undoMark();
    } else if (e.key === "Enter") {
      e.preventDefault();
      exportTake();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      $("video").currentTime = Math.max(0, currentTime() - 1 / 30);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      $("video").currentTime = currentTime() + 1 / 30;
    }
  });
}

bind();
