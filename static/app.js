/* Lamp remote - frontend.
 *
 * One source of truth: the snapshot the server pushes over /events (Server-Sent
 * Events) whenever something changes. Every render() draws the whole UI from
 * that snapshot. Actions are plain fetch() calls; their effect arrives as the
 * next snapshot, so the UI always shows what the lamps really report.
 */
"use strict";

// Errors are collected for automated UI checks (and are handy in the console).
window.__errors = [];
window.addEventListener("error", (e) => window.__errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => window.__errors.push(String(e.reason)));

const SVGNS = "http://www.w3.org/2000/svg";
const LONG_PRESS_MS = 450;
const PLUG_LAMP_MIRED = 370; // lamps on plugs have their own fixed light; draw them warm

let config = null;
let snap = null;
let connected = false;
let selection = null; // {type: "lamp" | "room", id}
let sheetRender = null;
let sliderActive = false; // don't re-render the sheet while a slider is being dragged
let colourDragging = false;
let lastBannerKey = "";

const lampEls = {};
const roomEls = {};
const lampByName = {};
const roomByName = {};
const phaseNames = { kalt: "Kalt", warm: "Warm", twilight: "Twilight", night: "Night" };

// ------------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function button(label, onclick, cls = "") {
  return el("button", { class: cls, onclick }, label);
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["content-type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch (_) { /* empty body */ }
  if (!response.ok) {
    let message = response.statusText;
    if (data && data.detail) {
      message = typeof data.detail === "string" ? data.detail
        : Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(" ")
        : (data.detail.errors || []).join(" ");
    }
    throw new Error(message);
  }
  return data;
}

async function act(method, path, body) {
  try {
    return await api(method, path, body);
  } catch (err) {
    toast(err.message || "Fehler");
    return null;
  }
}

let toastTimer = null;
function toast(message) {
  let node = $("#toast");
  if (!node) {
    node = el("div", { id: "toast", class: "banner error" });
    node.style.cssText = "position:fixed;left:14px;right:14px;bottom:90px;z-index:20";
    document.body.append(node);
  }
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 3500);
}

const enc = encodeURIComponent;
const hhmm = (iso) => (iso ? iso.slice(11, 16) : "");
const pct = (level) => Math.max(1, Math.round((level / 254) * 100));
const phaseName = (key) => phaseNames[key] || key;

/** Colour temperature (Kelvin) -> sRGB, Tanner Helland's approximation. */
function kelvinRgb(kelvin) {
  const t = kelvin / 100;
  let r, g, b;
  if (t <= 66) {
    r = 255;
    g = 99.4708025861 * Math.log(t) - 161.1195681661;
    b = t <= 19 ? 0 : 138.5177312231 * Math.log(t - 10) - 305.0447927307;
  } else {
    r = 329.698727446 * Math.pow(t - 60, -0.1332047592);
    g = 288.1221695283 * Math.pow(t - 60, -0.0755148492);
    b = 255;
  }
  const clamp = (v) => Math.round(Math.min(255, Math.max(0, v)));
  return [clamp(r), clamp(g), clamp(b)];
}

function miredColour(mired) {
  const [r, g, b] = kelvinRgb(1e6 / mired);
  return `rgb(${r}, ${g}, ${b})`;
}

const DAY_ORDER = ["kalt", "warm", "twilight", "night"];
function scenesInDayOrder() {
  return [...config.scenes].sort((a, b) => DAY_ORDER.indexOf(a.key) - DAY_ORDER.indexOf(b.key));
}

function roomState(room) {
  return snap.state.rooms.find((r) => r.room === room);
}

// ------------------------------------------------------------------ floor plan

async function initPlan() {
  const text = await (await fetch("/floorplan.svg")).text();
  const parsed = new DOMParser().parseFromString(text, "image/svg+xml");
  const root = document.importNode(parsed.documentElement, true);
  root.removeAttribute("width");
  root.removeAttribute("height");
  root.setAttribute("preserveAspectRatio", "xMidYMid meet");
  // The plan's own <style> would apply to the whole page once inlined (e.g. ".name");
  // style.css has scoped replacements under #plan.
  for (const style of root.querySelectorAll("style")) style.remove();
  $("#plan").append(root);

  for (const room of config.rooms) {
    const path = root.querySelector(`#${CSS.escape(room.svg_id)}`);
    if (!path) continue;
    path.classList.add("room");
    path.addEventListener("click", () => openRoom(room.room));
    roomEls[room.room] = path;
    const label = [...root.querySelectorAll("text.name")].find((t) => isInside(path, t));
    if (label) label.textContent = room.display_name;
  }

  const group = svg("g", { id: "lamps" });
  root.append(group);
  for (const lamp of config.lamps) {
    const node = drawLamp(lamp);
    group.append(node);
    lampEls[lamp.name] = node;
  }
  // Room names on top of the lamps (they don't take taps, see CSS).
  const labels = root.querySelector("#labels");
  if (labels) root.append(labels);
}

function isInside(path, text) {
  try {
    const box = path.getBBox();
    const x = Number(text.getAttribute("x"));
    const y = Number(text.getAttribute("y"));
    return x >= box.x && x <= box.x + box.width && y >= box.y && y <= box.y + box.height;
  } catch (_) {
    return false;
  }
}

function drawLamp(lamp) {
  const group = svg("g", { class: "lamp unknown" });
  const shapes = [];
  const hits = [];
  for (const part of lamp.geometry || []) {
    if (lamp.shape === "point") {
      const [x, y] = part;
      shapes.push(svg("circle", { cx: x, cy: y, r: lamp.mount === "vertical" ? 15 : 13, "stroke-width": 4 }));
      hits.push(svg("circle", { cx: x, cy: y, r: 34 }));
    } else if (lamp.shape === "line") {
      const points = part.map((p) => p.join(",")).join(" ");
      shapes.push(svg("polyline", {
        points, fill: "none", "stroke-width": 12, "stroke-linecap": "round", "stroke-linejoin": "round",
      }));
      hits.push(svg("polyline", { points, fill: "none", "stroke-width": 46, "stroke-linecap": "round" }));
    } else if (lamp.shape === "rect") {
      const [x, y, w, h] = part;
      shapes.push(svg("rect", { x, y, width: w, height: h, rx: 5, "stroke-width": 4 }));
      hits.push(svg("rect", { x: x - 14, y: y - 14, width: w + 28, height: h + 28, rx: 12 }));
    }
  }
  for (const shape of shapes) { shape.classList.add("shape"); group.append(shape); }
  for (const hit of hits) { hit.classList.add("hit"); hit.setAttribute("pointer-events", "all"); group.append(hit); }
  attachPress(group, () => tapLamp(lamp.name), () => openLamp(lamp.name));
  return group;
}

/** Tap vs. long press on touch and mouse. */
function attachPress(node, onTap, onLong) {
  let timer = null;
  let startX = 0;
  let startY = 0;
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
  node.addEventListener("pointerdown", (e) => {
    startX = e.clientX; startY = e.clientY;
    cancel();
    timer = setTimeout(() => {
      timer = null;
      if (navigator.vibrate) navigator.vibrate(15);
      onLong();
    }, LONG_PRESS_MS);
  });
  node.addEventListener("pointermove", (e) => {
    if (Math.hypot(e.clientX - startX, e.clientY - startY) > 12) cancel();
  });
  node.addEventListener("pointerup", () => { if (timer) { cancel(); onTap(); } });
  node.addEventListener("pointercancel", cancel);
  node.addEventListener("pointerleave", cancel);
  node.addEventListener("contextmenu", (e) => e.preventDefault());
  node.addEventListener("click", (e) => e.stopPropagation());
}

function paintLamp(name) {
  const group = lampEls[name];
  const device = snap.state.devices[name];
  const lamp = lampByName[name];
  if (!group || !device) return;
  const on = device.state === "ON";
  const unknown = device.state === null || device.state === undefined;
  const selected = selection && selection.type === "lamp" && selection.id === name;
  group.classList.toggle("on", on);
  group.classList.toggle("off", !on && !unknown);
  group.classList.toggle("unknown", unknown);
  group.classList.toggle("locked", Boolean(device.locked));

  let colour = "#4a525d";
  let opacity = 0.85;
  let glow = "";
  if (on) {
    const mired = !lamp.has_colour ? PLUG_LAMP_MIRED
      : device.color_temp || snap.schedule.colour.mired;
    colour = miredColour(mired);
    const level = lamp.dimmable && device.brightness ? device.brightness / 254 : 1;
    opacity = 0.4 + 0.6 * level;
    glow = `drop-shadow(0 0 ${4 + 12 * level}px ${colour})`;
  }
  for (const shape of group.querySelectorAll(".shape")) {
    const isLine = shape.tagName.toLowerCase() === "polyline";
    shape.style.stroke = selected && !isLine ? "#ffffff" : colour;
    shape.style.fill = isLine || unknown ? "none" : colour;
    shape.style.opacity = opacity;
    shape.style.filter = glow;
  }
}

function tapLamp(name) {
  const device = snap && snap.state.devices[name];
  if (!device) return;
  if (device.locked) { openLamp(name); return; }
  act("POST", `/api/lamps/${enc(name)}`, { on: device.state !== "ON" });
}

// ------------------------------------------------------------------ bottom sheet

function openSheet(sel, render) {
  selection = sel;
  sheetRender = render;
  renderSheet();
  document.body.classList.add("sheet-open");
  paintSelection();
}

function closeSheet() {
  selection = null;
  sheetRender = null;
  document.body.classList.remove("sheet-open");
  paintSelection();
}

function renderSheet() {
  if (!sheetRender || !snap) return;
  $("#sheet-body").replaceChildren(...sheetRender());
}

function paintSelection() {
  if (!snap) return;
  for (const name of Object.keys(lampEls)) paintLamp(name);
  for (const [room, path] of Object.entries(roomEls)) {
    path.classList.toggle("selected", Boolean(selection && selection.type === "room" && selection.id === room));
  }
}

function slider(min, max, value, onCommit, format) {
  const out = el("span", { class: "value" }, format(value));
  const input = el("input", { type: "range", min, max, step: 1, value });
  input.addEventListener("pointerdown", () => { sliderActive = true; });
  input.addEventListener("input", () => { out.textContent = format(Number(input.value)); });
  input.addEventListener("change", async () => {
    await onCommit(Number(input.value));
    sliderActive = false;
  });
  return el("div", { class: "row" }, input, out);
}

function openLamp(name) {
  openSheet({ type: "lamp", id: name }, () => {
    const lamp = lampByName[name];
    const device = snap.state.devices[name];
    const room = roomByName[lamp.room];
    const on = device.state === "ON";
    const status = device.state === null ? "Zustand unbekannt"
      : on ? (lamp.dimmable && device.brightness ? `an · ${pct(device.brightness)} %` : "an") : "aus";
    const nodes = [el("h2", {}, lamp.display_name), el("div", { class: "sub" }, `${room.display_name} · ${status}`)];
    if (device.locked) {
      nodes.push(
        el("div", { class: "sunrise-note" }, "🌅 Sonnenaufgang läuft – diese Lampe ist bis zur Weckzeit gesperrt."),
        el("div", { class: "row" }, button("Sonnenaufgang abbrechen", () => act("POST", "/api/sunrise/cancel"), "danger")),
      );
      return nodes;
    }
    nodes.push(el("div", { class: "row" },
      button("Ein", () => act("POST", `/api/lamps/${enc(name)}`, { on: true }), on ? "primary" : ""),
      button("Aus", () => act("POST", `/api/lamps/${enc(name)}`, { on: false }), !on && device.state ? "primary" : ""),
    ));
    if (lamp.dimmable) {
      nodes.push(el("div", { class: "label" }, "Helligkeit"));
      nodes.push(slider(1, 254, device.brightness || 254,
        (v) => act("POST", `/api/lamps/${enc(name)}`, { brightness: v }), (v) => `${pct(v)} %`));
    } else {
      nodes.push(el("p", { class: "sub" }, "Lampe an einer Steckdose: nur ein/aus."));
    }
    return nodes;
  });
}

function openRoom(roomName) {
  openSheet({ type: "room", id: roomName }, () => {
    const room = roomByName[roomName];
    const state = roomState(roomName);
    const locked = state.lamps.some((n) => snap.state.devices[n] && snap.state.devices[n].locked);
    const parts = [state.on ? "an" : "aus"];
    if (state.on && state.scene) parts.push(`Szene ${phaseName(state.scene)}`);
    if (state.on && Math.abs(state.factor - 1) > 0.005) parts.push(`gedimmt ${Math.round(state.factor * 100)} %`);
    const base = `/api/rooms/${enc(roomName)}`;
    const nodes = [
      el("h2", {}, room.display_name),
      el("div", { class: "sub" }, parts.join(" · ")),
      el("div", { class: "row" },
        button("Ein", () => act("POST", `${base}/on`), ""),
        button("Aus", () => act("POST", `${base}/off`), "")),
      el("div", { class: "row" },
        button("− Dunkler", () => act("POST", `${base}/dim`, { direction: "down" })),
        button("+ Heller", () => act("POST", `${base}/dim`, { direction: "up" }))),
      el("div", { class: "label" }, "Szene"),
      el("div", { class: "chips" }, scenesInDayOrder().map((s) =>
        button(s.display_name, () => act("POST", `${base}/scene`, { scene: s.key }),
          `chip${state.on && state.scene === s.key ? " active" : ""}`))),
    ];
    if (locked) {
      nodes.push(el("div", { class: "sunrise-note" },
        "🌅 Sonnenaufgang läuft – Bodenstrips, Schreibtischlampe und Deckenlampe sind gesperrt."));
      nodes.push(el("div", { class: "row" }, button("Sonnenaufgang abbrechen", () => act("POST", "/api/sunrise/cancel"), "danger")));
    }
    return nodes;
  });
}

// ------------------------------------------------------------------ phase bar / colour

function renderPhase() {
  const s = snap.schedule;
  const bar = $("#phase-bar");
  const start = new Date(s.segments[0].start).getTime();
  const end = new Date(s.segments[s.segments.length - 1].end).getTime();
  const span = end - start;
  const nodes = s.segments.map((seg) => {
    const left = ((new Date(seg.start).getTime() - start) / span) * 100;
    const width = ((new Date(seg.end).getTime() - new Date(seg.start).getTime()) / span) * 100;
    const node = el("div", { class: `seg ${seg.phase}`, title: `${phaseName(seg.phase)} ${hhmm(seg.start)}–${hhmm(seg.end)}` });
    node.style.left = `${left}%`;
    node.style.width = `${width}%`;
    return node;
  });
  const marker = el("div", { class: "marker" });
  marker.style.left = `${((new Date(s.now).getTime() - start) / span) * 100}%`;
  bar.replaceChildren(...nodes, marker);

  const text = $("#phase-text");
  text.replaceChildren(el("b", {}, phaseName(s.phase)));
  if (s.next_boundary) text.append(` · ${phaseName(s.next_boundary.phase)} ab ${hhmm(s.next_boundary.at)}`);
  if (s.colour.ramping) text.append(" · Farbübergang");

  const next = $("#next-phase");
  next.disabled = !s.can_advance;
  next.textContent = s.can_advance ? `→ ${phaseName(s.next_phase)}` : "Nacht";
  next.title = s.can_advance ? `Jetzt zu ${phaseName(s.next_phase)} wechseln` : "In der Nacht gibt es keine nächste Phase";
}

function renderColour() {
  const c = snap.schedule.colour;
  const input = $("#colour");
  if (!colourDragging) input.value = c.master_ct;
  const mode = $("#colour-mode");
  mode.textContent = c.override ? "Manuell · Auto ↺" : "Auto";
  mode.classList.toggle("manual", c.override);
  mode.disabled = !c.override;
}

// ------------------------------------------------------------------ banners

function renderBanners() {
  const items = [];
  if (!connected) items.push(["error", "Keine Verbindung zum Server – verbinde neu …"]);
  if (snap) {
    const sys = snap.system;
    if (connected && !sys.mqtt_connected) items.push(["error", "Keine Verbindung zum MQTT-Broker."]);
    else if (connected && !sys.z2m_online) items.push(["error", "Zigbee2MQTT ist offline."]);
    const sr = snap.sunrise;
    if (sr.running) items.push(["warn", sr.test ? "🌅 Sonnenaufgang-Test läuft" : "🌅 Sonnenaufgang läuft", "Abbrechen", "/api/sunrise/cancel"]);
    if (sr.warning) {
      items.push(["warn", `Sonnenaufgang hatte Probleme: ${sr.warning.problems.join("; ")}`, "Ausblenden", "/api/sunrise/dismiss"]);
    }
    if (sys.dry_run) items.push(["", "Testbetrieb: Befehle werden nur protokolliert (DRY_RUN)."]);
  }
  const key = JSON.stringify(items);
  if (key === lastBannerKey) return;
  lastBannerKey = key;
  $("#banners").replaceChildren(...items.map(([cls, text, label, path]) =>
    el("div", { class: `banner ${cls}` },
      el("span", { class: "grow" }, text),
      label ? button(label, () => act("POST", path)) : null)));
}

// ------------------------------------------------------------------ plugs tab

function renderPlugs() {
  const plugs = config.plugs.filter((p) => !p.is_lamp);
  $("#plugs-page").replaceChildren(
    ...plugs.map((p) => {
      const d = snap.state.devices[p.name] || {};
      const watts = d.power === null || d.power === undefined ? "–" : Math.round(d.power);
      const state = d.state === "ON" ? "an" : d.state === "OFF" ? "aus" : "noch keine Meldung";
      return el("div", { class: "card plug" },
        el("div", { class: "name" }, p.display_name, el("small", {}, `Steckdose ${state}`)),
        el("div", { class: "watts" }, String(watts), el("small", {}, " W")));
    }),
    el("p", { class: "hint", style: "color:var(--muted);font-size:13px" },
      "Nur Anzeige – diese Steckdosen werden hier bewusst nicht geschaltet. Verlauf folgt."),
  );
}

// ------------------------------------------------------------------ settings tab

const settingsStatus = {};

function field(labelText, control, todayId) {
  return el("div", { class: "field" },
    el("label", {}, labelText, todayId ? el("span", { class: "today", id: todayId }) : null),
    control);
}

function timeInput(id, onSave) {
  const input = el("input", { type: "time", id, required: true });
  input.addEventListener("change", () => { if (input.value) onSave(input.value); });
  return input;
}

function numberInput(id, min, max, step, onSave) {
  const input = el("input", { type: "number", id, min, max, step, inputmode: "decimal" });
  input.addEventListener("change", () => { if (input.value !== "") onSave(Number(input.value)); });
  return input;
}

function toggle(id, onSave) {
  const input = el("input", { type: "checkbox", id });
  input.addEventListener("change", () => onSave(input.checked));
  return el("label", { class: "switch" }, input, el("span"));
}

function statusLine(card) {
  const node = el("div", { id: `${card}-status` });
  settingsStatus[card] = node;
  return node;
}

async function save(card, method, path, body) {
  const node = settingsStatus[card];
  try {
    await api(method, path, body);
    node.className = "saved";
    node.textContent = "Gespeichert ✓";
    setTimeout(() => { if (node.textContent === "Gespeichert ✓") node.textContent = ""; }, 2000);
  } catch (err) {
    node.className = "errors";
    node.textContent = err.message;
  }
}

const saveSchedule = (body) => save("phases", "PUT", "/api/schedule/settings", body);

function buildSettings() {
  const phaseField = (key, label) => field(label, timeInput(`set-${key}`, (v) => saveSchedule({ [key]: v })), `today-${key}`);
  $("#settings-page").replaceChildren(
    el("div", { class: "card" },
      el("h3", {}, "Tagesphasen"),
      el("p", { class: "hint" }, "Beginn jeder Phase. Mit „Nach Sonnenstand“ beginnt Warm beim Sonnenuntergang (18–21 Uhr) und Twilight eine Stunde danach (21–22 Uhr)."),
      phaseField("kalt", "Kalt"),
      phaseField("warm", "Warm"),
      phaseField("twilight", "Twilight"),
      phaseField("night", "Night"),
      field("Nach Sonnenstand", toggle("set-sun", (v) => saveSchedule({ sun_mode: v }))),
      statusLine("phases"),
    ),
    el("div", { class: "card" },
      el("h3", {}, "Lichtfarbe"),
      el("p", { class: "hint" }, "Übergang zu warm ab Beginn von Warm, zu kalt ab Beginn von Kalt."),
      field("Warm-Übergang (Min.)", numberInput("set-warm-ramp", 0, 240, 5, (v) => save("colour", "PUT", "/api/schedule/settings", { warm_ramp_min: v }))),
      field("Kalt-Übergang (Min.)", numberInput("set-kalt-ramp", 0, 240, 5, (v) => save("colour", "PUT", "/api/schedule/settings", { kalt_ramp_min: v }))),
      el("div", { class: "status-line", id: "colour-state" }),
      el("div", { class: "buttons" }, el("button", { id: "colour-auto", onclick: () => save("colour", "DELETE", "/api/colour") }, "Zurück zu Automatik")),
      statusLine("colour"),
    ),
    el("div", { class: "card" },
      el("h3", {}, "Sonnenaufgang"),
      el("p", { class: "hint" }, "Animierter Sonnenaufgang im Schlafzimmer, der zur Weckzeit endet. Danach geht in allen Räumen „Kalt“ an."),
      field("Sonnenaufgang", toggle("set-sr-enabled", (v) => save("sunrise", "PUT", "/api/sunrise/settings", { enabled: v }))),
      field("Weckzeit", timeInput("set-sr-wake", (v) => save("sunrise", "PUT", "/api/sunrise/settings", { wake: v }))),
      field("Dauer (Min.)", numberInput("set-sr-duration", 5, 120, 5, (v) => save("sunrise", "PUT", "/api/sunrise/settings", { duration_min: v }))),
      el("div", { class: "status-line", id: "sunrise-state" }),
      el("div", { class: "buttons", id: "sunrise-buttons" }),
      statusLine("sunrise"),
    ),
    el("div", { class: "card" },
      el("h3", {}, "Standort"),
      el("p", { class: "hint" }, "Für die Sonnenuntergangszeit (wird lokal berechnet)."),
      field("Breitengrad", numberInput("set-lat", -90, 90, 0.01, (v) => save("location", "PUT", "/api/schedule/settings", { latitude: v }))),
      field("Längengrad", numberInput("set-lon", -180, 180, 0.01, (v) => save("location", "PUT", "/api/schedule/settings", { longitude: v }))),
      statusLine("location"),
    ),
    el("div", { class: "card" },
      el("h3", {}, "System"),
      el("div", { id: "system-lines" }),
    ),
  );
}

function setValue(id, value) {
  const input = document.getElementById(id);
  if (!input || document.activeElement === input) return; // don't overwrite while editing
  if (input.type === "checkbox") input.checked = Boolean(value);
  else if (String(input.value) !== String(value)) input.value = value;
}

function fillSettings() {
  const st = snap.schedule.settings;
  for (const key of ["kalt", "warm", "twilight", "night"]) setValue(`set-${key}`, st[key]);
  setValue("set-sun", st.sun_mode);
  setValue("set-warm-ramp", st.warm_ramp_min);
  setValue("set-kalt-ramp", st.kalt_ramp_min);
  setValue("set-lat", st.latitude);
  setValue("set-lon", st.longitude);

  // today's effective start of each phase (sun mode, early wake-up, manual advance)
  const firstStart = {};
  for (const seg of snap.schedule.segments.slice(1)) { // [0] is the night carried over from 00:00
    if (!(seg.phase in firstStart)) firstStart[seg.phase] = seg.start;
  }
  for (const key of ["kalt", "warm", "twilight", "night"]) {
    const node = document.getElementById(`today-${key}`);
    const today = firstStart[key] && hhmm(firstStart[key]);
    if (node) node.textContent = today && today !== st[key] ? `heute ${today}` : "";
  }

  const c = snap.schedule.colour;
  $("#colour-state").textContent = c.override ? "Farbe manuell eingestellt (bis zum nächsten Übergang)." : "Farbe folgt automatisch den Tagesphasen.";
  $("#colour-auto").disabled = !c.override;

  const sr = snap.sunrise;
  setValue("set-sr-enabled", sr.settings.enabled);
  setValue("set-sr-wake", sr.settings.wake);
  setValue("set-sr-duration", sr.settings.duration_min);
  let line = "Aus.";
  if (sr.running) line = sr.test ? "Test läuft …" : "Läuft gerade.";
  else if (sr.next) line = `Nächster Lauf: ${dayWord(sr.next.start)} ${hhmm(sr.next.start)}–${hhmm(sr.next.wake)}`;
  else if (sr.settings.enabled && sr.today) line = sr.today.status === "cancelled" ? "Heute abgebrochen, morgen wieder." : "Heute erledigt, morgen wieder.";
  $("#sunrise-state").textContent = line;
  const key = `${sr.running}`;
  const box = $("#sunrise-buttons");
  if (box.dataset.key !== key) {
    box.dataset.key = key;
    box.replaceChildren(sr.running
      ? button("Abbrechen", () => save("sunrise", "POST", "/api/sunrise/cancel"), "danger")
      : button("Test (ca. 1 Minute)", () => save("sunrise", "POST", "/api/sunrise/test"), ""));
  }

  const sys = snap.system;
  const dot = (ok) => el("span", { class: `dot${ok ? " ok" : ""}` });
  $("#system-lines").replaceChildren(
    el("div", { class: "status-line" }, dot(sys.mqtt_connected), "MQTT-Broker"),
    el("div", { class: "status-line" }, dot(sys.z2m_online), "Zigbee2MQTT"),
    el("div", { class: "status-line" }, dot(!sys.dry_run), sys.dry_run ? "Testbetrieb (DRY_RUN): nichts wird gesendet" : "Befehle werden gesendet"),
    el("div", { class: "status-line" }, dot(sys.buttons_enabled), sys.buttons_enabled ? "Wandschalter aktiv" : "Wandschalter aus (alter Hub zuständig)"),
  );
}

function dayWord(iso) {
  const day = iso.slice(0, 10);
  const today = snap.schedule.now.slice(0, 10);
  if (day === today) return "heute";
  const tomorrow = new Date(new Date(today + "T12:00:00").getTime() + 86400000).toISOString().slice(0, 10);
  return day === tomorrow ? "morgen" : day;
}

// ------------------------------------------------------------------ render / wiring

function render() {
  if (!snap || !config) return;
  renderPhase();
  for (const room of config.rooms) {
    const path = roomEls[room.room];
    if (path) path.classList.toggle("lit", roomState(room.room).on);
  }
  for (const name of Object.keys(lampEls)) paintLamp(name);
  renderColour();
  renderBanners();
  renderPlugs();
  fillSettings();
  if (sheetRender && !sliderActive) renderSheet();
}

function connect() {
  const source = new EventSource("/events");
  source.onmessage = (event) => {
    connected = true;
    snap = JSON.parse(event.data);
    render();
  };
  source.onerror = () => {
    connected = false;
    renderBanners();
  };
}

function wireControls() {
  for (const tab of document.querySelectorAll("nav#tabs button")) {
    tab.addEventListener("click", () => {
      for (const t of document.querySelectorAll("nav#tabs button")) t.classList.toggle("active", t === tab);
      for (const section of document.querySelectorAll(".tab")) {
        section.classList.toggle("active", section.id === `tab-${tab.dataset.tab}`);
      }
      closeSheet();
    });
  }
  $("#sheet-backdrop").addEventListener("click", closeSheet);
  $("#next-phase").addEventListener("click", () => act("POST", "/api/schedule/next"));
  $("#all-off").addEventListener("click", () => act("POST", "/api/all-off"));
  const colour = $("#colour");
  colour.addEventListener("input", () => { colourDragging = true; });
  colour.addEventListener("change", async () => {
    await act("POST", "/api/colour", { master_ct: Number(colour.value) });
    colourDragging = false;
  });
  $("#colour-mode").addEventListener("click", () => act("DELETE", "/api/colour"));
}

async function init() {
  config = await api("GET", "/api/config");
  for (const lamp of config.lamps) lampByName[lamp.name] = lamp;
  for (const room of config.rooms) roomByName[room.room] = room;
  for (const scene of config.scenes) phaseNames[scene.key] = scene.display_name;
  wireControls();
  buildSettings();
  await initPlan();
  connect();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* needs HTTPS; fine on plain http */ });
  }
}

init().catch((err) => {
  window.__errors.push(String(err));
  toast(`Start fehlgeschlagen: ${err.message}`);
});
