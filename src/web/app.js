"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let voiceData = { chatterbox: [], kokoro: [], emotions: [] };
let currentJob = null;
let currentScript = null;
let dirty = { characters: {}, segments: {}, narrator_voice: null };
let pollTimer = null;

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* not JSON */ }
    throw new Error(msg);
  }
  const type = r.headers.get("content-type") || "";
  return type.includes("json") ? r.json() : r.blob();
}
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 4000);
}

// ----- tabs -----
function show(tab) {
  $$("main > section").forEach((s) => (s.hidden = s.id !== `tab-${tab}`));
  $$("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab || (tab === "job" && b.dataset.tab === "stories")));
  clearInterval(pollTimer);
  if (tab === "stories") { loadJobs(); pollTimer = setInterval(loadJobs, 4000); }
  if (tab === "voices") loadVoices();
  if (tab === "settings") loadSettings();
}
$$("nav button").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.tab; }));
window.addEventListener("hashchange", route);
function route() {
  const h = location.hash.slice(1) || "stories";
  if (h.startsWith("job/")) openJob(h.slice(4));
  else show(["stories", "voices", "settings"].includes(h) ? h : "stories");
}

async function loadStatus() {
  try {
    const s = await api("/api/status");
    $("#gpu").textContent = s.gpu ? `${s.gpu.name}, ${s.gpu.free_gb} of ${s.gpu.total_gb} GB free` : "No GPU visible (CPU only)";
  } catch (e) { $("#gpu").textContent = ""; }
}

// ----- stories -----
const STATUS = { queued: "Waiting", tagging: "Casting", review: "Ready to check", rendering: "Rendering", mixing: "Mixing", done: "Done", failed: "Failed", stopped: "Stopped" };

async function loadJobs() {
  const list = await api("/api/jobs");
  const el = $("#job-list");
  if (!list.length) { el.textContent = "Nothing yet."; el.className = "muted"; return; }
  el.className = "";
  el.innerHTML = list.map((j) => `
    <div class="job" data-id="${j.id}">
      <span class="title">${esc(j.title)}</span>
      <span class="muted">${esc(j.message || "")}</span>
      <span class="pill ${j.status}">${STATUS[j.status] || j.status}</span>
    </div>`).join("");
  $$(".job", el).forEach((d) => d.addEventListener("click", () => { location.hash = `job/${d.dataset.id}`; }));
}

$("#new-story").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  if (!fd.get("file")?.name) fd.delete("file");
  if (!fd.get("file") && !fd.get("text").trim()) { toast("Choose a file or paste the story"); return; }
  try {
    const job = await api("/api/jobs", { method: "POST", body: fd });
    e.target.reset();
    location.hash = `job/${job.id}`;
  } catch (err) { toast(err.message); }
});

// ----- one story -----
$("#back").addEventListener("click", () => { location.hash = "stories"; });

async function openJob(id) {
  show("job");
  if (!currentJob || currentJob.id !== id) { currentScript = null; resetDirty(); }
  await refreshJob(id);
  pollTimer = setInterval(() => refreshJob(id), 3000);
}

function resetDirty() { dirty = { characters: {}, segments: {}, narrator_voice: null }; }

async function refreshJob(id) {
  let job;
  try { job = await api(`/api/jobs/${id}`); } catch (e) { toast(e.message); location.hash = "stories"; return; }
  const changed = !currentJob || currentJob.status !== job.status;
  currentJob = job;
  $("#job-title").textContent = job.title;
  $("#job-message").textContent = job.error ? `${job.message}` : job.message;
  const working = ["queued", "tagging", "rendering", "mixing"].includes(job.status);
  $("#job-progress").hidden = !working;
  $("#job-progress").max = job.total || 1;
  $("#job-progress").value = job.done || 0;
  if (changed) {
    renderActions(job);
    if (!working) await loadScript(id);
    const audio = $("#job-audio");
    audio.hidden = job.status !== "done";
    if (job.status === "done") audio.src = `/api/jobs/${id}/audio?t=${job.updated}`;
  }
  $("#cast-card").hidden = $("#lines-card").hidden = working || !currentScript;
}

function renderActions(job) {
  const a = $("#job-actions");
  const working = ["queued", "tagging", "rendering", "mixing"].includes(job.status);
  const buttons = [];
  if (working) buttons.push(`<button data-act="stop" class="danger">Stop</button>`);
  else {
    buttons.push(`<button data-act="render" class="primary">${job.status === "done" ? "Render again" : "Render"}</button>`);
    buttons.push(`<button data-act="save">Save changes</button>`);
    buttons.push(`<button data-act="retag">Read and cast again</button>`);
  }
  if (job.status === "done") buttons.push(`<a href="/api/jobs/${job.id}/audio" download><button>Download ${job.format.toUpperCase()}</button></a>`);
  if (!working) buttons.push(`<button data-act="delete" class="danger">Delete</button>`);
  a.innerHTML = buttons.join("");
  $$("button[data-act]", a).forEach((b) => b.addEventListener("click", () => jobAction(b.dataset.act)));
}

async function saveChanges() {
  const body = {
    characters: dirty.characters,
    segments: Object.entries(dirty.segments).map(([id, v]) => ({ id: Number(id), ...v })),
  };
  if (dirty.narrator_voice) body.narrator_voice = dirty.narrator_voice;
  if (!Object.keys(body.characters).length && !body.segments.length && !body.narrator_voice) return;
  currentScript = await api(`/api/jobs/${currentJob.id}/script`, json("PUT", body));
  resetDirty();
}

async function jobAction(act) {
  const id = currentJob.id;
  try {
    if (act === "save") { await saveChanges(); toast("Saved"); renderScript(); return; }
    if (act === "render") { await saveChanges(); await api(`/api/jobs/${id}/render`, { method: "POST" }); }
    if (act === "retag") {
      if (!confirm("Read the story and cast it again? Your changes to voices, speakers and feelings will be replaced.")) return;
      await api(`/api/jobs/${id}/retag`, { method: "POST" });
    }
    if (act === "stop") await api(`/api/jobs/${id}/stop`, { method: "POST" });
    if (act === "delete") {
      if (!confirm("Delete this story and its audio?")) return;
      await api(`/api/jobs/${id}`, { method: "DELETE" });
      location.hash = "stories";
      return;
    }
    currentJob = null;
    await refreshJob(id);
  } catch (e) { toast(e.message); }
}

async function loadScript(id) {
  await ensureVoices();
  currentScript = await api(`/api/jobs/${id}/script`);
  const sel = $("#chapter-filter");
  sel.innerHTML = currentScript.chapters.map((c, i) => `<option value="${i}">${esc(c.title)}</option>`).join("");
  renderScript();
}

function voiceOptions(selected) {
  const opt = (v) => `<option value="${esc(v.id)}"${v.id === selected ? " selected" : ""}>${esc(v.name)}${voiceMeta(v) ? ` (${voiceMeta(v)})` : ""}</option>`;
  let html = `<optgroup label="Chatterbox (expressive)">${voiceData.chatterbox.map(opt).join("")}</optgroup>`;
  if (voiceData.kokoro.length) html += `<optgroup label="Kokoro (fast)">${voiceData.kokoro.map(opt).join("")}</optgroup>`;
  const known = [...voiceData.chatterbox, ...voiceData.kokoro].some((v) => v.id === selected);
  if (selected && !known) html = `<option value="${esc(selected)}" selected>${esc(selected)} (not found)</option>` + html;
  return html;
}

function renderScript() {
  if (!currentScript) return;
  const chars = currentScript.characters || {};
  const narrator = dirty.narrator_voice || currentScript.narrator_voice;
  const rows = [`<tr><td><b>Narrator</b></td><td></td><td></td><td><select data-narrator>${voiceOptions(narrator)}</select></td><td><button data-hear="Narrator">&#9654;</button></td></tr>`];
  Object.entries(chars).sort((a, b) => (b[1].lines || 0) - (a[1].lines || 0)).forEach(([name, c]) => {
    const d = dirty.characters[name] || {};
    const g = d.gender || c.gender || "unknown";
    rows.push(`<tr><td>${esc(name)}<div class="muted">${esc(c.description || "")}</div></td><td>${c.lines || 0}</td>
      <td><select data-gender="${esc(name)}">${["female", "male", "unknown"].map((x) => `<option${x === g ? " selected" : ""}>${x}</option>`).join("")}</select></td>
      <td><select data-voice="${esc(name)}">${voiceOptions(d.voice || c.voice)}</select></td>
      <td><button data-hear="${esc(name)}">&#9654;</button></td></tr>`);
  });
  $("#cast tbody").innerHTML = rows.join("");
  $$("#cast [data-voice]").forEach((s) => s.addEventListener("change", () => { (dirty.characters[s.dataset.voice] ||= {}).voice = s.value; }));
  $$("#cast [data-gender]").forEach((s) => s.addEventListener("change", () => { (dirty.characters[s.dataset.gender] ||= {}).gender = s.value; }));
  $("#cast [data-narrator]").addEventListener("change", (e) => { dirty.narrator_voice = e.target.value; });
  $$("#cast [data-hear]").forEach((b) => b.addEventListener("click", () => hearCharacter(b.dataset.hear, b)));
  renderLines();
}

function renderLines() {
  const chapter = Number($("#chapter-filter").value || 0);
  const dialogueOnly = $("#dialogue-only").checked;
  const names = ["Narrator", ...Object.keys(currentScript.characters || {})];
  const segs = currentScript.segments.filter((s) => s.chapter === chapter && (!dialogueOnly || s.kind === "dialogue"));
  $("#lines tbody").innerHTML = segs.map((s) => {
    if (s.kind === "pause") return `<tr class="pause"><td colspan="5" class="muted">Pause, ${s.seconds} s</td></tr>`;
    const d = dirty.segments[s.id] || {};
    const who = d.speaker || s.speaker, emo = d.emotion || s.emotion, inten = d.intensity ?? s.intensity;
    const whoSel = s.kind === "dialogue"
      ? `<select data-seg="${s.id}" data-field="speaker">${[...new Set([...names, who])].map((n) => `<option${n === who ? " selected" : ""}>${esc(n)}</option>`).join("")}<option value="__new">New character...</option></select>`
      : `<span class="muted">Narrator</span>`;
    return `<tr class="${s.kind}"><td>${whoSel}</td>
      <td><select data-seg="${s.id}" data-field="emotion">${voiceData.emotions.map((e) => `<option${e === emo ? " selected" : ""}>${e}</option>`).join("")}</select></td>
      <td><input type="range" min="0" max="1" step="0.1" value="${inten}" data-seg="${s.id}" data-field="intensity"></td>
      <td>${esc(s.text)}${s.direction ? `<div class="muted">[${esc(s.direction)}]</div>` : ""}</td><td><button data-play="${s.id}">&#9654;</button></td></tr>`;
  }).join("") || `<tr><td colspan="5" class="muted">No lines here.</td></tr>`;
  $$("#lines [data-seg]").forEach((el) => el.addEventListener("change", () => {
    let v = el.value;
    if (el.dataset.field === "speaker" && v === "__new") {
      v = prompt("Character name")?.trim();
      if (!v) { renderLines(); return; }
    }
    if (el.dataset.field === "intensity") v = Number(v);
    (dirty.segments[el.dataset.seg] ||= {})[el.dataset.field] = v;
    if (el.dataset.field === "speaker" && !names.includes(v)) {
      currentScript.characters[v] = { gender: "unknown", lines: 0 };
      renderScript();
    }
  }));
  $$("#lines [data-play]").forEach((b) => b.addEventListener("click", () => playSegment(Number(b.dataset.play), b)));
}
$("#chapter-filter").addEventListener("change", renderLines);
$("#dialogue-only").addEventListener("change", renderLines);

function segmentVoice(seg) {
  const d = dirty.segments[seg.id] || {};
  const who = d.speaker || seg.speaker;
  if (seg.kind !== "dialogue") return dirty.narrator_voice || currentScript.narrator_voice;
  const c = currentScript.characters[who] || {};
  return (dirty.characters[who] || {}).voice || c.voice || dirty.narrator_voice || currentScript.narrator_voice;
}

async function speak(text, voice, emotion, intensity, button) {
  const old = button?.innerHTML;
  if (button) { button.disabled = true; button.textContent = "..."; }
  try {
    const blob = await api("/api/preview", json("POST", { text, voice, emotion, intensity }));
    const audio = new Audio(URL.createObjectURL(blob));
    audio.play();
    return audio;
  } catch (e) { toast(e.message); } finally { if (button) { button.disabled = false; button.innerHTML = old; } }
}

function playSegment(id, button) {
  const seg = currentScript.segments.find((s) => s.id === id);
  const d = dirty.segments[id] || {};
  speak(seg.text, segmentVoice(seg), d.emotion || seg.emotion, d.intensity ?? seg.intensity, button);
}

function hearCharacter(name, button) {
  const seg = currentScript.segments.find((s) => (name === "Narrator" ? s.kind === "narration" && s.text : (dirty.segments[s.id]?.speaker || s.speaker) === name));
  if (!seg) { toast("No lines for them yet"); return; }
  playSegment(seg.id, button);
}

// ----- voices -----
async function ensureVoices(force) {
  if (force || !voiceData.emotions.length) voiceData = await api("/api/voices");
}

let narratorVoice = null;
let player = null;

async function loadVoices() {
  await ensureVoices(true);
  try { narratorVoice = (await api("/api/settings")).narrator_voice; } catch (e) { /* keep last */ }
  renderGallery();
  $("#try-voice").innerHTML = voiceOptions($("#try-voice").value || narratorVoice || "chatterbox:default");
  $("#try-emotion").innerHTML = voiceData.emotions.map((e) => `<option${e === ($("#try-emotion").value || "angry") ? " selected" : ""}>${e}</option>`).join("");
}

function allVoices() {
  return [
    ...voiceData.chatterbox.map((v) => ({ ...v, kind: "chatterbox" })),
    ...voiceData.kokoro.map((v) => ({ ...v, kind: "kokoro" })),
  ];
}

function voiceMeta(v) {
  return [v.accent, v.gender !== "unknown" ? v.gender : ""].filter(Boolean).join(", ");
}

function renderGallery() {
  const q = $("#voice-search").value.trim().toLowerCase();
  const gender = $("#voice-gender").value, kind = $("#voice-kind").value, english = $("#voice-english").checked;
  const list = allVoices().filter((v) =>
    (!q || `${v.name} ${v.accent || ""} ${v.description || ""} ${v.id}`.toLowerCase().includes(q)) &&
    (!gender || v.gender === gender) && (!kind || v.kind === kind) && (!english || v.english !== false));
  const current = allVoices().find((v) => v.id === narratorVoice);
  $("#narrator-now").innerHTML = current ? `Narrator voice now: <b>${esc(current.name)}</b>` : "";
  $("#kokoro-missing").hidden = voiceData.kokoro.length > 0;
  $("#voice-grid").innerHTML = list.map((v) => {
    const key = v.id.split(":")[1];
    const own = v.kind === "chatterbox" && key !== "default";
    const meta = [voiceMeta(v), v.kind === "chatterbox" ? v.description : ""].filter(Boolean).join(" · ");
    return `<div class="voice${v.id === narratorVoice ? " narrator" : ""}">
      <div class="name"><span>${esc(v.name)}</span><span class="kind ${v.kind === "chatterbox" ? "expressive" : ""}">${v.kind === "chatterbox" ? "Expressive" : "Fast"}</span></div>
      <div class="meta">${esc(meta)}${v.kind === "kokoro" ? ` <code>${esc(key)}</code>` : ""}</div>
      <div class="buttons">
        <button data-sample="${esc(v.id)}">&#9654; Play</button>
        ${v.id === narratorVoice ? `<button disabled>Narrator</button>` : `<button data-narrate="${esc(v.id)}">Use as narrator</button>`}
        ${own ? `<button data-clip="${esc(key)}" title="The clip you uploaded">Clip</button><button class="danger" data-del="${esc(key)}">Delete</button>` : ""}
      </div></div>`;
  }).join("") || `<p class="muted">No voices match.</p>`;
  $$("#voice-grid [data-sample]").forEach((b) => b.addEventListener("click", () => playSample(b)));
  $$("#voice-grid [data-narrate]").forEach((b) => b.addEventListener("click", async () => {
    try {
      await api("/api/settings", json("PUT", { narrator_voice: b.dataset.narrate }));
      narratorVoice = b.dataset.narrate;
      toast("Narrator voice saved. New stories will use it");
      renderGallery();
    } catch (e) { toast(e.message); }
  }));
  $$("#voice-grid [data-clip]").forEach((b) => b.addEventListener("click", () => playUrl(`/api/voices/${b.dataset.clip}/clip`, b)));
  $$("#voice-grid [data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this voice?")) return;
    await api(`/api/voices/${b.dataset.del}`, { method: "DELETE" });
    loadVoices();
  }));
}
["#voice-search", "#voice-gender", "#voice-kind", "#voice-english"].forEach((id) => $(id).addEventListener("input", renderGallery));

function stopPlayer() {
  if (!player) return;
  player.audio.pause();
  player.button.classList.remove("playing");
  player.button.innerHTML = player.label;
  player = null;
}

async function playUrl(url, button) {
  const same = player && player.button === button;
  stopPlayer();
  if (same) return;
  const label = button.innerHTML;
  button.disabled = true;
  button.textContent = "Making...";
  try {
    const r = await fetch(url);
    if (!r.ok) {
      let m = r.statusText;
      try { m = (await r.json()).detail; } catch (e) { /* not JSON */ }
      throw new Error(m);
    }
    const audio = new Audio(URL.createObjectURL(await r.blob()));
    player = { audio, button, label };
    button.classList.add("playing");
    button.innerHTML = "&#9632; Stop";
    audio.addEventListener("ended", stopPlayer);
    await audio.play();
  } catch (e) {
    toast(e.message);
    button.innerHTML = label;
  } finally {
    button.disabled = false;
  }
}

function playSample(button) {
  playUrl(`/api/voices/sample?voice=${encodeURIComponent(button.dataset.sample)}`, button);
}

$("#new-voice").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/voices", { method: "POST", body: new FormData(e.target) });
    e.target.reset();
    toast("Voice added");
    loadVoices();
  } catch (err) { toast(err.message); }
});

$("#try-go").addEventListener("click", async () => {
  $("#try-status").textContent = "Rendering (the first one loads the model, which can take a minute)...";
  try {
    const blob = await api("/api/preview", json("POST", {
      text: $("#try-text").value, voice: $("#try-voice").value,
      emotion: $("#try-emotion").value, intensity: Number($("#try-intensity").value),
    }));
    const a = $("#try-audio");
    a.src = URL.createObjectURL(blob);
    a.hidden = false;
    a.play();
    $("#try-status").textContent = "";
  } catch (e) { $("#try-status").textContent = e.message; }
});

// ----- settings -----
async function loadSettings() {
  await ensureVoices(true);
  const s = await api("/api/settings");
  const form = $("#settings");
  form.narrator_voice.innerHTML = voiceOptions(s.narrator_voice);
  for (const [k, v] of Object.entries(s)) {
    const el = form.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
}

$("#settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target, body = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    body[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  try {
    await api("/api/settings", json("PUT", body));
    $("#settings-status").textContent = "Saved";
    setTimeout(() => ($("#settings-status").textContent = ""), 2500);
  } catch (err) { toast(err.message); }
});

$$("[data-test]").forEach((b) => b.addEventListener("click", async () => {
  const out = $(`.test-result[data-for="${b.dataset.test}"]`);
  out.textContent = "Saving and testing...";
  out.className = "test-result";
  $("#settings").requestSubmit();
  await new Promise((r) => setTimeout(r, 400));
  try {
    const r = await api(`/api/test/${b.dataset.test}`, { method: "POST" });
    out.textContent = r.message;
    out.className = `test-result ${r.ok ? "ok" : "bad"}`;
  } catch (e) { out.textContent = e.message; out.className = "test-result bad"; }
}));

loadStatus();
setInterval(loadStatus, 30000);
route();
