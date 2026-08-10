// Frontend — entirely snapshot-driven. No series_id is ever hard-coded (spec §8.1);
// every card and chart is generated from data/snapshot.json's self-describing
// metadata. Adding a collector makes a new card appear with zero frontend edits.

const SVGNS = "http://www.w3.org/2000/svg";
const STATUS_LABEL = {
  confirmed: "confirmed", provisional: "provisional",
  under_review: "under review", estimated: "estimated",
};

// Runtime fetch with cache-bust (spec §8.3). Data is never inlined at build time.
async function loadSnapshot() {
  const res = await fetch(`data/snapshot.json?v=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`snapshot fetch failed: ${res.status}`);
  return res.json();
}

function fmt(value, precision) {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: precision, maximumFractionDigits: precision,
  });
}

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

// delta direction coloured by the series' own notion of "good" (meta.direction_good).
function deltaInfo(latest, previous, dirGood) {
  if (!latest || !previous || latest.value === null || previous.value === null) return null;
  const d = latest.value - previous.value;
  if (d === 0) return { cls: "flat", text: "→ 0" };
  const rising = d > 0;
  const arrow = rising ? "▲" : "▼";
  let cls = "flat";
  if (dirGood === "up") cls = rising ? "up" : "down";
  else if (dirGood === "down") cls = rising ? "down" : "up";
  return { cls, text: `${arrow} ${Math.abs(d).toLocaleString(undefined, { maximumFractionDigits: 2 })}` };
}

// --- SVG line chart (shared by sparkline + detail) ------------------------
function buildPath(points, w, h, pad) {
  const vals = points.map(p => p.value).filter(v => v !== null);
  if (vals.length < 2) return null;
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1;
  const n = points.length;
  const x = i => pad + (i / (n - 1)) * (w - 2 * pad);
  const y = v => h - pad - ((v - min) / span) * (h - 2 * pad);
  let d = "", started = false;
  points.forEach((p, i) => {
    if (p.value === null) { started = false; return; }
    d += `${started ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)} `;
    started = true;
  });
  return { d, x, y, min, max };
}

function sparkline(points) {
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", "0 0 300 44");
  svg.setAttribute("preserveAspectRatio", "none");
  const built = buildPath(points.slice(-60), 300, 44, 3);
  if (built) {
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("class", "line");
    path.setAttribute("d", built.d);
    svg.appendChild(path);
  } else if (points.some((p) => p.value !== null)) {
    // Only one data point yet — draw a single dot so the card doesn't look empty.
    const dot = document.createElementNS(SVGNS, "circle");
    dot.setAttribute("cx", "150"); dot.setAttribute("cy", "22"); dot.setAttribute("r", "3");
    dot.setAttribute("class", "dot");
    svg.appendChild(dot);
  }
  return svg;
}

// number of points that carry an actual value
function valueCount(points) {
  return points.reduce((n, p) => n + (p.value !== null ? 1 : 0), 0);
}

// --- cards ----------------------------------------------------------------
function renderCard(s) {
  const { meta, latest, previous, health } = s;
  const card = document.createElement("div");
  card.className = "card" + (health.stale ? " stale" : "");
  card.tabIndex = 0;

  const status = latest ? latest.status : null;
  const badge = status
    ? `<span class="badge ${status}">${STATUS_LABEL[status] || status}</span>` : "";
  card.innerHTML = `
    <div class="name"><span>${meta.display_name}</span>${badge}</div>
    <div><span class="value">${latest ? fmt(latest.value, meta.precision) : "—"}</span>
      <span class="unit">${meta.unit}</span></div>
    <div class="row2"></div>
    <div class="updated">最后数据：${latest ? fmtDate(latest.observed_at) : "—"}</div>
  `;
  const row2 = card.querySelector(".row2");
  const di = deltaInfo(latest, previous, meta.direction_good);
  if (di) {
    const span = document.createElement("span");
    span.className = "delta " + di.cls;
    span.textContent = di.text;
    row2.appendChild(span);
  }
  card.appendChild(sparkline(s.points));
  if (health.stale) {
    const note = document.createElement("div");
    note.className = "stale-note";
    note.textContent = "⚠ 采集中断 · 数据长时间未更新";
    card.appendChild(note);
  } else if (latest && valueCount(s.points) < 2) {
    // Fresh series with a single reading — make clear it's accumulating, not broken.
    const note = document.createElement("div");
    note.className = "accum-note";
    note.textContent = "📈 首个数据点 · 趋势将逐日累积";
    card.appendChild(note);
  }
  card.addEventListener("click", () => openDetail(s));
  card.addEventListener("keydown", e => { if (e.key === "Enter") openDetail(s); });
  return card;
}

// --- detail overlay -------------------------------------------------------
const RANGES = [
  { label: "30", n: 30 }, { label: "90", n: 90 },
  { label: "180", n: 180 }, { label: "全部", n: Infinity },
];
let detailState = null;

function openDetail(s) {
  detailState = { series: s, range: 90 };
  document.getElementById("p-name").textContent = s.meta.display_name;
  document.getElementById("p-desc").textContent = s.meta.description || "";
  const ranges = document.getElementById("ranges");
  ranges.innerHTML = "";
  RANGES.forEach(r => {
    const b = document.createElement("button");
    b.textContent = r.label;
    if (r.n === detailState.range) b.classList.add("active");
    b.addEventListener("click", () => {
      detailState.range = r.n;
      [...ranges.children].forEach(c => c.classList.remove("active"));
      b.classList.add("active");
      drawBig();
    });
    ranges.appendChild(b);
  });
  document.getElementById("overlay").classList.add("open");
  drawBig();
}

function drawBig() {
  const { series, range } = detailState;
  const pts = range === Infinity ? series.points : series.points.slice(-range);
  const svg = document.getElementById("bigchart");
  svg.innerHTML = "";
  const W = 720, H = 320, pad = 40;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const built = buildPath(pts, W, H, pad);
  if (!built) {
    // Single point (or none): show the value + an accumulating note rather than "no data".
    const one = pts.filter((p) => p.value !== null).pop();
    if (one) {
      const c = document.createElementNS(SVGNS, "circle");
      c.setAttribute("cx", W / 2); c.setAttribute("cy", H / 2 - 6); c.setAttribute("r", "5");
      c.setAttribute("class", "dot"); svg.appendChild(c);
      const t1 = addText(svg, W / 2, H / 2 + 22,
        `${one.observed_at.slice(0, 10)} · ${fmt(one.value, series.meta.precision)} ${series.meta.unit}`, "tt");
      t1.setAttribute("text-anchor", "middle");
      const t2 = addText(svg, W / 2, H / 2 + 44, "仅 1 个数据点 · 历史逐日累积中", "axislbl");
      t2.setAttribute("text-anchor", "middle");
    } else {
      const t = addText(svg, W / 2, H / 2, "暂无数据", "tt"); t.setAttribute("text-anchor", "middle");
    }
    return;
  }

  // axes
  addLine(svg, pad, H - pad, W - pad, H - pad, "axis");
  addLine(svg, pad, pad, pad, H - pad, "axis");
  // y labels (min / max)
  addText(svg, 4, built.y(built.max) + 4, fmt(built.max, series.meta.precision), "axislbl");
  addText(svg, 4, built.y(built.min) + 4, fmt(built.min, series.meta.precision), "axislbl");
  // x labels (first / last observed_at)
  addText(svg, pad, H - pad + 16, pts[0].observed_at.slice(0, 10), "axislbl");
  const lastLbl = pts[pts.length - 1].observed_at.slice(0, 10);
  const t = addText(svg, W - pad, H - pad + 16, lastLbl, "axislbl");
  t.setAttribute("text-anchor", "end");

  const path = document.createElementNS(SVGNS, "path");
  path.setAttribute("class", "line");
  path.setAttribute("d", built.d);
  svg.appendChild(path);

  // hover crosshair + tooltip
  const focus = document.createElementNS(SVGNS, "circle");
  focus.setAttribute("r", "3.5"); focus.setAttribute("class", "dot"); focus.style.display = "none";
  const label = addText(svg, 0, 0, "", "tt"); label.style.display = "none";
  svg.appendChild(focus);
  const overlay = document.createElementNS(SVGNS, "rect");
  overlay.setAttribute("x", pad); overlay.setAttribute("y", pad);
  overlay.setAttribute("width", W - 2 * pad); overlay.setAttribute("height", H - 2 * pad);
  overlay.setAttribute("fill", "transparent");
  overlay.addEventListener("mousemove", ev => {
    const rect = svg.getBoundingClientRect();
    const px = (ev.clientX - rect.left) / rect.width * W;
    const i = Math.round(((px - pad) / (W - 2 * pad)) * (pts.length - 1));
    const p = pts[Math.max(0, Math.min(pts.length - 1, i))];
    if (!p || p.value === null) return;
    focus.style.display = ""; label.style.display = "";
    focus.setAttribute("cx", built.x(i)); focus.setAttribute("cy", built.y(p.value));
    label.setAttribute("x", Math.min(built.x(i) + 6, W - 120));
    label.setAttribute("y", Math.max(built.y(p.value) - 8, pad + 12));
    label.textContent = `${p.observed_at.slice(0, 10)} · ${fmt(p.value, series.meta.precision)} ${series.meta.unit}`;
  });
  overlay.addEventListener("mouseleave", () => { focus.style.display = "none"; label.style.display = "none"; });
  svg.appendChild(overlay);
}

function addLine(svg, x1, y1, x2, y2, cls) {
  const l = document.createElementNS(SVGNS, "line");
  l.setAttribute("x1", x1); l.setAttribute("y1", y1);
  l.setAttribute("x2", x2); l.setAttribute("y2", y2);
  l.setAttribute("class", cls); svg.appendChild(l); return l;
}
function addText(svg, x, y, text, cls) {
  const t = document.createElementNS(SVGNS, "text");
  t.setAttribute("x", x); t.setAttribute("y", y); t.setAttribute("class", cls);
  t.textContent = text; svg.appendChild(t); return t;
}

function closeDetail() { document.getElementById("overlay").classList.remove("open"); }

// --- boot -----------------------------------------------------------------
function renderCollectors(snap) {
  const box = document.getElementById("collectors");
  box.innerHTML = "";
  (snap.collectors || []).forEach(c => {
    const chip = document.createElement("span");
    chip.className = "chip" + (c.ok ? "" : " bad");
    chip.textContent = `${c.id} ${c.ok ? "✓" : "✕"}`;
    if (c.error) chip.title = c.error;
    box.appendChild(chip);
  });
}

// collapse state persisted per category id
function isCollapsed(id) {
  try { return localStorage.getItem("cat.collapsed." + id) === "1"; } catch { return false; }
}
function setCollapsed(id, v) {
  try { localStorage.setItem("cat.collapsed." + id, v ? "1" : "0"); } catch {}
}

function renderCategory(cat, items) {
  const section = document.createElement("section");
  section.className = "cat" + (isCollapsed(cat.id) ? " collapsed" : "");

  const header = document.createElement("button");
  header.className = "cat-header";
  header.type = "button";
  header.setAttribute("aria-expanded", String(!isCollapsed(cat.id)));
  header.innerHTML =
    `<span class="chev">▸</span><span class="cat-title">${cat.title}</span>` +
    `<span class="cat-count">${items.length}</span>`;

  const body = document.createElement("div");
  body.className = "cat-body";
  // within a category, sink stale/unhealthy cards to the bottom (stable sort)
  items.slice()
    .sort((a, b) => Number(a.health.stale) - Number(b.health.stale))
    .forEach(s => body.appendChild(renderCard(s)));

  header.addEventListener("click", () => {
    const collapsed = section.classList.toggle("collapsed");
    header.setAttribute("aria-expanded", String(!collapsed));
    setCollapsed(cat.id, collapsed);
  });
  section.appendChild(header);
  section.appendChild(body);
  return section;
}

async function render() {
  try {
    const snap = await loadSnapshot();
    document.getElementById("meta").textContent = "更新于 " + fmtDate(snap.generated_at);
    renderCollectors(snap);
    const cards = document.getElementById("cards");
    cards.innerHTML = "";

    // group series by category, preserving snapshot order within each group
    const byCat = new Map();
    snap.series.forEach(s => {
      const key = s.category || "other";
      if (!byCat.has(key)) byCat.set(key, []);
      byCat.get(key).push(s);
    });
    // category order from snap.categories; append any present-but-unlisted at the end
    const cats = (snap.categories && snap.categories.length)
      ? snap.categories.slice() : [];
    byCat.forEach((_, id) => {
      if (!cats.find(c => c.id === id)) cats.push({ id, title: id === "other" ? "其他" : id });
    });

    cats.forEach(cat => {
      const items = byCat.get(cat.id);
      if (items && items.length) cards.appendChild(renderCategory(cat, items));
    });
    document.getElementById("err").hidden = true;
  } catch (e) {
    const err = document.getElementById("err");
    err.hidden = false;
    err.textContent = "加载快照失败：" + e.message;
  }
}

document.getElementById("close").addEventListener("click", closeDetail);
document.getElementById("overlay").addEventListener("click", e => {
  if (e.target.id === "overlay") closeDetail();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDetail(); });

render();

// PWA: register service worker (cache-first, spec §8.2). When the SW finishes
// refreshing the snapshot in the background, it messages us to re-render — the
// "paint instantly, then silently update" half of the requirement.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data && e.data.type === "snapshot-updated") render();
  });
}
