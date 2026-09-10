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

let uiMode = "single";    // "single" | "fleet"
let fleetDrivers = [];    // [zone_id, ...] in click order
let fleetMarkers = {};    // zone_id -> L.marker
let fleetLines = [];
const DRIVER_COLORS = ["#f5c518", "#3b82f6", "#22c55e", "#ef4444", "#a855f7", "#06b6d4", "#f97316", "#ec4899"];
const MAX_FLEET_DRIVERS = 8;

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
      layer.on("click", () => {
        if (uiMode === "fleet") toggleFleetDriver(zid);
        else selectZone(zid);
      });
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

// ---- fleet (multi-driver load balancing) mode ----
function setMode(mode) {
  uiMode = mode;
  document.getElementById("mode-single").classList.toggle("active", mode === "single");
  document.getElementById("mode-fleet").classList.toggle("active", mode === "fleet");
  document.getElementById("selection-panel").style.display = mode === "single" ? "block" : "none";
  document.getElementById("fleet-panel").style.display = mode === "fleet" ? "block" : "none";
}

function toggleFleetDriver(zid) {
  const idx = fleetDrivers.indexOf(zid);
  if (idx !== -1) {
    fleetDrivers.splice(idx, 1);
    if (fleetMarkers[zid]) {
      map.removeLayer(fleetMarkers[zid]);
      delete fleetMarkers[zid];
    }
  } else {
    if (fleetDrivers.length >= MAX_FLEET_DRIVERS) return;
    fleetDrivers.push(zid);
  }
  renderFleetMarkers();
  renderFleetList();
  clearFleetLines();
  document.getElementById("fleet-results").innerHTML = "";
}

function renderFleetMarkers() {
  fleetDrivers.forEach((zid, i) => {
    const c = zoneCentroids[zid];
    if (!c) return;
    if (!fleetMarkers[zid]) {
      fleetMarkers[zid] = L.circleMarker([c.lat, c.lon], {
        radius: 9, weight: 2, color: "#fff", fillColor: DRIVER_COLORS[i % DRIVER_COLORS.length], fillOpacity: 1,
      }).addTo(map);
    } else {
      fleetMarkers[zid].setStyle({ fillColor: DRIVER_COLORS[i % DRIVER_COLORS.length] });
    }
    fleetMarkers[zid].bindTooltip(`Driver ${i + 1}`, { permanent: false });
  });
}

function renderFleetList() {
  const list = document.getElementById("fleet-driver-list");
  list.innerHTML = fleetDrivers
    .map((zid, i) => {
      const meta = zoneMeta[zid] || {};
      const color = DRIVER_COLORS[i % DRIVER_COLORS.length];
      return `<li>
        <span class="driver-chip" style="background:${color}">${i + 1}</span>
        <span class="reco-name">${meta.zone_name ?? "Zone " + zid}<span class="reco-borough">${meta.borough ?? ""}</span></span>
        <button class="driver-remove" data-zid="${zid}" title="Remove">&times;</button>
      </li>`;
    })
    .join("");
  list.querySelectorAll(".driver-remove").forEach((btn) =>
    btn.addEventListener("click", () => toggleFleetDriver(Number(btn.dataset.zid)))
  );
  document.getElementById("fleet-balance-btn").disabled = fleetDrivers.length === 0;
}

function clearFleetLines() {
  fleetLines.forEach((l) => map.removeLayer(l));
  fleetLines = [];
}

function clearFleet() {
  fleetDrivers.forEach((zid) => fleetMarkers[zid] && map.removeLayer(fleetMarkers[zid]));
  fleetDrivers = [];
  fleetMarkers = {};
  clearFleetLines();
  renderFleetList();
  document.getElementById("fleet-results").innerHTML = "";
}

async function balanceFleet() {
  if (fleetDrivers.length === 0) return;
  const dt = currentDatetime();
  const drivers = fleetDrivers.map((zid, i) => ({ driver_id: `driver_${i + 1}`, zone_id: zid }));
  const resp = await fetch(`${API}/recommend_batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ datetime: dt, drivers }),
  });
  const results = document.getElementById("fleet-results");
  if (!resp.ok) {
    results.innerHTML = `<p class="hint">No data for this hour.</p>`;
    return;
  }
  const data = await resp.json();
  clearFleetLines();

  let html = `<h3 style="font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.04em;margin-top:16px;">Assignments</h3><ul class="reco-list">`;
  data.assignments.forEach((a, i) => {
    const originZid = fleetDrivers[i];
    const origin = zoneCentroids[originZid];
    const dest = zoneCentroids[a.zone_id];
    const color = DRIVER_COLORS[i % DRIVER_COLORS.length];
    if (origin && dest) {
      const line = L.polyline([[origin.lat, origin.lon], [dest.lat, dest.lon]], {
        color, weight: 3, opacity: 0.7, dashArray: originZid === a.zone_id ? null : "4 5",
      }).addTo(map);
      fleetLines.push(line);
    }
    html += `
      <li>
        <span class="driver-chip" style="background:${color}">${i + 1}</span>
        <span class="reco-name">${a.zone_name}<span class="reco-borough">${a.predicted_demand.toFixed(0)} trips/hr &middot; ${a.travel_min.toFixed(0)} min${a.over_capacity ? " &middot; over capacity" : ""}</span></span>
      </li>`;
  });
  html += `</ul>`;
  results.innerHTML = html;
}

function setupFleetMode() {
  document.getElementById("mode-single").addEventListener("click", () => setMode("single"));
  document.getElementById("mode-fleet").addEventListener("click", () => setMode("fleet"));
  document.getElementById("fleet-balance-btn").addEventListener("click", balanceFleet);
  document.getElementById("fleet-clear-btn").addEventListener("click", clearFleet);
}

// ---- earnings simulator tab ----
function populateSimZoneSelect() {
  const select = document.getElementById("sim-zone-select");
  const zones = Object.entries(zoneMeta).sort((a, b) => a[1].zone_name.localeCompare(b[1].zone_name));
  select.innerHTML = zones
    .map(([zid, meta]) => `<option value="${zid}">${meta.zone_name} (${meta.borough})</option>`)
    .join("");
  select.value = "161"; // Midtown Center, a reliably high-demand default
}

function renderSimResults(data) {
  const results = document.getElementById("sim-results");
  const lift = data.earnings_lift_pct;
  const liftHtml = lift == null ? "n/a" : `${lift > 0 ? "+" : ""}${lift.toFixed(1)}%`;

  const allHours = data.stay_put.hours.map((h, i) => ({
    stay: h.earnings, follow: data.follow_model.hours[i].earnings, label: h.hour.slice(11, 13),
  }));
  const maxEarn = Math.max(1, ...allHours.flatMap((h) => [h.stay, h.follow]));

  const bars = allHours
    .map(
      (h) => `<div class="sim-hour-col">
        <div class="sim-bar stay" style="height:${(h.stay / maxEarn) * 100}%" title="Stay put: $${h.stay.toFixed(2)}"></div>
        <div class="sim-bar follow" style="height:${(h.follow / maxEarn) * 100}%" title="Follow model: $${h.follow.toFixed(2)}"></div>
      </div>`
    )
    .join("");
  const labels = allHours.map((h) => `<span>${h.label}</span>`).join("");

  results.innerHTML = `
    <div class="stat-tiles">
      <div class="stat-tile">
        <div class="stat-label">Stay put &mdash; total</div>
        <div class="stat-value">$${data.stay_put.total_earnings.toFixed(2)}</div>
      </div>
      <div class="stat-tile">
        <div class="stat-label">Follow model &mdash; total</div>
        <div class="stat-value good">$${data.follow_model.total_earnings.toFixed(2)}</div>
      </div>
      <div class="stat-tile">
        <div class="stat-label">Earnings lift</div>
        <div class="stat-value good">${liftHtml}</div>
      </div>
    </div>
    <div class="sim-legend">
      <span class="legend-stay">Stay put</span>
      <span class="legend-follow">Follow model</span>
    </div>
    <div class="sim-chart">${bars}</div>
    <div class="sim-hour-labels">${labels}</div>
    <p class="hint" style="margin-top:16px;">${data.assumptions.note} Saturation constant:
      ${data.assumptions.trips_per_driver_saturation} predicted trips/hr = 1 fully-utilized driver.</p>
  `;
}

async function runSimulation() {
  const startZone = document.getElementById("sim-zone-select").value;
  const date = document.getElementById("sim-date-input").value;
  const results = document.getElementById("sim-results");
  results.innerHTML = `<p class="hint">Running simulation&hellip;</p>`;
  const resp = await fetch(`${API}/simulate?start_zone=${startZone}&date=${date}`);
  if (!resp.ok) {
    results.innerHTML = `<p class="hint">No data for that date.</p>`;
    return;
  }
  renderSimResults(await resp.json());
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
  const tabs = {
    map: { btn: document.getElementById("tab-map"), view: document.getElementById("view-map") },
    earnings: { btn: document.getElementById("tab-earnings"), view: document.getElementById("view-earnings") },
    metrics: { btn: document.getElementById("tab-metrics"), view: document.getElementById("view-metrics") },
  };

  function activate(name) {
    Object.entries(tabs).forEach(([key, t]) => {
      t.btn.classList.toggle("active", key === name);
      t.view.classList.toggle("active", key === name);
    });
    if (name === "map") setTimeout(() => map.invalidateSize(), 50);
    if (name === "metrics") loadMetrics();
  }

  tabs.map.btn.addEventListener("click", () => activate("map"));
  tabs.earnings.btn.addEventListener("click", () => activate("earnings"));
  tabs.metrics.btn.addEventListener("click", () => activate("metrics"));
}

async function main() {
  setupTabs();
  setupFleetMode();
  await initMap();
  await refreshHeatmap();
  populateSimZoneSelect();

  document.getElementById("date-input").addEventListener("change", refreshHeatmap);
  document.getElementById("hour-input").addEventListener("input", refreshHeatmap);
  document.getElementById("sim-run-btn").addEventListener("click", runSimulation);
}

main();
