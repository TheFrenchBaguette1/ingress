import { PatchDataError, retryWhileBusy } from './data.js';

const jobIdPattern = /^[a-f0-9]{32}$/;
const numeric = new Intl.NumberFormat('en-US', { maximumSignificantDigits: 5 });
const dateTick = new Intl.DateTimeFormat('en-US', {
  year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC',
});
const timeTick = new Intl.DateTimeFormat('en-US', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23', timeZone: 'UTC',
});
const radians = Math.PI / 180;

// The viewer grid uses a north-up, east-left gnomonic projection.
export function tangentPlaneToSky(center, xi, eta) {
  const dec0 = center.decDeg * radians;
  const denominator = Math.cos(dec0) - eta * Math.sin(dec0);
  const ra = center.raDeg + Math.atan2(xi, denominator) / radians;
  return {
    raDeg: (ra % 360 + 360) % 360,
    decDeg: Math.atan2(Math.sin(dec0) + eta * Math.cos(dec0), Math.hypot(denominator, xi)) / radians,
  };
}
export function imagePositionToSky(center, fov, x, y) {
  const width = fov / 60 * radians;
  return tangentPlaneToSky(center, (0.5 - x) * width, (0.5 - y) * width);
}
export function skyToImagePosition(center, fov, target) {
  const dec = target.decDeg * radians, dec0 = center.decDeg * radians;
  const dra = (target.raDeg - center.raDeg) * radians;
  const denominator = Math.sin(dec0) * Math.sin(dec) + Math.cos(dec0) * Math.cos(dec) * Math.cos(dra);
  if (denominator <= 0) return null;
  const xi = Math.cos(dec) * Math.sin(dra) / denominator;
  const eta = (Math.cos(dec0) * Math.sin(dec) - Math.sin(dec0) * Math.cos(dec) * Math.cos(dra)) / denominator;
  const width = fov / 60 * radians;
  const x = 0.5 - xi / width, y = 0.5 - eta / width;
  return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
}

async function requestCurve(path, { dates, signal }) {
  const response = await fetch(path, {
    method: dates ? 'POST' : 'GET', cache: 'no-store', signal,
    headers: dates ? { 'Content-Type': 'application/json', Accept: 'application/json' } : { Accept: 'application/json' },
    ...(dates ? { body: JSON.stringify(dates) } : {}),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail;
    const message = typeof detail === 'string' ? detail : detail?.message;
    const busy = dates && response.status === 503 && detail?.code === 'light_curve_busy';
    const error = new PatchDataError(busy ? 'busy' : 'light_curve_unavailable',
      message || 'The light curve service is temporarily unavailable.');
    if (busy) {
      const header = response.headers.get('Retry-After');
      if (header && /^\d+$/.test(header)) error.retryAfterMs = Number(header) * 1000;
    }
    throw error;
  }
  if (!jobIdPattern.test(payload?.light_curve_id) || !['queued', 'running', 'complete'].includes(payload.status)
      || !Array.isArray(payload.points) || !Array.isArray(payload.channels)) {
    throw new PatchDataError('invalid_response', 'The light curve service returned incomplete data.');
  }
  return payload;
}

export function startLightCurve(jobId, { from, to, target, signal, onBusy }) {
  const dates = {
    ...(from ? { start_date: from } : {}), ...(to ? { end_date: to } : {}),
    ...(target ? { ra_deg: target.raDeg, dec_deg: target.decDeg } : {}),
  };
  return retryWhileBusy(() => requestCurve(`/api/flipbook/${jobId}/light-curve`, { dates, signal }), {
    signal, onBusy, delayMs: 3000, maxRetries: 5, maxWaitMs: 20_000,
    timeoutMessage: 'The light curve queue is still full. Try again shortly.',
  });
}

export function readLightCurve(id, signal) {
  return requestCurve(`/api/light-curve/${id}`, { signal });
}

export function lightCurveChannelKey(point) { return `${point.detector}:${point.channel}`; }

function value(number) {
  if (!Number.isFinite(number)) return '—';
  const magnitude = Math.abs(number);
  return magnitude && (magnitude < 0.001 || magnitude >= 1e6) ? number.toExponential(3) : numeric.format(number);
}

function observationTime(point) {
  return String(point.time_utc || '').replace('T', ' ').replace(/(?:Z|\+00:00| UTC)$/, '') + ' UTC';
}

function chart(points, active, esc, spectrum = false) {
  const width = 660, height = 250, left = 83, right = 604, top = 19, bottom = 196;
  const xKey = spectrum ? 'wavelength_um' : 'time_mjd';
  const first = points[0][xKey];
  const last = points.at(-1)[xKey];
  const lower = Math.min(...points.map(point => point.flux_ujy - point.error_ujy));
  const upper = Math.max(...points.map(point => point.flux_ujy + point.error_ujy));
  const pad = (upper - lower || Math.abs(upper) || 1) * 0.08;
  const min = lower - pad, max = upper + pad;
  const x = input => (last === first ? (left + right) / 2 : left + (input - first) / (last - first) * (right - left)).toFixed(2);
  const y = flux => (bottom - (flux - min) / (max - min) * (bottom - top)).toFixed(2);
  const mjdDate = time => new Date(Math.round((time - 40587) * 86400000));
  const ticks = last === first ? [first] : [first, (first + last) / 2, last];
  const tickFluxes = Array.from({ length: 5 }, (_, index) => lower + (upper - lower) * index / 4);
  const grid = tickFluxes.map(flux => `<line x1="${left}" x2="${right}" y1="${y(flux)}" y2="${y(flux)}"/>
    <text x="${left - 8}" y="${y(flux)}" text-anchor="end" dominant-baseline="middle">${esc(value(flux))}</text>`).join('');
  const timeLabel = Math.floor(first) === Math.floor(last) && first !== last ? timeTick : dateTick;
  const xLabels = ticks.map(tick => `<text x="${x(tick)}" y="${bottom + 19}" text-anchor="middle">${esc(
    spectrum ? value(tick) : timeLabel.format(mjdDate(tick)),
  )}</text>`).join('');
  const marks = points.map(point => {
    const cx = x(point[xKey]), cy = y(point.flux_ujy);
    const high = y(point.flux_ujy + point.error_ujy), low = y(point.flux_ujy - point.error_ujy);
    return `<g class="light-curve-point${point === active ? ' selected' : ''}" data-action="light-curve-point" data-product="${esc(point.product_id)}">
      <title>${esc(`${observationTime(point)} · ${value(point.wavelength_um)} µm · ${value(point.flux_ujy)} ± ${value(point.error_ujy)} µJy`)}</title>
      <path d="M${cx} ${high}V${low}M${cx - 4} ${high}H${Number(cx) + 4}M${cx - 4} ${low}H${Number(cx) + 4}"/>
      <circle class="light-curve-marker" cx="${cx}" cy="${cy}" r="4"/>
      <circle class="light-curve-hit" cx="${cx}" cy="${cy}" r="11"/>
    </g>`;
  }).join('');
  return `<div class="light-curve-plot"><svg viewBox="0 0 ${width} ${height}" aria-hidden="true">
    <g class="light-curve-grid">${grid}${xLabels}<line x1="${left}" x2="${right}" y1="${bottom}" y2="${bottom}"/></g>
    <g class="light-curve-axis"><text text-anchor="middle" transform="translate(14 ${(top + bottom) / 2}) rotate(-90)">Flux density (µJy)</text>
    <text x="${(left + right) / 2}" y="${height - 11}" text-anchor="middle">${spectrum ? 'Wavelength (µm)' : 'Observation time (UTC)'}</text></g>
    ${marks}</svg></div>`;
}

function pointDetails(point, points, esc) {
  const details = [
    ['Flux density', `${value(point.flux_ujy)} ± ${value(point.error_ujy)} µJy (1σ)`],
    ['Statistical uncertainty', `${value(point.stat_error_ujy)} µJy`],
    ['Calibration scatter', `${value(point.systematic_error_ujy)} µJy`],
    ['Wavelength · bandwidth', `${value(point.wavelength_um)} µm · ${value(point.bandwidth_um)} µm`],
    ['Fit residual', `${value(point.fit_quality)}σ`],
    ['Exposure time', `${observationTime(point)}${point.time_source === 'archive_start' ? ' (archive start)' : ''}`],
    ['Product', point.product_id],
  ];
  return `<label class="light-curve-field light-curve-exposures">Exposure <select data-range="light-curve-exposure">
    ${points.map(item => `<option value="${esc(item.product_id)}" ${item === point ? 'selected' : ''}>${esc(observationTime(item))} · ${esc(value(item.flux_ujy))} ± ${esc(value(item.error_ujy))} µJy</option>`).join('')}
    </select></label>
    <dl class="light-curve-details">${details.map(([label, measure]) => `<div><dt>${esc(label)}</dt><dd>${esc(measure)}</dd></div>`).join('')}</dl>`;
}

const exclusionLabels = {
  missing_time: 'missing observation time', duplicate_observation: 'duplicate exposure',
  unsupported_product: 'unsupported FITS product', archive_unavailable: 'archive unavailable',
  calibration_unavailable: 'calibration unavailable', outside_detector: 'target outside detector',
  outside_channel: 'target outside the calibrated channels',
  invalid_stamp: 'invalid detector pixels', invalid_psf: 'invalid point-spread function',
  masked_core: 'masked star core', saturated_core: 'saturated star core',
  insufficient_psf_coverage: 'insufficient coverage around the star',
  invalid_fit: 'fit could not be calculated', poor_fit: 'poor fit', measurement_failed: 'measurement failed',
};

export function lightCurveContents(curve, { enabled, currentProduct, target, esc }) {
  if (!enabled) return '';
  if (!curve) return '<button type="button" class="soft-button" data-action="measure-light-curve">Light curve</button>';
  const { payload, measuring, error } = curve;
  const channel = payload?.channels.find(entry => lightCurveChannelKey(entry) === curve.channel) || payload?.channels[0];
  const usable = (payload?.points || []).filter(point => Number.isFinite(point.time_mjd)
    && Number.isFinite(point.wavelength_um) && Number.isFinite(point.flux_ujy)
    && Number.isFinite(point.error_ujy) && point.error_ujy > 0);
  const points = channel ? usable.filter(point => lightCurveChannelKey(point) === lightCurveChannelKey(channel))
    .sort((a, b) => a.time_mjd - b.time_mjd) : [];
  const active = points.find(point => point.product_id === curve.product) ||
    points.find(point => point.product_id === currentProduct) || points[0];
  const skipped = Object.entries(payload?.exclusions || {}).filter(([, count]) => Number.isInteger(count) && count > 0);
  const skippedCount = skipped.reduce((sum, [, count]) => sum + count, 0);
  const status = payload ? `${payload.status === 'queued' ? 'Queued · ' : ''}${payload.processed_count} of ${payload.total_count} loaded exposures checked · ${payload.points.length} measured · ${skippedCount} skipped`
    : curve.waiting ? 'Waiting for a photometry worker…' : measuring ? 'Starting measurements…' : '';
  const variability = channel?.variability === 'candidate' ? 'Candidate variability'
    : channel?.variability === 'no_significant_change' ? 'No significant change detected' : 'Too few comparable observations';
  const preliminary = payload?.status !== 'complete';
  return `<section class="light-curve-card" aria-labelledby="light-curve-title">
    <div class="light-curve-head"><h2 id="light-curve-title">Light curve</h2>
    <div>${measuring ? '' : `<button type="button" class="soft-button" data-action="measure-light-curve">${error ? 'Try again' : 'Measure again'}</button>`}
    ${target ? '<button type="button" class="soft-button" data-action="measure-crosshair">Use crosshair</button>' : ''}
    <button type="button" class="soft-button" data-action="close-light-curve">${measuring ? 'Cancel' : 'Close'}</button></div></div>
    <p class="light-curve-intro">${target ? `Selected star · RA ${esc(target.raDeg.toFixed(6))}° · Dec ${esc(target.decDeg.toFixed(6))}°` : 'Measuring the star at the crosshair.'} Loaded exposures within the timeline dates.</p>
    ${error ? `<p class="light-curve-error" role="alert">${esc(error)}</p>` : ''}
    ${status ? `<p class="light-curve-progress" role="status">${esc(status)}</p>` : ''}
    ${measuring ? `<progress aria-label="Light curve measurements" ${payload?.total_count ? `max="${payload.total_count}" value="${payload.processed_count}"` : ''}></progress>` : ''}
    ${active ? `<label class="light-curve-field">Wavelength <select data-range="light-curve-channel">
      ${payload.channels.map(item => `<option value="${esc(lightCurveChannelKey(item))}" ${item === channel ? 'selected' : ''}>${esc(item.detector)} · channel ${esc(item.channel)} · ${esc(value(item.wavelength_um))} µm</option>`).join('')}
      </select></label>
      <p class="light-curve-result"><strong>${esc(preliminary ? 'Preliminary' : variability)}</strong> · ${esc(channel.day_count)} comparable UTC days${!preliminary && Number.isFinite(channel.p_value) ? ` · p = ${esc(value(channel.p_value))}` : ''}</p>
      ${chart(points, active, esc)}
      ${pointDetails(active, points, esc)}
      <details class="light-curve-spectrum"><summary>Spectrum across observation dates</summary>
      ${chart(usable.sort((a, b) => a.wavelength_um - b.wavelength_um), active, esc, true)}</details>` : !measuring && !error ? '<p class="light-curve-progress">No usable measurements in the loaded exposures.</p>' : ''}
    ${curve.frameNotice ? `<p class="light-curve-progress" role="status">${esc(curve.frameNotice)}</p>` : ''}
    ${skipped.length ? `<details class="light-curve-exclusions"><summary>Why exposures were skipped</summary><ul>
      ${skipped.map(([reason, count]) => `<li>${count} · ${esc(exclusionLabels[reason] || reason.replaceAll('_', ' '))}</li>`).join('')}</ul></details>` : ''}
    ${active ? '<p class="light-curve-note">Error bars show 1σ. The variability test uses exposures with comparable passbands. Measurements assume an isolated star at a fixed position.</p>' : ''}
  </section>`;
}
