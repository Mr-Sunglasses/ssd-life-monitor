const DEFAULT_REFRESH_MS = 15_000;
const REQUEST_TIMEOUT_MS = 10_000;
const state = {
  timer: null,
  loading: false,
  refreshMs: DEFAULT_REFRESH_MS,
  lastPayload: null,
  histories: new Map(),
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function number(value, digits = 1) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function bytes(value) {
  if (!Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let size = value;
  let unit = 0;
  while (size >= 1000 && unit < units.length - 1) {
    size /= 1000;
    unit += 1;
  }
  return `${number(size, size >= 100 ? 0 : 1)} ${units[unit]}`;
}

function date(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "unknown time" : parsed.toLocaleString();
}

function duration(days) {
  if (!Number.isFinite(days) || days < 0) return "unknown";
  if (days >= 730) return `${number(days / 365, 1)} years`;
  if (days >= 60) return `${number(days / 30.44, 1)} months`;
  return `${number(days, 1)} days`;
}

function lifeStatus(remaining) {
  if (typeof remaining !== "number") return "unknown";
  if (remaining <= 10) return "critical";
  if (remaining <= 20) return "warning";
  return "healthy";
}

function ring(remaining) {
  const available = typeof remaining === "number" && Number.isFinite(remaining);
  const value = available ? Math.max(0, Math.min(100, remaining)) : 0;
  const circumference = 2 * Math.PI * 46;
  const offset = circumference * (1 - value / 100);
  const status = lifeStatus(remaining);
  const text = available ? `${Math.round(value)}%` : "—";
  return `
    <svg class="life-ring" viewBox="0 0 110 110" role="img" aria-label="${escapeHtml(text)} endurance remaining">
      <circle class="ring-track" cx="55" cy="55" r="46"></circle>
      <circle class="ring-value ${status}" cx="55" cy="55" r="46" stroke-dasharray="${circumference}" stroke-dashoffset="${offset}"></circle>
      <text class="ring-number" x="55" y="59">${escapeHtml(text)}</text>
      <text class="ring-label" x="55" y="75">REMAINING</text>
    </svg>`;
}

function projectionText(projection) {
  if (!projection) return "Rated-endurance projection unavailable.";
  if (projection.status === "estimated") {
    const low = projection.days_remaining_low;
    const high = projection.days_remaining_high;
    const confidence = projection.confidence || "low";
    if (Number.isFinite(low) && Number.isFinite(high)) {
      return `Rated-endurance projection: ${duration(low)}–${duration(high)} (${confidence} confidence).`;
    }
    if (Number.isFinite(projection.days_remaining)) {
      return `Rated-endurance projection: about ${duration(projection.days_remaining)} (${confidence} confidence).`;
    }
  }
  const messages = {
    "insufficient-history": "Projection needs at least 14 days of history.",
    "insufficient-wear-change": "Projection needs at least two endurance-counter steps.",
    "no-wear-observed": "No endurance-counter change has been observed yet.",
    "counter-reset": "Projection restarted after an endurance-counter reset.",
    "unstable-identity": "Projection disabled because this drive lacks a stable hardware identity.",
    "unsupported-source": "Projection is unavailable for vendor-specific SATA life counters.",
  };
  return messages[projection.status] || "Rated-endurance projection unavailable.";
}

function warningText(drive) {
  const warnings = [];
  if (drive.smart_status === "unhealthy") warnings.push("SMART reports a problem.");
  if (drive.temperature_status === "critical") warnings.push("Temperature is critical.");
  else if (drive.temperature_status === "warning") {
    warnings.push("Temperature is above the controller warning threshold.");
  }
  if (typeof drive.endurance_remaining_percent === "number" && drive.endurance_remaining_percent <= 20) {
    warnings.push("Rated endurance remaining is low.");
  }
  if (drive.identity_quality === "path-fallback") {
    warnings.push("No serial number or WWN was available; history cannot safely follow device renames.");
  }
  for (const warning of drive.nvme_critical_warnings || []) warnings.push(`NVMe: ${warning}`);
  for (const warning of drive.health_warnings || []) warnings.push(warning);
  for (const error of drive.collector_errors || []) warnings.push(`Collector: ${error}`);
  return [...new Set(warnings)];
}

function chart(points) {
  const samples = points
    .filter(
      (point) =>
        typeof point.observed_at === "number" &&
        Number.isFinite(point.observed_at) &&
        typeof point.used_percent === "number" &&
        Number.isFinite(point.used_percent),
    )
    .sort((left, right) => left.observed_at - right.observed_at);
  if (samples.length < 2) {
    return `<div class="chart-heading"><span>Endurance history</span><span>Collecting samples…</span></div>`;
  }
  const used = samples.map((point) => point.used_percent);
  const min = Math.min(...used);
  const max = Math.max(...used);
  const firstTime = samples[0].observed_at;
  const elapsed = Math.max(1, samples.at(-1).observed_at - firstTime);
  const spread = Math.max(1, max - min);
  const path = samples
    .map((point, index) => {
      const x = ((point.observed_at - firstTime) / elapsed) * 100;
      const y = 58 - ((point.used_percent - min) / spread) * 48;
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  return `
    <div class="chart-heading"><span>Endurance used history</span><span>${number(min, 2)}% → ${number(max, 2)}%</span></div>
    <svg class="chart" viewBox="0 0 100 60" preserveAspectRatio="none" aria-label="Endurance used history over time">
      <line class="chart-guide" x1="0" y1="10" x2="100" y2="10"></line>
      <line class="chart-guide" x1="0" y1="58" x2="100" y2="58"></line>
      <path class="chart-line" d="${path}"></path>
    </svg>`;
}

function nvmeDetails(drive) {
  if (drive.protocol !== "nvme") return "";
  const details = [
    ["Available spare", drive.available_spare_percent == null ? "—" : `${number(drive.available_spare_percent)}%`],
    ["Media errors", number(drive.media_errors, 0)],
    ["Unsafe shutdowns", number(drive.unsafe_shutdowns, 0)],
    ["Power-on hours", number(drive.power_on_hours, 0)],
  ];
  return `<div class="detail-grid">${details
    .map(
      ([label, value]) =>
        `<div class="detail"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`,
    )
    .join("")}</div>`;
}

function driveCard(drive, points = []) {
  const remaining = drive.endurance_remaining_percent;
  const lifeAvailable = typeof remaining === "number" && Number.isFinite(remaining);
  const status = drive.smart_status || "unknown";
  const temperatureStatus = drive.temperature_status || "unknown";
  const warnings = warningText(drive);
  const warningsMarkup = warnings.length
    ? `<div class="warnings">${warnings.map((warning) => `<div>${escapeHtml(warning)}</div>`).join("")}</div>`
    : "";
  const lifeTitle = lifeAvailable ? `${number(remaining, 1)}% remaining` : "Not available";
  const lifeNote = lifeAvailable
    ? drive.endurance_source === "nvme-percentage-used"
      ? "From the standardized NVMe percentage-used health counter."
      : "From an explicitly labelled, vendor-specific SATA SMART attribute."
    : drive.type === "ssd"
      ? "This SSD did not expose a recognized endurance percentage."
      : "Hard drives do not expose SSD endurance data.";
  const transport = String(drive.transport || "unknown").toUpperCase();

  return `
    <article class="drive-card">
      <div class="drive-heading">
        <div>
          <h2 class="drive-model" title="${escapeHtml(drive.model)}">${escapeHtml(drive.model || "Unknown drive")}</h2>
          <p class="drive-meta" title="${escapeHtml(drive.path)}">${escapeHtml(drive.path)} · ${escapeHtml(transport)} · ${bytes(drive.size_bytes)}</p>
        </div>
        <span class="badge ${escapeHtml(status)}">SMART ${escapeHtml(status)}</span>
      </div>

      <div class="life-panel">
        ${ring(remaining)}
        <div class="life-copy">
          <p class="metric-label">Rated endurance remaining</p>
          <p class="life-title">${escapeHtml(lifeTitle)}</p>
          <p class="life-note">${escapeHtml(lifeNote)}</p>
          <p class="projection">${escapeHtml(projectionText(drive.projection))}</p>
        </div>
      </div>

      <div class="metrics">
        <div class="metric"><span class="metric-label">Endurance used</span><strong class="metric-value">${lifeAvailable ? `${number(drive.endurance_used_percent, 1)}%` : "—"}</strong></div>
        <div class="metric"><span class="metric-label">Temperature</span><strong class="metric-value ${escapeHtml(temperatureStatus)}">${drive.temperature_c == null ? "—" : `${number(drive.temperature_c, 1)}°C`}</strong></div>
        <div class="metric"><span class="metric-label">Serial</span><strong class="metric-value" title="${escapeHtml(drive.serial)}">${escapeHtml(drive.serial || "Unknown")}</strong></div>
      </div>

      ${nvmeDetails(drive)}
      <div class="chart-wrap">${chart(points)}</div>
      ${warningsMarkup}
    </article>`;
}

async function fetchJson(url) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(url, { cache: "no-store", signal: controller.signal });
    let payload;
    try {
      payload = await response.json();
    } catch {
      throw new Error("The service returned invalid JSON.");
    }
    if (!response.ok) throw new Error(payload.detail || "The service could not complete the request.");
    return payload;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("The service did not respond within 10 seconds.");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function history(driveId) {
  try {
    const payload = await fetchJson(
      `/api/drives/${encodeURIComponent(driveId)}/history?hours=2160&max_points=1200`,
    );
    const points = Array.isArray(payload.points) ? payload.points : [];
    state.histories.set(driveId, points);
    return points;
  } catch {
    return state.histories.get(driveId) || [];
  }
}

function render(payload, requestError = null) {
  const drives = Array.isArray(payload.drives) ? payload.drives : [];
  const driveContainer = $("#drives");
  if (!drives.length) {
    driveContainer.innerHTML = `<div class="empty-card">No supported disks were found. Check the collector permissions and host inventory.</div>`;
  } else {
    driveContainer.innerHTML = drives
      .map((drive) => driveCard(drive, state.histories.get(drive.id) || []))
      .join("");
  }

  const status = $("#status");
  if (requestError) {
    status.className = "status error";
    status.textContent = `Live refresh failed; keeping the previous reading. ${requestError}`;
  } else if (payload.stale) {
    status.className = "status warning";
    const reason = payload.collector_error ? ` Collector: ${payload.collector_error}` : "";
    status.textContent = `Showing the last successful reading from ${date(payload.last_success_at || payload.generated_at)}.${reason}`;
  } else {
    status.className = "status";
    const deferred = payload.force_deferred ? " · manual refresh rate-limited" : "";
    status.textContent = `${drives.length} supported disk${drives.length === 1 ? "" : "s"} · collecting every ${number(payload.collection_interval_seconds, 0)}s${deferred}`;
  }
  $("#updated").textContent = `Last successful reading: ${date(payload.last_success_at || payload.generated_at)}`;
}

async function load(force = false) {
  if (state.loading) return;
  state.loading = true;
  const button = $("#refresh");
  button.disabled = true;
  try {
    const payload = await fetchJson(`/api/drives${force ? "?force=true" : ""}`);
    const drives = Array.isArray(payload.drives) ? payload.drives : [];
    await Promise.all(drives.map((drive) => history(drive.id)));
    state.lastPayload = payload;
    const pollSeconds = Number(payload.poll_seconds);
    if (Number.isFinite(pollSeconds)) {
      state.refreshMs = Math.min(300_000, Math.max(5_000, pollSeconds * 1000));
    }
    render(payload);
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unable to read drive data.";
    if (state.lastPayload) {
      render(state.lastPayload, message);
    } else {
      const status = $("#status");
      status.className = "status error";
      status.textContent = message;
      $("#drives").innerHTML = `<div class="empty-card">Drive readings are unavailable. Check the collector service, device permissions, and host utilities.</div>`;
      $("#updated").textContent = "No successful reading is available yet.";
    }
  } finally {
    state.loading = false;
    button.disabled = false;
  }
}

async function poll() {
  await load(false);
  state.timer = window.setTimeout(poll, state.refreshMs);
}

$("#refresh").addEventListener("click", () => load(true));
poll();
