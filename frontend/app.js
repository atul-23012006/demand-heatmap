const API = "/api";

// ---- sequential color scale (dark -> blue -> amber -> red) ----
const COLOR_STOPS = [
  { t: 0.00, c: [15, 20, 40] },
  { t: 0.18, c: [30, 64, 175] },
  { t: 0.45, c: [59, 130, 246] },
  { t: 0.72, c: [245, 197, 24] },
  { t: 1.00, c: [220, 38, 38] },
];

function lerp(a, b, f) { return a + (b - a) * f; }

function colorAt(t) {
  t = Math.max(0, Math.min(1, t));
  for (let i = 0; i < COLOR_STOPS.length - 1; i++) {
    const s0 = COLOR_STOPS[i], s1 = COLOR_STOPS[i + 1];
    if (t >= s0.t && t <= s1.t) {
      const f = (t - s0.t) / (s1.t - s0.t);
      const c = s0.c.map((v, idx) => Math.round(lerp(v, s1.c[idx], f)));
      return `rgb(${c[0]},${c[1]},${c[2]})`;
    }
  }
  return `rgb(${COLOR_STOPS.at(-1).c.join(",")})`;
}

// sqrt scaling: demand is heavily right-skewed (a handful of Manhattan zones
// dominate), sqrt spreads out the low/mid range so the map isn't just a few
// hot zones on a sea of near-black.
let currentMaxDemand = 50;
function colorForDemand(v) {
  const t = Math.sqrt(Math.max(0, v)) / Math.sqrt(currentMaxDemand);
  return colorAt(t);
}

// ---- state ----
let map, geoLayer, zoneLayers = {};
let zoneMeta = {};        // zone_id -> {zone_name, borough}
let zoneCentroids = {};   // zone_id -> {lat, lon}
let demandByZone = {};    // zone_id -> predicted value (current hour)
let selectedZoneId = null;
let recoLines = [];

function currentDatetime() {
  const date = document.getElementById("date-input").value;
  const hour = document.getElementById("hour-input").value.padStart(2, "0");
  return `${date}T${hour}:00:00`;
}

async function initMap() {
  map = L.map("map", { zoomControl: true }).setView([40.735, -73.95], 11);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
    className: "map-tiles-dark",
  }).addTo(map);

  const [zonesGeojson, centroids] = await Promise.all([
    fetch(`${API}/zones`).then((r) => r.json()),
    fetch(`${API}/zone_centroids`).then((r) => r.json()),
  ]);

  centroids.forEach((c) => (zoneCentroids[c.zone_id] = c));
  zonesGeojson.features.forEach((f) => (zoneMeta[f.properties.zone_id] = f.properties));

  geoLayer = L.geoJSON(zonesGeojson, {
    style: () => ({ color: "#1a2138", weight: 0.6, fillColor: "#0f1428", fillOpacity: 0.7 }),
    onEachFeature: (feature, layer) => {
      const zid = feature.properties.zone_id;
      zoneLayers[zid] = layer;
      layer.on("click", () => selectZone(zid));
      layer.on("mouseover", () => layer.setStyle({ weight: 2, color: "#fff" }));
      layer.on("mouseout", () => restyleZone(zid));
      layer.bindTooltip(
        `<strong>${feature.properties.zone_name}</strong><br>${feature.properties.borough}`,
        { sticky: true }
      );
    },
  }).addTo(map);
}

function restyleZone(zid) {
  const isSelected = zid === selectedZoneId;
  zoneLayers[zid].setStyle({
    weight: isSelected ? 3 : 0.6,
    color: isSelected ? "#f5c518" : "#1a2138",
    fillColor: colorForDemand(demandByZone[zid] ?? 0),
    fillOpacity: 0.78,
  });
}

async function refreshHeatmap() {
  const dt = currentDatetime();
  document.getElementById("hour-label").textContent =
    `Hour: ${document.getElementById("hour-input").value.padStart(2, "0")}:00`;

  const resp = await fetch(`${API}/heatmap?datetime=${encodeURIComponent(dt)}`);
  if (!resp.ok) return;
  const data = await resp.json();

  demandByZone = {};
  let maxV = 1;
  data.zones.forEach((z) => {
    demandByZone[z.zone_id] = z.predicted;
    if (z.predicted > maxV) maxV = z.predicted;
  });
  currentMaxDemand = maxV;

  Object.keys(zoneLayers).forEach((zid) => restyleZone(Number(zid)));
  renderLegend();

  if (selectedZoneId) await loadRecommendations(selectedZoneId);
}

function renderLegend() {
  const stopsCss = COLOR_STOPS.map((s) => `${colorAt(s.t)} ${s.t * 100}%`).join(", ");
  document.getElementById("legend-scale").style.background = `linear-gradient(90deg, ${stopsCss})`;
  const el = document.getElementById("legend-scale");
  el.parentElement.querySelectorAll(".legend-ticks").forEach((n) => n.remove());
  const ticks = document.createElement("div");
  ticks.className = "legend-ticks";
  [0, 0.25, 0.5, 0.75, 1].forEach((t) => {
    const v = Math.round((t * t) * currentMaxDemand); // invert sqrt scale
    const span = document.createElement("span");
    span.textContent = v;
    ticks.appendChild(span);
  });
  el.parentElement.appendChild(ticks);
}

function clearRecoLines() {
  recoLines.forEach((l) => map.removeLayer(l));
  recoLines = [];
}

async function selectZone(zid) {
  const prev = selectedZoneId;
  selectedZoneId = zid;
  if (prev) restyleZone(prev);
  restyleZone(zid);
  await loadRecommendations(zid);
}

async function loadRecommendations(zid) {
  const dt = currentDatetime();
  const resp = await fetch(`${API}/recommend?zone_id=${zid}&datetime=${encodeURIComponent(dt)}&top_n=5`);
  const panel = document.getElementById("selection-panel");
  if (!resp.ok) {
    panel.innerHTML = `<p class="hint">No data for this hour.</p>`;
    return;
  }
  const data = await resp.json();
  const meta = zoneMeta[zid] || {};
  const currentDemand = demandByZone[zid] ?? 0;

  let html = `
    <div class="zone-card">
      <h4>${meta.zone_name ?? "Zone " + zid}</h4>
      <div class="zone-sub">${meta.borough ?? ""} &middot; zone ${zid}</div>
      <div class="zone-stat">${currentDemand.toFixed(1)}</div>
      <div class="zone-stat-label">predicted trips this hour</div>
    </div>
    <h3 style="font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.04em;">
      Best zones to reposition to
    </h3>
    <ul class="reco-list">`;
  data.recommendations.forEach((r, i) => {
    html += `
      <li>
        <span class="reco-rank">${i + 1}</span>
        <span class="reco-name">${r.zone_name}<span class="reco-borough">${r.borough}</span></span>
        <span class="reco-metric">${r.predicted_demand.toFixed(1)} trips/hr<br>${r.travel_min.toFixed(0)} min away</span>
      </li>`;
  });
  html += `</ul>`;
  panel.innerHTML = html;

  clearRecoLines();
  const origin = zoneCentroids[zid];
  if (origin) {
    data.recommendations.forEach((r, i) => {
      const dest = zoneCentroids[r.zone_id];
      if (!dest) return;
      const line = L.polyline(
        [[origin.lat, origin.lon], [dest.lat, dest.lon]],
        { color: "#f5c518", weight: 2, opacity: 0.35 + (0.5 * (5 - i)) / 5, dashArray: "4 5" }
      ).addTo(map);
      recoLines.push(line);
    });
  }
}

// ---- metrics tab ----
async function loadMetrics() {
  const resp = await fetch(`${API}/metrics`);
  const data = await resp.json();
  const m = data.overall;

  const tiles = document.getElementById("stat-tiles");
  tiles.innerHTML = `
    <div class="stat-tile">
      <div class="stat-label">Model MAE</div>
      <div class="stat-value good">${m.model_mae.toFixed(2)}</div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">Baseline MAE (historical mean)</div>
      <div class="stat-value">${m.baseline_mae.toFixed(2)}</div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">MAE improvement vs. baseline</div>
      <div class="stat-value good">${m.mae_improvement_pct.toFixed(1)}%</div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">Model WAPE</div>
      <div class="stat-value good">${(m.model_wape * 100).toFixed(1)}%</div>
    </div>
    <div class="stat-tile">
      <div class="stat-label">Test set size</div>
      <div class="stat-value">${m.n_test.toLocaleString()}</div>
    </div>
  `;

  const rows = data.per_zone
    .sort((a, b) => b.actual_mean - a.actual_mean)
    .slice(0, 20);
  const tbody = document.querySelector("#zone-table tbody");
  tbody.innerHTML = rows
    .map(
      (r) => `<tr>
        <td>${r.zone_name}</td>
        <td>${r.borough}</td>
        <td>${r.actual_mean.toFixed(1)}</td>
        <td>${r.model_mae.toFixed(2)}</td>
        <td>${r.baseline_mae.toFixed(2)}</td>
      </tr>`
    )
    .join("");
}

// ---- tabs ----
function setupTabs() {
  const tabMap = document.getElementById("tab-map");
  const tabMetrics = document.getElementById("tab-metrics");
  const viewMap = document.getElementById("view-map");
  const viewMetrics = document.getElementById("view-metrics");

  tabMap.addEventListener("click", () => {
    tabMap.classList.add("active");
    tabMetrics.classList.remove("active");
    viewMap.classList.add("active");
    viewMetrics.classList.remove("active");
    setTimeout(() => map.invalidateSize(), 50);
  });
  tabMetrics.addEventListener("click", () => {
    tabMetrics.classList.add("active");
    tabMap.classList.remove("active");
    viewMetrics.classList.add("active");
    viewMap.classList.remove("active");
    loadMetrics();
  });
}

async function main() {
  setupTabs();
  await initMap();
  await refreshHeatmap();

  document.getElementById("date-input").addEventListener("change", refreshHeatmap);
  document.getElementById("hour-input").addEventListener("input", refreshHeatmap);
}

main();
