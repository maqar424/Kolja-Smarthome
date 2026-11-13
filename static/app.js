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
const ALL_ROOMS = "*"; // selection id of the "Alle Zimmer" sheet

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

/** Display colour for a colour temperature (mired), for markers and glows.
 *
 * Not a physical conversion: real 6500 K light is nearly pure white and would
 * look grey on the dark plan. Instead a small palette, the same idea as the
 * colour slider: light blue (cold, 153) -> neutral (about 4000 K) -> orange
 * (warm, 370), linear in between. */
const MIRED_PALETTE = [
  [153, [150, 190, 255]],
  [250, [255, 240, 220]],
  [370, [255, 160, 50]],
];
function miredColour(mired) {
  const m = Math.min(370, Math.max(153, mired));
  for (let i = 1; i < MIRED_PALETTE.length; i++) {
    const [m1, c1] = MIRED_PALETTE[i];
    if (m <= m1) {
      const [m0, c0] = MIRED_PALETTE[i - 1];
      const t = (m - m0) / (m1 - m0);
      const [r, g, b] = c0.map((c, k) => Math.round(c + (c1[k] - c) * t));
      return `rgb(${r}, ${g}, ${b})`;
    }
  }
  return "rgb(255, 160, 50)";
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
    path.classList.add("room"); // taps on the plan only switch lamps; rooms open from the list
    roomEls[room.room] = path;
    const label = [...root.querySelectorAll("text.name")].find((t) => isInside(path, t));
    if (label) label.textContent = room.display_name;
  }

  buildRoomList();
  buildLight(root);

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

/** Top left of the plan: "Alle Zimmer" and every room, each opens its sheet. */
const roomListEls = {};

function buildRoomList() {
  const list = $("#room-list");
  const entries = [{ id: ALL_ROOMS, name: "Alle Zimmer", open: openAll }]
    .concat(config.rooms.map((r) => ({ id: r.room, name: r.display_name, open: () => openRoom(r.room) })));
  for (const entry of entries) {
    const node = el("button", { class: "room-item", onclick: entry.open }, el("span", { class: "dot-lit" }), entry.name);
    roomListEls[entry.id] = node;
    list.append(node);
  }
}

function paintRoomList() {
  for (const [id, node] of Object.entries(roomListEls)) {
    const lit = id === ALL_ROOMS ? snap.state.rooms.some((r) => r.on) : roomState(id).on;
    node.classList.toggle("lit", lit);
    node.classList.toggle("selected", Boolean(selection && selection.type === "room" && selection.id === id));
  }
}

// ------------------------------------------------------------------ live lighting

/* A rough picture of the light (spec §8 "Live lighting rendering"). Each lamp
 * owns one glow, drawn between the floor and the walls and clipped to its room,
 * so walls block it. Glows use "screen" blending: where two lamps overlap the
 * floor gets brighter, never darker. Points and panels are radial gradients,
 * lines and strips a blurred band. Nothing animates continuously: on a state
 * change only opacity and colour change, and CSS fades them (0.6 s). */
const GLOW = { // sizes in cm (1 SVG unit = 1 cm)
  ceiling: { r: 300, band: 90, blur: 55, peak: 0.55 }, // wide and soft: lights most of the room
  middle: { r: 160, band: 40, blur: 30, peak: 0.7 },
  floor: { r: 100, band: 24, blur: 16, peak: 0.85 }, // narrow, strong near the source
  vertical: { r: 190, band: 50, blur: 35, peak: 0.65 },
};
const glowEls = {};

function buildLight(root) {
  const defs = svg("defs");
  root.insertBefore(defs, root.firstChild);
  const layer = svg("g", { id: "light" });
  const floors = root.querySelector("#floors");
  if (floors) floors.after(layer); else root.append(layer);

  for (const room of config.rooms) {
    const clip = svg("clipPath", { id: `clip-${room.svg_id}` });
    clip.append(svg("use", { href: `#${room.svg_id}` }));
    defs.append(clip);
  }

  for (const lamp of config.lamps) {
    const spec = GLOW[lamp.mount] || GLOW.middle;
    const room = roomByName[lamp.room];
    const id = lamp.name.replace(/[^A-Za-z0-9_-]/g, "_");
    const glow = svg("g", { class: "glow", ...(room ? { "clip-path": `url(#clip-${room.svg_id})` } : {}) });
    const stops = [];
    const bands = [];
    if (lamp.shape === "line") {
      (lamp.geometry || []).forEach((part, i) => {
        const xs = part.map((p) => p[0]);
        const ys = part.map((p) => p[1]);
        const pad = spec.band / 2 + 3 * spec.blur;
        // Filter region in user space and only around this strip: a blur over
        // the whole plan would be far more work for the phone.
        const filter = svg("filter", {
          id: `blur-${id}-${i}`, filterUnits: "userSpaceOnUse",
          x: Math.min(...xs) - pad, y: Math.min(...ys) - pad,
          width: Math.max(...xs) - Math.min(...xs) + 2 * pad, height: Math.max(...ys) - Math.min(...ys) + 2 * pad,
        });
        filter.append(svg("feGaussianBlur", { stdDeviation: spec.blur }));
        defs.append(filter);
        const band = svg("polyline", {
          points: part.map((p) => p.join(",")).join(" "), fill: "none",
          "stroke-width": spec.band, "stroke-linecap": "round", "stroke-linejoin": "round",
          "stroke-opacity": spec.peak, filter: `url(#blur-${id}-${i})`,
        });
        bands.push(band);
        glow.append(band);
      });
    } else {
      const grad = svg("radialGradient", { id: `glow-${id}` });
      for (const [offset, factor] of [[0, 1], [0.35, 0.5], [0.7, 0.15], [1, 0]]) {
        const stop = svg("stop", { offset, "stop-opacity": spec.peak * factor });
        stops.push(stop);
        grad.append(stop);
      }
      defs.append(grad);
      for (const part of lamp.geometry || []) {
        let cx, cy, r = spec.r;
        if (lamp.shape === "rect") {
          const [x, y, w, h] = part;
          cx = x + w / 2; cy = y + h / 2; r += Math.max(w, h) / 2;
        } else {
          [cx, cy] = part;
        }
        glow.append(svg("circle", { cx, cy, r, fill: `url(#glow-${id})` }));
      }
    }
    layer.append(glow);
    glowEls[lamp.name] = { glow, stops, bands };
  }
}

/** level 0..1 (brightness), colour as CSS rgb(). Off keeps the last colour so it fades out in it. */
function paintGlow(name, on, colour, level) {
  const g = glowEls[name];
  if (!g) return;
  // sqrt: a lamp at 5 % is still clearly visible, like in a dark room
  g.glow.style.opacity = on ? (0.15 + 0.85 * Math.sqrt(level)).toFixed(3) : "0";
  if (!on) return;
  for (const stop of g.stops) stop.style.stopColor = colour;
  for (const band of g.bands) band.style.stroke = colour;
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
  let level = 0;
  if (on) {
    const mired = !lamp.has_colour ? PLUG_LAMP_MIRED
      : device.color_temp || snap.schedule.colour.mired;
    colour = miredColour(mired);
    level = lamp.dimmable && device.brightness ? device.brightness / 254 : 1;
    opacity = 0.55 + 0.45 * level;
  }
  paintGlow(name, on, colour, level);
  for (const shape of group.querySelectorAll(".shape")) {
    const isLine = shape.tagName.toLowerCase() === "polyline";
    shape.style.stroke = selected && !isLine ? "#ffffff" : colour;
    shape.style.fill = isLine || unknown ? "none" : colour;
    shape.style.opacity = opacity;
  }
}

function tapLamp(name) {
  const device = snap && snap.state.devices[name];
  if (!device) return;
  if (device.locked) { openLamp(name); return; }
  act("POST", `/api/lamps/${enc(name)}`, { on: device.state !== "ON" });
}

// ------------------------------------------------------------------ bottom sheet

/** The sheet opens above the bottom controls: on the floor plan above colour
 * slider and "Alles aus/an" (the global colour stays reachable), elsewhere
 * above the tab bar. Closed, it slides fully off-screen (style.css). */
function placeSheet() {
  const planActive = $("#tab-plan").classList.contains("active");
  // floor plan: above the colour slider; other tabs: above the tab bar
  const below = planActive ? $("#plan-controls") : $("nav#tabs");
  const offset = Math.max(0, window.innerHeight - below.getBoundingClientRect().top);
  document.documentElement.style.setProperty("--sheet-bottom", `${Math.round(offset)}px`);
  document.body.classList.toggle("sheet-above-controls", offset > 0);
}

function openSheet(sel, render) {
  placeSheet();
  selection = sel;
  sheetRender = render;
  renderSheet();
  document.body.classList.add("sheet-open");
  paintSelection();
}

function closeSheet() {
  selection = null;
  powerView = null;
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
  paintRoomList();
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

function openAll() {
  openSheet({ type: "room", id: ALL_ROOMS }, () => {
    const lit = snap.state.rooms.filter((r) => r.on);
    const sub = lit.length === 0 ? "alles aus"
      : lit.length === snap.state.rooms.length ? "alle Zimmer an"
        : `an: ${lit.map((r) => r.display_name).join(", ")}`;
    return [
      el("h2", {}, "Alle Zimmer"),
      el("div", { class: "sub" }, sub),
      el("div", { class: "row" },
        button("Ein", () => act("POST", "/api/all-on")),
        button("Aus", () => act("POST", "/api/all-off"))),
      el("div", { class: "row" },
        button("− Dunkler", () => act("POST", "/api/all/dim", { direction: "down" })),
        button("+ Heller", () => act("POST", "/api/all/dim", { direction: "up" }))),
      el("div", { class: "label" }, "Szene für alle Zimmer"),
      el("div", { class: "chips" }, scenesInDayOrder().map((s) =>
        button(s.display_name, () => act("POST", "/api/all/scene", { scene: s.key }),
          `chip${lit.length && lit.every((r) => r.scene === s.key) ? " active" : ""}`))),
    ];
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
  next.replaceChildren(el("small", {}, "→"), s.can_advance ? phaseName(s.next_phase) : "–");
  next.title = s.can_advance ? `Jetzt zu ${phaseName(s.next_phase)} wechseln` : "Nach der Nacht kommt der nächste Morgen";
  const prev = $("#prev-phase");
  prev.disabled = !s.can_go_back;
  prev.replaceChildren(el("small", {}, "←"), s.can_go_back ? phaseName(s.previous_phase) : "–");
  prev.title = s.can_go_back ? `Zurück zu ${phaseName(s.previous_phase)}` : "Kalt ist die erste Phase des Tages";
  $("#resume-schedule").hidden = !s.manual;
}

function renderAllButton() {
  const anyOn = snap.state.rooms.some((r) => r.on);
  const btn = $("#all-off");
  btn.textContent = anyOn ? "Alles aus" : "Alles an";
  btn.className = anyOn ? "danger" : "primary";
  btn.title = anyOn ? "Alle Lampen aus" : "Alle Zimmer an, in der Szene der aktuellen Phase";
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

const POWER_RANGES = [["6h", "6 Std."], ["24h", "24 Std."], ["7d", "7 Tage"], ["30d", "30 Tage"], ["1y", "1 Jahr"]];
let powerView = null; // {name, range, data, error} while the history sheet is open
let powerRange = "24h"; // remembered between openings

function fmtWatts(w) {
  if (w === null || w === undefined) return "–";
  if (Math.abs(w) >= 1000) return `${(w / 1000).toLocaleString("de-DE", { maximumFractionDigits: 2 })} kW`;
  return `${Math.round(w)} W`;
}

function plugCard(p, big) {
  const d = snap.state.devices[p.name] || {};
  const watts = d.power === null || d.power === undefined ? "–" : Math.round(d.power);
  const state = d.available === false ? "nicht erreichbar"
    : d.state === "ON" ? "an" : d.state === "OFF" ? "aus" : "noch keine Meldung";
  return el("div", { class: `card plug${big ? "" : " small"}`, role: "button", tabindex: "0", onclick: () => openPower(p.name) },
    el("div", { class: "name" }, p.display_name, el("small", {}, `${p.is_lamp ? "Lampe" : "Steckdose"} ${state}`)),
    el("div", { class: "watts" }, String(watts), el("small", {}, " W")),
    el("span", { class: "chev" }, "›"));
}

function renderPlugs() {
  const plugs = config.plugs.filter((p) => !p.is_lamp);
  const lampPlugs = config.plugs.filter((p) => p.is_lamp);
  $("#plugs-page").replaceChildren(
    ...plugs.map((p) => plugCard(p, true)),
    el("p", { class: "hint" }, "Nur Anzeige – diese Steckdosen werden hier bewusst nicht geschaltet. Tippen zeigt den Verlauf."),
    lampPlugs.length ? el("h4", { class: "section" }, "Lampen an Steckdosen") : null,
    ...lampPlugs.map((p) => plugCard(p, false)),
  );
}

async function loadPower() {
  const view = powerView;
  if (!view) return;
  try {
    const data = await api("GET", `/api/power/${encodeURIComponent(view.name)}?range=${view.range}`);
    if (powerView !== view) return; // closed or switched meanwhile
    view.data = data;
    view.error = null;
  } catch (err) {
    if (powerView !== view) return;
    view.error = err.message;
  }
  renderSheet();
}

function openPower(name) {
  const plug = config.plugs.find((p) => p.name === name);
  powerView = { name, range: powerRange, data: null, error: null };
  openSheet({ type: "plug", id: name }, () => {
    const view = powerView;
    const d = snap.state.devices[name] || {};
    const data = view.data;
    const stats = data ? [
      ["Jetzt", fmtWatts(d.power)],
      ["Ø", fmtWatts(data.avg_w)],
      ["Spitze", fmtWatts(data.peak_w)],
      ["Verbrauch", `${(data.energy_kwh ?? 0).toLocaleString("de-DE", { maximumFractionDigits: 2 })} kWh`],
    ] : [];
    return [
      el("h2", {}, plug ? plug.display_name : name),
      el("div", { class: "sub" }, "Leistung – Minutenwerte, ein Jahr gespeichert"),
      el("div", { class: "chips" }, ...POWER_RANGES.map(([key, label]) =>
        button(label, () => {
          powerRange = key;
          powerView = { name, range: key, data: null, error: null };
          renderSheet();
          loadPower();
        }, `chip${view.range === key ? " active" : ""}`))),
      view.error ? el("div", { class: "errors" }, view.error)
        : !data ? el("div", { class: "chart-empty" }, "Lade …")
        : data.points.length === 0 ? el("div", { class: "chart-empty" }, "Noch keine Messwerte in diesem Zeitraum.")
        : (view.chart ||= powerChart(data)), // built once per data set: keeps the tapped value across live updates
      el("div", { class: "stats" }, ...stats.map(([k, v]) => el("div", {}, el("small", {}, k), el("b", {}, v)))),
    ];
  });
  loadPower();
}

function niceMax(v) {
  if (v <= 0) return 10;
  const exp = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * exp) return m * exp;
  return 10 * exp;
}

function timeLabel(ts, range) {
  const date = new Date(ts * 1000);
  if (range === "6h" || range === "24h") return date.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  if (range === "7d") return date.toLocaleDateString("de-DE", { weekday: "short" });
  if (range === "1y") return date.toLocaleDateString("de-DE", { month: "short" });
  return date.toLocaleDateString("de-DE", { day: "numeric", month: "numeric" });
}

/** Round tick times in local time: full hours, midnights, month starts. */
function timeTicks(data) {
  const ticks = [];
  const t = new Date(data.start * 1000);
  let next;
  if (data.range === "6h" || data.range === "24h") {
    const step = data.range === "6h" ? 1 : 4;
    t.setMinutes(0, 0, 0);
    do t.setHours(t.getHours() + 1); while (t.getHours() % step);
    next = () => t.setHours(t.getHours() + step);
  } else if (data.range === "1y") {
    t.setHours(0, 0, 0, 0);
    t.setDate(1);
    do t.setMonth(t.getMonth() + 1); while (t.getMonth() % 2);
    next = () => t.setMonth(t.getMonth() + 2);
  } else {
    const step = data.range === "7d" ? 1 : 7;
    t.setHours(24, 0, 0, 0);
    if (step === 7) while (t.getDay() !== 1) t.setDate(t.getDate() + 1); // Mondays
    next = () => t.setDate(t.getDate() + step);
  }
  while (t.getTime() / 1000 < data.end) {
    ticks.push(t.getTime() / 1000);
    next();
  }
  return ticks;
}

function pointLabel(ts, range) {
  const date = new Date(ts * 1000);
  if (range === "1y") return date.toLocaleDateString("de-DE", { weekday: "short", day: "numeric", month: "short" });
  return date.toLocaleString("de-DE", { weekday: "short", day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });
}

function powerChart(data) {
  const W = 600, H = 230, L = 52, R = 10, T = 12, B = 26;
  const span = data.end - data.start;
  const ymax = niceMax(Math.max(...data.points.map((p) => p[2])) * 1.05);
  const x = (ts) => L + ((ts - data.start) / span) * (W - L - R);
  const y = (w) => T + (1 - w / ymax) * (H - T - B);
  const half = data.bucket_s / 2;

  // split into runs where buckets follow each other (a gap = app or Z2M was down)
  const runs = [];
  let run = [];
  for (const p of data.points) {
    if (run.length && p[0] - run[run.length - 1][0] > data.bucket_s * 1.5) { runs.push(run); run = []; }
    run.push(p);
  }
  if (run.length) runs.push(run);

  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": "Leistungsverlauf" });
  for (const frac of [0, 0.5, 1]) {
    const yy = y(ymax * frac);
    chart.append(svg("line", { x1: L, x2: W - R, y1: yy, y2: yy, class: frac === 0 ? "axis" : "grid" }));
    const label = svg("text", { x: L - 6, y: yy + 4, class: "ylabel" });
    label.textContent = fmtWatts(ymax * frac);
    chart.append(label);
  }
  for (const ts of timeTicks(data)) {
    const xx = x(ts);
    chart.append(svg("line", { x1: xx, x2: xx, y1: T, y2: H - B, class: "vgrid" }));
    if (xx < L + 22 || xx > W - R - 22) continue; // label would stick out
    const label = svg("text", { x: xx, y: H - 6, class: "xlabel", "text-anchor": "middle" });
    label.textContent = timeLabel(ts, data.range);
    chart.append(label);
  }
  const showPeak = data.bucket_s > data.sample_s;
  for (const r of runs) {
    const pts = r.map((p) => [x(p[0] + half), y(p[1])]);
    if (pts.length === 1) pts.push([pts[0][0] + 2, pts[0][1]]);
    const line = pts.map(([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`).join(" ");
    const base = y(0).toFixed(1);
    chart.append(svg("polygon", { points: `${pts[0][0].toFixed(1)},${base} ${line} ${pts[pts.length - 1][0].toFixed(1)},${base}`, class: "area" }));
    if (showPeak) {
      chart.append(svg("polyline", { points: r.map((p) => `${x(p[0] + half).toFixed(1)},${y(p[2]).toFixed(1)}`).join(" "), class: "peak" }));
    }
    chart.append(svg("polyline", { points: line, class: "avg" }));
  }

  // touch / hover readout
  const cursor = svg("line", { y1: T, y2: H - B, class: "cursor", visibility: "hidden" });
  chart.append(cursor);
  const readout = el("div", { class: "readout" }, showPeak ? "Fläche = Durchschnitt, Linie oben = Spitze" : "Tippen für Einzelwerte");
  const pick = (event) => {
    const box = chart.getBoundingClientRect();
    const sx = ((event.clientX - box.left) / box.width) * W;
    const ts = data.start + ((sx - L) / (W - L - R)) * span;
    let best = data.points[0];
    for (const p of data.points) if (Math.abs(p[0] + half - ts) < Math.abs(best[0] + half - ts)) best = p;
    cursor.setAttribute("x1", x(best[0] + half));
    cursor.setAttribute("x2", x(best[0] + half));
    cursor.setAttribute("visibility", "visible");
    readout.textContent = `${pointLabel(best[0], data.range)} · ` +
      (showPeak ? `Ø ${fmtWatts(best[1])} · Spitze ${fmtWatts(best[2])}` : fmtWatts(best[1]));
  };
  chart.addEventListener("pointerdown", pick);
  chart.addEventListener("pointermove", (e) => { if (e.buttons || e.pointerType === "mouse") pick(e); });
  return el("div", { class: "chart-wrap" }, chart, readout);
}

// refresh an open history sheet once a minute (new samples arrive every minute)
setInterval(() => { if (powerView && document.visibilityState === "visible") loadPower(); }, 60000);

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
  renderAllButton();
  paintRoomList();
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
  $("#prev-phase").addEventListener("click", () => act("POST", "/api/schedule/back"));
  $("#resume-schedule").addEventListener("click", () => act("POST", "/api/schedule/resume"));
  $("#all-off").addEventListener("click", () => {
    const anyOn = snap && snap.state.rooms.some((r) => r.on);
    act("POST", anyOn ? "/api/all-off" : "/api/all-on");
  });
  window.addEventListener("resize", () => { if (sheetRender) placeSheet(); });
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
