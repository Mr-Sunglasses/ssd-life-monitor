const REFRESH_MS = 15_000;
const state = { timer: null, loading: false };

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
  if (!projection || projection.status === "insufficient-history") {
    return "Time projection needs more history.";
  }
  if (projection.status === "no-wear-observed") {
    return "No endurance change observed yet.";
  }
  if (projection.status === "estimated" && typeof projection.days_remaining === "number") {
    const days = projection.days_remaining;
    if (days >= 730) return `Rough projection: ${number(days / 365, 1)} years at this wear rate.`;
    if (days >= 60) return `Rough projection: ${number(days / 30, 1)} months at this wear rate.`;
    return `Rough projection: ${number(days, 1)} days at this wear rate.`;
  }
  return "Time projection unavailable.";
}

function warningText(drive) {
  const warnings = [];
  if (drive.smart_status === "unhealthy") warnings.push("SMART reports a problem.");
  if (drive.temperature_status === "critical") warnings.push("Temperature is critical.");
  else if (drive.temperature_status === "warning") warnings.push("Temperature is above the warning threshold.");
  if (typeof drive.endurance_remaining_percent === "number" && drive.endurance_remaining_percent <= 20) {
    warnings.push("Rated endurance remaining is low.");
  }
  if (drive.collector_errors?.length) warnings.push("Some controller details could not be read.");
  return warnings;
}

function chart(points, driveId) {
  const used = points
    .map((point) => point.used_percent)
    .filter((value) => typeof value === "number" && Number.isFinite(value));
  if (used.length < 2) {
    return `<div class="chart-heading"><span>Endurance history</span><span>Collecting samples…</span></div>`;
  }
  const min = Math.min(...used);
  const max = Math.max(...used);
  const spread = Math.max(1, max - min);
  const path = used
    .map((value, index) => {
      const x = (index / (used.length - 1)) * 100;
      const y = 58 - ((value - min) / spread) * 48;
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
  return `
    <div class="chart-heading"><span>Endurance used history</span><span>${number(min, 2)}% → ${number(max, 2)}%</span></div>
    <svg class="chart" id="chart-${driveId}" viewBox="0 0 100 60" preserveAspectRatio="none" aria-label="Endurance used history">
      <line class="chart-guide" x1="0" y1="10" x2="100" y2="10"></line>
      <line class="chart-guide" x1="0" y1="58" x2="100" y2="58"></line>
      <path class="chart-line" d="${path}"></path>
    </svg>`;
}

function driveCard(drive, points = []) {
  const remaining = drive.endurance_remaining_percent;
  const lifeAvailable = typeof remaining === "number";
  const status = drive.smart_status || "unknown";
  const temperatureStatus = drive.temperature_status || "unknown";
  const warnings = warningText(drive);
  const warningsMarkup = warnings.length
    ? `<div class="warnings">${warnings.map((warning) => `<div>${escapeHtml(warning)}</div>`).join("")}</div>`
    : "";
  const lifeTitle = lifeAvailable ? `${number(remaining, 1)}% remaining` : "Not available";
  const lifeNote = lifeAvailable
    ? drive.endurance_source === "nvme-percentage-used"
      ? "From the NVMe percentage-used health counter."
      : "From a clearly-labelled SATA SMART attribute."
    : drive.type === "ssd"
      ? "This SSD did not expose a standardized endurance percentage."
      : "Hard drives do not expose SSD endurance data.";

  return `
    <article class="drive-card">
      <div class="drive-heading">
        <div>
          <h2 class="drive-model" title="${escapeHtml(drive.model)}">${escapeHtml(drive.model)}</h2>
          <p class="drive-meta" title="${escapeHtml(drive.path)}">${escapeHtml(drive.path)} · ${escapeHtml(drive.transport.toUpperCase())} · ${bytes(drive.size_bytes)}</p>
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
        <div class="metric"><span class="metric-label">Serial</span><strong class="metric-value" title="${escapeHtml(drive.serial)}">${escapeHtml(drive.serial)}</strong></div>
      </div>

      <div class="chart-wrap">${chart(points, drive.id)}</div>
      ${warningsMarkup}
    </article>`;
}

async function history(driveId) {
  const response = await fetch(`/api/drives/${encodeURIComponent(driveId)}/history?hours=720`, { cache: "no-store" });
  if (!response.ok) return [];
  const payload = await response.json();
  return Array.isArray(payload.points) ? payload.points : [];
}

async function load(force = false) {
  if (state.loading) return;
  state.loading = true;
  const button = $("#refresh");
  button.disabled = true;
  try {
    const response = await fetch(`/api/drives${force ? "?force=true" : ""}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "The host inventory could not be read.");

    const drives = Array.isArray(payload.drives) ? payload.drives : [];
    const driveContainer = $("#drives");
    if (!drives.length) {
      driveContainer.innerHTML = `<div class="empty-card">No NVMe or SATA disks were found. Check the container device permissions and host inventory.</div>`;
    } else {
      const histories = await Promise.all(drives.map((drive) => history(drive.id)));
      driveContainer.innerHTML = drives.map((drive, index) => driveCard(drive, histories[index])).join("");
    }
    const status = $("#status");
    status.className = "status";
    status.textContent = `${drives.length} supported disk${drives.length === 1 ? "" : "s"} · refreshing every ${payload.poll_seconds || 15}s`;
    $("#updated").textContent = `Last reading: ${date(payload.generated_at)}`;
  } catch (error) {
    const status = $("#status");
    status.className = "status error";
    status.textContent = error instanceof Error ? error.message : "Unable to read drive data.";
    $("#drives").innerHTML = `<div class="empty-card">Drive readings are unavailable. Check the collector permissions and host utilities, then refresh.</div>`;
    $("#updated").textContent = "The previous reading, if any, may be stale.";
  } finally {
    state.loading = false;
    button.disabled = false;
  }
}

$("#refresh").addEventListener("click", () => load(true));
load();
state.timer = window.setInterval(() => load(false), REFRESH_MS);
