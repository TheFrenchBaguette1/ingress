import {
  parseCoordinates, fetchZoomedRealPatch, restoreRealSequence, loadBundledRealPatch,
  prepareRealBands, preparedBandPatch, startFlipbook, readFlipbook, requestFlipbookBand,
  waitForFlipbookUpdate, flipbookBandPatch,
} from './data.js';
import { homeStory, heroArt, heroOrnaments } from './story.js';
import {
  imagePositionToSky, lightCurveChannelKey, lightCurveContents, readLightCurve,
  skyToImagePosition, startLightCurve, tangentPlaneToSky,
} from './light-curve.js';

const app = document.querySelector('#app');
const staticPreview = document.querySelector('meta[name="ingress-static-preview"]')?.getAttribute?.('content') === 'true';
const liveFlipbook = !staticPreview &&
  document.querySelector('meta[name="ingress-live-flipbook"]')?.getAttribute?.('content') === 'true';
const fullViewerUrl = 'https://ideal-train-jjgp96g4g46rfqgwv-5173.app.github.dev/';
const params = new URLSearchParams(location.search);
const DEFAULT_FOV = 5;
const ALL_WAVELENGTHS = Object.freeze({ number: 'all', rangeUm: [0.744, 5.002], color: '#e2c54a' });
// Fixed sky-patch centers from Henry's Ingress test targets.
const TEST_TARGETS = [
  { id: 'barnard', name: 'Barnard’s Star', ra: '269.4464', dec: '4.7686' },
  { id: 'epsilon-indi-b', name: 'Epsilon Indi B', ra: '331.096', dec: '-56.801' },
  { id: 'wise-0855', name: 'WISE 0855−0714', ra: '133.795129', dec: '-7.245147' },
  { id: 'rw-aurigae', name: 'RW Aurigae', ra: '76.9564', dec: '30.4014' },
  { id: 'r-draconis', name: 'R Draconis', ra: '248.1675', dec: '66.7550' },
];
function bandFromQuery(query) {
  const number = Number(query.get('band'));
  if (Number.isInteger(number) && number >= 1 && number <= 6) return number;
  return liveFlipbook && !query.get('sequence') ? 'all' : 4;
}
const PLAYBACK_RATES = [1, 2, 4, 6, 8, 12, 16, 20];
const PLAYBACK_LOOKAHEAD = 8;
const frameDateFormat = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC', day: 'numeric', month: 'short', year: 'numeric',
});
const frameMonthFormat = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC', month: 'short', year: 'numeric',
});
function fovFromQuery(query) {
  const value = Number(query.get('fov'));
  if (Number.isInteger(value) && value >= 1 && value <= 20) return value;
  // Earlier shared coordinate links omitted fov and displayed a 10′ field.
  return query.get('ra') && query.get('dec') ? 10 : DEFAULT_FOV;
}
const initialFov = fovFromQuery(params);
const state = {
  view: 'home',
  ra: params.get('ra') || '', dec: params.get('dec') || '',
  from: validDate(params.get('from')) ? params.get('from') : '',
  to: validDate(params.get('to')) ? params.get('to') : '',
  fov: initialFov,
  customFov: ![5, 10, 20].includes(initialFov),
  band: bandFromQuery(params), activeDateId: params.get('date') || '',
  patch: null, error: null, raError: '', decError: '', dateError: '',
  headerRa: null, headerDec: null,
  playing: false, playbackRate: 6, drawer: '', aboutOpen: false, request: null, imageError: false,
  zoomRequest: null, zoomError: null,
  linkedSequenceId: /^[a-f0-9]{32}$/.test(params.get('sequence') || '') ? params.get('sequence') : null,
  prepared: null,
  recovery: null,
  sampleFallback: null,
  lightCurve: null,
  lightCurveTarget: null,
};
let playbackTimer = null;
let renderedFrames = [];
const playbackImages = new Map();
let loadSequence = 0;
let restoreDrawerAction = null;
const realCache = new Map();
const preparedImageCache = new Map();
const retiredPreviewUrls = new Set();
let lastRealView = null;
let activeFlipbook = null;

const ACTIVE_BAND_STATUSES = new Set(['queued', 'running']);
const FLIPBOOK_IMAGE_WORKERS = 8;

function prunePreparedImages() {
  const keep = new Set();
  const keepBlobs = new Set();
  for (const group of [state.prepared, lastRealView?.prepared]) {
    for (const patch of group?.patches.values() || []) {
      for (const frame of patch.frames) {
        keep.add(frame.imageUrl);
        if (frame.previewUrl) keepBlobs.add(frame.previewUrl);
      }
    }
  }
  for (const patch of [state.patch, lastRealView?.patch]) {
    for (const frame of patch?.frames || []) {
      keep.add(frame.imageUrl);
      if (frame.previewUrl) keepBlobs.add(frame.previewUrl);
    }
  }
  for (const url of activeFlipbook?.previews.keys() || []) keep.add(url);
  for (const url of retiredPreviewUrls) {
    if (!keepBlobs.has(url)) {
      URL.revokeObjectURL(url);
      retiredPreviewUrls.delete(url);
    }
  }
  for (const [url, cached] of preparedImageCache) {
    if (!keep.has(url)) {
      URL.revokeObjectURL(cached.blobUrl);
      preparedImageCache.delete(url);
    }
  }
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}
function validDate(value) {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}
function rangeLabel(band) { return band?.rangeUm?.map(n => Number(n).toFixed(3)).join('–') + ' µm'; }
function displayDate(iso) {
  const date = new Date(`${String(iso).slice(0, 10)}T12:00:00Z`);
  return Number.isNaN(date.getTime()) ? String(iso).slice(0, 10) :
    frameDateFormat.format(date);
}
function displayMonth(iso) {
  const date = new Date(`${String(iso).slice(0, 10)}T12:00:00Z`);
  return frameMonthFormat.format(date);
}
function bandColor(band) { return band?.color || '#e2c54a'; }
function selectedBand() {
  return state.band === 'all' ? ALL_WAVELENGTHS : state.patch?.bands.find(b => b.number === state.band);
}
function allBandFrames() {
  if (!state.patch) return [];
  const order = new Map(state.patch.dates.map((d, i) => [d.id, i]));
  return state.patch.frames.filter(f => state.band === 'all' || f.band === state.band)
    .sort((a, b) => (order.get(a.dateId) ?? 0) - (order.get(b.dateId) ?? 0));
}
function inRange(dateId) {
  const day = String(dateId).slice(0, 10);
  return (!state.from || day >= state.from) && (!state.to || day <= state.to);
}
function visibleFrames() { return allBandFrames().filter(f => inRange(f.dateId)); }
function realCacheKey(coords, band) {
  return [coords.raDeg, coords.decDeg, state.fov, state.from, state.to, band].join('|');
}
function preparationKey(coords, from, to) {
  return [coords.raDeg, coords.decDeg, from || '2025-05-01',
    to || new Date().toISOString().slice(0, 10)].join('|');
}
function realViewSnapshot() {
  return {
    patch: state.patch, ra: state.ra, dec: state.dec, from: state.from, to: state.to,
    fov: state.fov, customFov: state.customFov, band: state.band,
    activeDateId: state.activeDateId, linkedSequenceId: state.linkedSequenceId,
    prepared: state.prepared,
  };
}
function cancelZoom() {
  state.zoomRequest?.controller.abort();
  state.zoomRequest = null;
  state.zoomError = null;
}
function sameSkyPosition(first, second) {
  const a = parseCoordinates(first.ra, first.dec);
  const b = parseCoordinates(second.ra, second.dec);
  return a.isValid && b.isValid && Math.abs(a.raDeg - b.raDeg) < 1e-5 &&
    Math.abs(a.decDeg - b.decDeg) < 1e-5;
}
function sameRealQuery(first, second) {
  return sameSkyPosition(first, second) && first.band === second.band &&
    first.fov === second.fov &&
    (first.from || '2025-05-01') === (second.from || '2025-05-01') &&
    (first.to || new Date().toISOString().slice(0, 10)) ===
      (second.to || new Date().toISOString().slice(0, 10));
}
function matchesBundledExample(query) {
  return sameSkyPosition(query, { ra: '180', dec: '-30' }) &&
    query.band === 4 && query.fov === 5 &&
    (query.from || '2025-05-01') === '2025-05-01' &&
    (query.to || new Date().toISOString().slice(0, 10)) === '2026-11-15';
}
function restoreRealView() {
  if (!lastRealView) return;
  cancelZoom();
  Object.assign(state, lastRealView);
  state.view = 'viewer';
  state.error = null;
  state.headerRa = null;
  state.headerDec = null;
  state.imageError = false;
  settleFrame();
}
function rangeNeedsRefresh() {
  if (staticPreview) return false;
  const range = state.patch?.queryRange;
  if (state.patch?.source !== 'real' || !range) return false;
  const requestedFrom = state.from || '2025-05-01';
  const requestedTo = state.to || new Date().toISOString().slice(0, 10);
  return requestedFrom < range.from || requestedTo > range.to;
}
function currentFrame() {
  const frames = visibleFrames();
  return frames.find(f => f.dateId === state.activeDateId) || frames[0] || null;
}
function skyFootprint(target, fovArcmin) {
  // The backend uses a north-up/east-left RA---TAN/DEC--TAN output grid.
  // Evaluate its four field-edge corners for locally cropped zooms too.
  const half = fovArcmin / 120 * Math.PI / 180;
  // Same corner order as the saved grid: bottom-left, bottom-right,
  // top-right, top-left in science-array coordinates.
  return [[half, -half], [-half, -half], [-half, half], [half, half]].map(([xi, eta]) => {
    const sky = tangentPlaneToSky(target, xi, eta);
    return { ra_deg: Number(sky.raDeg.toFixed(9)), dec_deg: Number(sky.decDeg.toFixed(9)) };
  });
}
function displayedDimensions(patch, fov) {
  const fraction = fov / (patch.baseFovArcmin || fov);
  return {
    width: Number(((patch.grid?.widthPixels || patch.sizePx || 0) * fraction).toFixed(6)),
    height: Number(((patch.grid?.heightPixels || patch.sizePx || 0) * fraction).toFixed(6)),
    units: 'aligned science pixels in displayed region',
  };
}
function frameAudit(patch, frame, fov) {
  return {
    sequence_id: patch.sequenceId,
    product_id: frame?.productId || null,
    exposure_url: frame?.sourceUrl || null,
    observation_utc: frame?.observedAt || null,
    detector_band: frame?.detector || null,
    wavelength_at_target_um: frame?.wavelengthUm ?? null,
    displayed_science_pixel_dimensions: displayedDimensions(patch, fov),
    output_grid_pixel_dimensions: frame?.pixelDimensions || null,
    native_parent_cutout_pixel_dimensions: frame?.nativeCutoutDimensions || null,
    sky_footprint_ra_dec_deg: fov === patch.baseFovArcmin && patch.grid?.skyFootprint || skyFootprint(patch.target, fov),
    wcs_header_of_loaded_grid: patch.grid?.wcsHeader || null,
    loaded_raster_dimensions: { width: patch.grid?.widthPixels, height: patch.grid?.heightPixels },
    loaded_raster_fov_arcmin: patch.baseFovArcmin,
    brightness_scale: patch.displayByBand?.[frame?.band] || patch.display,
    image_url: frame?.imageUrl || null,
    timeline_product_ids: patch.frames.map(item => item.productId),
  };
}
function logFovTransition(beforePatch, beforeFrame, beforeFov, afterPatch, afterFrame, afterFov) {
  if (beforePatch?.source !== 'real') return;
  console.info('[Ingress] Field-of-view zoom, same selected exposures', JSON.stringify({
    before: frameAudit(beforePatch, beforeFrame, beforeFov),
    after: frameAudit(afterPatch, afterFrame, afterFov),
    timeline_frame_count_before: beforePatch.frames.length,
    timeline_frame_count_after: afterPatch.frames.length,
    selected_product_unchanged: beforeFrame?.sourceUrl === afterFrame?.sourceUrl,
  }));
}
function settleFrame() {
  const frame = currentFrame();
  state.activeDateId = frame?.dateId || '';
  state.imageError = false;
  if (!frame) stopPlayback();
}
function stopPlayback({ keepImages = false } = {}) {
  if (playbackTimer) clearInterval(playbackTimer);
  playbackTimer = null;
  state.playing = false;
  if (!keepImages) playbackImages.clear();
}
function warmPlaybackWindow(start = Math.max(0, renderedFrames.findIndex(frame => frame.dateId === state.activeDateId))) {
  if (typeof Image !== 'function' || !renderedFrames.length) return;
  const upcoming = Array.from({ length: Math.min(PLAYBACK_LOOKAHEAD, renderedFrames.length) },
    (_, offset) => renderedFrames[(start + offset + 1) % renderedFrames.length]);
  const keep = new Set(upcoming.map(frame => frame.previewUrl || frame.imageUrl));
  for (const src of playbackImages.keys()) {
    if (!keep.has(src)) playbackImages.delete(src);
  }
  for (const src of keep) {
    if (playbackImages.has(src)) continue;
    const image = new Image();
    const entry = { image, ready: false };
    playbackImages.set(src, entry);
    image.src = src;
    // Allow the normal image error UI if a preview could not be decoded.
    image.decode().catch(() => {}).finally(() => { entry.ready = true; });
  }
}
function updatePlaybackButton() {
  const button = app.querySelector('[data-action="play"]');
  if (!button) return false;
  button.textContent = state.playing ? '❚❚ Pause' : '▶ Play';
  button.setAttribute('aria-label', state.playing ? 'Pause timeline' : 'Play timeline');
  return true;
}
function updateFrameDetails(frame = currentFrame()) {
  app.querySelectorAll('[data-frame-details]').forEach(body => {
    const details = body.closest('details');
    details.querySelector('[data-frame-notes]').textContent = frameNoteCount(frame);
    if (details.open) body.innerHTML = frameDetailsBody(frame, selectedBand());
  });
}
function paintFrame(frame, index) {
  const image = app.querySelector('[data-frame-image]');
  if (!image) return false;
  const band = selectedBand();
  const presentation = framePresentation(frame, band, index, renderedFrames.length);
  image.alt = presentation.imageAlt;
  image.src = frame.previewUrl || frame.imageUrl;
  app.querySelector('.image-view').setAttribute('aria-label', presentation.imageLabel);
  app.querySelector('.frame-date').textContent = presentation.dateLabel;
  app.querySelector('.frame-ordinal').textContent = presentation.ordinal;
  app.querySelector('[data-frame-wavelength]').textContent = presentation.wavelength;
  app.querySelector('.wavelength-chip')?.style.setProperty('--band-color', presentation.color);

  const scrubber = app.querySelector('[data-range="frame"]');
  scrubber.value = index + 1;
  scrubber.setAttribute('aria-valuetext', presentation.scrubberLabel);
  const previous = app.querySelector('.date-dot[aria-current="true"]');
  const next = app.querySelector(`.date-dot[data-date="${CSS.escape(frame.dateId)}"]`);
  previous?.classList.remove('selected');
  previous?.setAttribute('aria-current', 'false');
  next?.classList.add('selected');
  next?.setAttribute('aria-current', 'true');
  updatePlaybackButton();
  updateFrameDetails(frame);
  return true;
}
function selectFrame(index, { share = true } = {}) {
  const frame = renderedFrames[index];
  if (!frame) return;
  state.activeDateId = frame.dateId;
  state.imageError = false;
  if (share) syncUrl();
  if (!paintFrame(frame, index)) render();
}
function stepFrame(direction, { playing = false } = {}) {
  if (!renderedFrames.length) return;
  const index = Math.max(0, renderedFrames.findIndex(frame => frame.dateId === state.activeDateId));
  const next = (index + direction + renderedFrames.length) % renderedFrames.length;
  if (playing) {
    warmPlaybackWindow(index);
    const frame = renderedFrames[next];
    const image = playbackImages.get(frame.previewUrl || frame.imageUrl);
    if (image && !image.ready) return;
  }
  selectFrame(next, { share: !playing });
}
function setPlayback(on) {
  stopPlayback({ keepImages: on });
  if (on && renderedFrames.length > 1) {
    state.playing = true;
    warmPlaybackWindow();
    playbackTimer = setInterval(() => {
      if (document.hidden) return;
      stepFrame(1, { playing: true });
    }, 1000 / state.playbackRate);
  } else {
    syncUrl();
  }
  if (!updatePlaybackButton()) render();
}
function syncUrl() {
  const url = new URL(location.href);
  for (const key of ['ra', 'dec', 'source', 'band', 'date', 'fov', 'from', 'to', 'sequence']) url.searchParams.delete(key);
  if (state.view !== 'home' && state.ra && state.dec) {
    url.searchParams.set('ra', state.ra);
    url.searchParams.set('dec', state.dec);
    if (state.band) url.searchParams.set('band', String(state.band));
    if (state.activeDateId) url.searchParams.set('date', state.activeDateId);
    url.searchParams.set('fov', String(state.fov));
    if (state.from) url.searchParams.set('from', state.from);
    if (state.to) url.searchParams.set('to', state.to);
    if (state.linkedSequenceId) url.searchParams.set('sequence', state.linkedSequenceId);
  }
  history.replaceState(null, '', url);
}
function headingMark() {
  return '<svg class="brand-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="7"/><path d="M12 0v5m0 14v5M0 12h5m14 0h5"/></svg>';
}
function header() {
  const compact = state.view !== 'home';
  return `<header class="site-header">
    <button class="brand" type="button" data-action="home" aria-label="Ingress: go to the start page">${headingMark()}<span class="brand-name">Ingress</span><span class="brand-subtitle">SPHEREx sky-patch time viewer</span></button>
    ${staticPreview ? `<a class="pill-button" href="${fullViewerUrl}">Open full viewer ↗</a>` : compact ? `<form class="header-form" id="header-form" novalidate>
      <label>RA <input class="coord-input" name="ra" aria-label="Right ascension" aria-invalid="${!!state.raError}" placeholder="08h55m10.831s" value="${esc(state.headerRa ?? state.ra)}" autocomplete="off"></label>
      <label>Dec <input class="coord-input" name="dec" aria-label="Declination" aria-invalid="${!!state.decError}" placeholder="-07d14m42.53s" value="${esc(state.headerDec ?? state.dec)}" autocomplete="off"></label>
      <button class="soft-button" type="submit">Show patch</button>
    </form><button class="mobile-change" data-action="home" type="button">Change patch</button>` : ''}
  </header>${compact && (state.raError || state.decError) ? `<div class="header-validation" role="alert">${esc(state.raError || state.decError)}</div>` : ''}<div class="spectrum-rule ${compact ? '' : 'sc-prog'}" aria-hidden="true"></div>`;
}
function footer() {
  return `<footer class="site-footer">Team Ingress · NASA Space Apps Challenge 2026 project · Independent of NASA.
    Available archive links and data notes appear under “About this frame.”
    <a href="https://www.spaceappschallenge.org/2026/challenges/planet-x-and-spherex/" target="_blank" rel="noopener noreferrer">Challenge brief ↗</a></footer>`;
}
function home() {
  return `<main class="home">
    <section class="home-hero tl0" id="start" aria-labelledby="hero-title">
      ${heroOrnaments()}
      <div>
        <div class="h-title">
        <div class="eyebrow">SPHEREx · Sky-patch time viewer</div>
        <h1 class="hero-title" id="hero-title">Pick a place in the sky.<br><em>Flip through dates</em><br>to see what changed.</h1>
        <p class="hero-description">Enter sky coordinates, then step through available images to spot light that moves or changes.</p>
        </div>
        <form class="search-card" id="home-form" novalidate>
          <div class="target-picker" role="group" aria-labelledby="target-picker-label">
            <div class="card-heading" id="target-picker-label">Try a known target</div>
            <div class="example-row">${TEST_TARGETS.map(target =>
              `<button type="button" class="pill-button" data-action="target-preset" data-target="${esc(target.id)}">${esc(target.name)}</button>`).join('')}</div>
          </div>
          <div class="card-heading">Enter coordinates</div>
          <div class="field-grid">
            <label class="field"><span class="field-label">RA <span>(right ascension)</span></span><input class="coord-input" name="ra" id="home-ra" placeholder="08h55m10.831s" autocomplete="off" value="${esc(state.ra)}" aria-invalid="${!!state.raError}" aria-describedby="ra-help"><span class="${state.raError ? 'input-error' : 'input-help'}" id="ra-help">${esc(state.raError || 'Hours or degrees, e.g. 08h55m10.831s or 133.795')}</span></label>
            <label class="field"><span class="field-label">Dec <span>(declination)</span></span><input class="coord-input" name="dec" id="home-dec" placeholder="-07d14m42.53s" autocomplete="off" value="${esc(state.dec)}" aria-invalid="${!!state.decError}" aria-describedby="dec-help"><span class="${state.decError ? 'input-error' : 'input-help'}" id="dec-help">${esc(state.decError || 'Degrees, e.g. -07d14m42.53s or -7.2451')}</span></label>
          </div>
          <div class="search-dates"><div class="card-heading">Enter dates <span class="muted small">(optional)</span></div>
            <div class="field-grid"><label class="field"><span class="field-label">From</span><input class="date-input" type="date" name="from" value="${esc(state.from)}" aria-label="Earliest UTC date"></label>
            <label class="field"><span class="field-label">To</span><input class="date-input" type="date" name="to" value="${esc(state.to)}" aria-label="Latest UTC date"></label></div>
            <p class="input-help">Blank dates search from May 2025 through today.</p>
          </div>
          ${state.dateError ? `<p class="form-error" role="alert">${esc(state.dateError)}</p>` : ''}
          <div class="search-actions"><button type="submit" class="primary-button">✦ &nbsp;Show this patch</button><button type="button" class="pill-button" data-action="saved-real">View saved SPHEREx observations</button></div>
          <details class="explainer"><summary>What are RA and Dec?</summary><p>They are the sky’s longitude and latitude. RA runs from 0 to 24 hours (or 0 to 360°); Dec runs from −90° to +90°.</p></details>
        </form>
      </div>
      <div class="hero-art h-art">${heroArt()}</div>
      <div class="story-scroll-cue" aria-hidden="true"><span>SCROLL</span><i></i></div>
    </section>
    ${homeStory()}
  </main>`;
}
function statusCard(kind) {
  const request = state.request || { ra: state.ra, dec: state.dec };
  if (kind === 'loading') {
    if (request.savedExample) {
      return `<main class="state-main"><div class="state-card" role="status"><h1>Opening saved SPHEREx observations</h1>
        <p>RA ${esc(request.ra)} · Dec ${esc(request.dec)} · Band 4</p>${staticPreview ? '' : '<button type="button" class="pill-button" data-action="cancel">Cancel</button>'}</div></main>`;
    }
    if (request.flipbook) {
      const number = request.flipbook.wantedBand ?? state.band;
      const info = flipbookEntry(request.flipbook, number);
      const waitingForQueue = request.flipbook.queueWaiting.has(number);
      const heading = waitingForQueue ? 'Waiting to start the archive search'
        : info?.status === 'queued' ? 'Archive search queued'
        : number === 'all' ? 'Opening observations across all wavelengths' : `Opening Band ${number} observations`;
      const progress = waitingForQueue ? 'The search queue is full. This search will retry automatically.'
        : info?.status === 'queued' ? 'Your search will start when a worker is free.'
        : info?.candidate_count == null ? 'Finding available SPHEREx exposures…'
        : `Preparing images from ${info.candidate_count} candidate exposures. Playback opens as soon as the first images arrive.`;
      return `<main class="state-main"><div class="state-card" role="status" aria-live="polite">
        <h1>${esc(heading)}</h1>
        <p>RA ${esc(request.ra)} · Dec ${esc(request.dec)}</p>
        <p>${esc(progress)}</p>
        <div class="progress-bar" aria-hidden="true"><span></span></div>
        <button type="button" class="pill-button" data-action="cancel">Cancel</button></div></main>`;
    }
    const progress = state.request?.preparationProgress;
    const rows = progress?.bands ? Object.entries(progress.bands).map(([band, entry]) =>
      `<li>${esc(band)} · ${esc(entry.status === 'ready' || entry.status === 'partial' ? 'real frames ready' :
        entry.status === 'empty' ? 'no usable observations' : entry.status === 'unavailable' ? 'archive unavailable' : entry.status)}</li>`).join('') : '';
    return `<main class="state-main"><div class="state-card" role="status" aria-live="polite"><span class="eyebrow">Preparing the sky</span><h1>Preparing all six infrared bands</h1>
    <p>RA ${esc(request.ra)} · Dec ${esc(request.dec)} · Real observations</p>
    ${progress ? `<p>${esc(progress.completed_count)} of ${esc(progress.total_count)} bands checked · 20′ master images for instant zoom${request.preloadedImages ? ` · ${esc(request.preloadedImages.loaded)} of ${esc(request.preloadedImages.total)} images loaded in this browser` : ''}</p><ol class="loading-steps">${rows}</ol>` : '<ol class="loading-steps"><li>Checking coordinates</li><li>Finding available observations</li><li>Preparing aligned images</li></ol>'}
    <div class="progress-bar" aria-hidden="true"><span></span></div>
    <p class="small">The initial preparation has a time limit. A band is offered only when real images are ready; unavailable bands will be labeled.</p><button type="button" class="pill-button" data-action="cancel">Cancel</button></div></main>`;
  }
  if (kind === 'band-unavailable') {
    const available = [...(state.prepared?.patches || new Map())].sort(([a], [b]) => a - b);
    const status = flipbookEntry(state.prepared, state.band)?.status;
    const name = esc(bandName(state.band));
    const sharedFailure = state.band === 'all' && sharedCatalogFailure(state.prepared);
    const heading = sharedFailure ? 'Archive search unavailable'
      : status === 'empty' ? `No loaded frames for ${name}` : `${name} is unavailable`;
    return `<main class="state-main"><div class="state-card" role="alert"><span class="state-icon">⚠</span><h1>${heading}</h1>
      <p>${esc(state.error?.message || 'No verified images were prepared for this detector.')}</p>
      ${sharedFailure ? '' : state.prepared?.live ? `<div class="actions">${['all', 1, 2, 3, 4, 5, 6].filter(band => band !== state.band).map(band => `<button type="button" class="pill-button" data-action="band" data-band="${band}">Try ${esc(bandName(band))}</button>`).join('')}</div>`
      : available.length ? `<p>These other detector bands have verified real frames ready:</p><div class="actions">${available.map(([band, patch]) => `<button type="button" class="pill-button" data-action="view-prepared-band" data-band="${band}">View Band ${band} · ${patch.frames.length} frame${patch.frames.length === 1 ? '' : 's'}</button>`).join('')}</div>` : '<p>No verified detector frames are ready for this patch yet.</p>'}
      <div class="actions"><button type="button" class="primary-button" data-action="retry-preparation">${sharedFailure ? 'Retry archive search' : state.prepared?.live ? 'Retry requested band' : 'Recheck missing bands'}</button><button type="button" class="pill-button" data-action="home">Change coordinates</button>${state.prepared?.live ? '<button class="pill-button" data-action="saved-real">View saved SPHEREx observations</button>' : ''}</div></div></main>`;
  }
  if (kind === 'empty') {
    const readyBands = [...(state.prepared?.patches || new Map())]
      .filter(([band, patch]) => band !== state.band && patch?.status === 'ok')
      .sort(([a], [b]) => a - b);
    return `<main class="state-main"><div class="state-card" role="status"><span class="state-icon">◎</span><h1>No usable images for this search</h1>
      <p>No usable frames were found in Band ${esc(state.band)} for the requested dates. Try another band, another patch, or a wider date range.</p>
      <p class="small mono">RA ${esc(state.ra)} · Dec ${esc(state.dec)}</p><div class="actions"><button class="primary-button" data-action="home">Change coordinates</button>
      ${state.from || state.to ? '<button class="pill-button" data-action="clear-and-retry">Search all dates</button>' : ''}
      </div>
      ${readyBands.length ? `<div class="empty-band-list" aria-label="Prepared detector bands">${readyBands.map(([band, patch]) => `<button type="button" class="pill-button" data-action="view-prepared-band" data-band="${band}">View ready Band ${band} · ${patch.frames.length} frames</button>`).join('')}</div>` : ''}
      <button type="button" class="pill-button" data-action="retry-preparation">Recheck missing bands</button></div></main>`;
  }
  if (staticPreview) {
    return `<main class="state-main"><div class="state-card" role="alert"><h1>Could not open the saved observations</h1>
      <p>${esc(state.error?.message || 'The saved files could not be loaded.')}</p><button class="primary-button" data-action="saved-real">Try again</button></div></main>`;
  }
  return `<main class="state-main"><div class="state-card" role="alert"><span class="state-icon">⚠</span><h1>Could not load this patch</h1>
    <p>${esc(state.error?.message || 'The data service did not answer. Please try again.')}</p>
    <div class="actions"><button class="primary-button" data-action="retry">Try again</button><button class="pill-button" data-action="home">Change coordinates</button><button class="pill-button" data-action="saved-real">View saved real observations at RA 180°, Dec −30° (5′)</button>${lastRealView ? '<button class="pill-button" data-action="return-real">Return to last real view</button>' : ''}</div>
    <details><summary>Technical details</summary><p>${esc(state.error?.code || 'request_failed')}</p></details></div></main>`;
}
function framePresentation(frame, band, index, count) {
  const detector = state.patch.bands.find(item => item.number === frame.band) || band;
  const number = frame.band ?? detector?.number;
  const dateLabel = displayDate(frame.dateId);
  const time = state.patch.dates.find(date => date.id === frame.dateId)?.timeUtc;
  const ordinal = `Frame ${index + 1} of ${count}${time ? ` · ${time} UTC` : ''}`;
  const wavelength = frame.wavelengthUm == null ? 'Wavelength unavailable'
    : `≈${Number(frame.wavelengthUm).toFixed(2)} µm at target`;
  return {
    dateLabel, ordinal,
    color: bandColor(detector),
    imageAlt: `SPHEREx sky patch image, band ${number}, ${dateLabel}`,
    imageLabel: `SPHEREx observation at ${dateLabel}`,
    wavelength: `${wavelength} · Band ${number}`,
    scrubberLabel: `Frame ${index + 1} of ${count}, ${dateLabel}${time ? `, ${time} UTC` : ''}, Band ${number}`,
  };
}
function imageStage(frame, band, index, frames) {
  const patch = state.patch;
  const presentation = framePresentation(frame, band, index, frames.length);
  // Crop the same raster around its WCS center. Neither the exposure list nor
  // any science pixel, missing-data alpha, or brightness scale changes here.
  const zoom = Math.max(1, (patch.baseFovArcmin || state.fov) / state.fov);
  const pixelScale = patch.pixelScaleArcsec;
  const scaleLength = state.fov >= 12 ? 5 : state.fov >= 6 ? 2 : 1;
  const canPick = lightCurveEnabled();
  const marker = lightCurveMarkerPosition();
  return `<div class="stage-top"><div><span class="frame-date">${esc(presentation.dateLabel)}</span><span class="frame-ordinal">${esc(presentation.ordinal)}</span></div>
    <span class="wavelength-chip" style="--band-color:${presentation.color}"><i class="color-chip"></i><span data-frame-wavelength>${esc(presentation.wavelength)}</span></span></div>
    <div class="image-view" aria-label="${esc(presentation.imageLabel)}">
      <div class="image-layer" style="--zoom:${zoom}"><img data-frame-image src="${esc(frame.previewUrl || frame.imageUrl)}" alt="${esc(presentation.imageAlt)}" ${canPick ? 'data-action="pick-light-curve"' : ''} /></div><div class="crosshair" title="Entered coordinates"></div>
      ${canPick ? `<div class="light-curve-marker-sky" data-light-curve-marker aria-hidden="true" ${marker ? `style="left:${marker.x * 100}%;top:${marker.y * 100}%"` : 'hidden'}></div>` : ''}
      <div class="compass"><span>N</span><span>↑</span><span>E ←</span></div>
      ${pixelScale ? `<div class="scale-bar" style="--scale-width:${(scaleLength / state.fov * 100).toFixed(1)}%">${scaleLength}′</div>` : ''}<div class="fov-label">${state.fov}′ wide</div>
    </div><div class="image-caption"><span>${canPick ? 'Click a star to measure its brightness.' : 'Crosshair marks the entered coordinates'}${pixelScale ? ` · 1 science pixel ≈ ${esc(pixelScale)}″ · display zoom uses nearest-neighbor resampling` : ''}</span><a href="#frame-details-desktop" data-action="show-details">Source and details</a></div>`;
}
function wavelengthComparisonNote(frames) {
  if (frames.length < 2) return '';
  const wavelengths = frames.map(frame => frame.wavelengthUm).filter(value => Number.isFinite(value) && value > 0);
  if (wavelengths.length < 2) return '';
  const first = Math.min(...wavelengths);
  const last = Math.max(...wavelengths);
  if (last - first < Math.max(0.02, first * 0.02)) return '';
  return `<p class="wavelength-caution">These frames span approximately ${first.toFixed(2)}–${last.toFixed(2)} µm at the target. Brightness changes may reflect different wavelengths; compare positions separately.</p>`;
}
function timeline(frames, band) {
  renderedFrames = frames;
  const patch = state.patch;
  const all = allBandFrames();
  const curr = currentFrame();
  const index = Math.max(0, frames.indexOf(curr));
  const scrubberLabel = curr ? framePresentation(curr, band, index, frames.length).scrubberLabel : 'No loaded frames';
  const compact = patch.dates.length > 14;
  const wavelengthScope = band.number === 'all' ? 'across all wavelengths' : `at ${rangeLabel(band)}`;
  const lookup = new Map(all.map(f => [f.dateId, f]));
  const dayCounts = new Map();
  const sampled = Number.isInteger(patch.candidateCount) && Number.isInteger(patch.attemptedCount)
    && patch.candidateCount > patch.attemptedCount
    ? ` · sampled ${patch.attemptedCount} of ${patch.candidateCount} candidate exposures` : '';
  for (const date of patch.dates) {
    const day = date.id.slice(0, 10);
    dayCounts.set(day, (dayCounts.get(day) || 0) + 1);
  }
  let lastMonth = '';
  const dots = compact ? '' : patch.dates.map(date => {
    const month = displayMonth(date.id);
    const showMonth = month !== lastMonth;
    lastMonth = month;
    const frame = lookup.get(date.id);
    const inside = inRange(date.id);
    const frameNote = frame?.flags?.some(flag => !['quality_unchecked', 'partial_sequence'].includes(flag.code));
    const cls = `${!frame ? 'missing' : ''} ${frameNote ? 'flagged' : ''} ${curr?.dateId === date.id ? 'selected' : ''} ${!inside ? 'outside' : ''}`;
    const repeated = dayCounts.get(date.id.slice(0, 10)) > 1;
    return `<button type="button" class="date-dot ${cls}" data-action="date" data-date="${esc(date.id)}" ${!frame || !inside ? 'disabled' : ''} aria-label="${esc(displayDate(date.id))}, ${esc(date.timeUtc)} UTC, ${frame ? `image in band ${frame.band}` : 'no loaded frame'}${inside ? '' : ', outside date filter'}" aria-current="${curr?.dateId === date.id ? 'true' : 'false'}"><i aria-hidden="true"></i>${esc(date.id.slice(8, 10).replace(/^0/, ''))}<span class="month">${repeated ? esc(date.timeUtc) : showMonth ? esc(month) : '&nbsp;'}</span></button>`;
  }).join('');
  return `<section class="timeline-card" aria-label="Observation timeline"><div class="timeline-head"><span class="timeline-title">◷ &nbsp; Time · observation dates</span>
      <div class="timeline-range"><label for="range-from">From</label><input class="date-input" id="range-from" data-range="from" type="date" value="${esc(state.from)}" min="2025-05-01" max="${new Date().toISOString().slice(0, 10)}" aria-label="Earliest UTC date">
      <label for="range-to">to</label><input class="date-input" id="range-to" data-range="to" type="date" value="${esc(state.to)}" min="2025-05-01" max="${new Date().toISOString().slice(0, 10)}" aria-label="Latest UTC date">
      <button type="button" class="pill-button" data-action="clear-range" ${!state.from && !state.to ? 'disabled' : ''}>All dates</button></div></div>
      <div class="timeline-summary ${frames.length ? '' : 'empty'}">${frames.length ? `Showing ${frames.length} of ${all.length} loaded frames ${esc(wavelengthScope)}${sampled}` : `No loaded frames ${esc(wavelengthScope)} in this date range.`}</div>
      ${wavelengthComparisonNote(frames)}
      <div class="timeline-content"><div class="playback"><button type="button" class="soft-button" data-action="prev" ${frames.length < 2 ? 'disabled' : ''} aria-label="Previous frame">Ⅰ◀</button>
      <button type="button" class="primary-button" data-action="play" ${frames.length < 2 ? 'disabled' : ''} aria-label="${state.playing ? 'Pause' : 'Play'} timeline">${state.playing ? '❚❚ Pause' : '▶ Play'}</button>
      <button type="button" class="soft-button" data-action="next" ${frames.length < 2 ? 'disabled' : ''} aria-label="Next frame">▶Ⅰ</button>
      <label class="playback-rate">Speed <select data-range="playback-rate" aria-label="Playback speed" ${frames.length < 2 ? 'disabled' : ''}>
        ${PLAYBACK_RATES.map(rate => `<option value="${rate}" ${rate === state.playbackRate ? 'selected' : ''}>${rate} fps</option>`).join('')}
      </select></label></div>
      <div class="timeline-scrub">
        <input data-range="frame" type="range" min="1" max="${Math.max(1, frames.length)}" value="${index + 1}" step="1"
          aria-label="Observation frame" aria-valuetext="${esc(scrubberLabel)}" ${frames.length < 2 ? 'disabled' : ''} />
        ${compact && frames.length > 1 ? `<div class="timeline-endpoints"><span>${esc(displayDate(frames[0].dateId))}</span><span>${esc(displayDate(frames.at(-1).dateId))}</span></div>` : ''}
      </div></div>
      ${dots ? `<div class="dots-wrap"><div class="date-dots">${dots}</div></div>
      <div class="legend"><span><i></i> observation</span><span><i class="flag"></i> frame note</span></div>` : ''}
    </section>`;
}
function frameNoteCount(frame) {
  return frame?.flags.length ? `${frame.flags.length} note(s)` : 'No notes';
}
function frameDetailsBody(frame, band) {
  if (!frame) return '<p class="notice">No frame in this wavelength and date range.</p>';
  const patch = state.patch;
  const detector = patch.bands.find(item => item.number === frame.band) || band;
  const display = patch.displayByBand?.[frame.band] || patch.display;
  return `<dl><div><dt>Date (UTC)</dt><dd>${esc(displayDate(frame.dateId))} ${esc(patch.dates.find(d => d.id === frame.dateId)?.timeUtc || '')}</dd></div>
      <div><dt>Wavelength range</dt><dd>${esc(rangeLabel(detector))} · Band ${frame.band ?? detector?.number}</dd></div><div><dt>At target (approx.)</dt><dd>${frame.wavelengthUm == null ? 'Not provided' : `${esc(Number(frame.wavelengthUm).toFixed(2))} µm`}</dd></div>
      <div><dt>Field of view</dt><dd>${state.fov}′ wide</dd></div><div><dt>Orientation</dt><dd>${esc(patch.orientation)}</dd></div>
      <div><dt>Data notes</dt><dd>${frame.flags.length ? frame.flags.map(f =>
        f.code === 'partial_coverage' && state.fov < (patch.baseFovArcmin || state.fov)
          ? `Loaded ${patch.baseFovArcmin}′ master coverage: ${Math.round((frame.coverageFraction || 0) * 100)}%; cropped viewport coverage may differ.`
          : esc(f.label)).join('; ') : 'None reported'}</dd></div>
      <div><dt>Product ID</dt><dd class="mono">${esc(frame.productId || 'Not provided')}</dd></div>
        <div><dt>Observed at (UTC)</dt><dd>${esc(frame.observedAt)}</dd></div>
        <div><dt>Detector</dt><dd>${esc(frame.detector)}</dd></div>
        <div><dt>Exact wavelength</dt><dd>${frame.wavelengthUm == null ? 'Not provided' : `${esc(frame.wavelengthUm)} µm at target`}</dd></div>
        <div><dt>Displayed pixels</dt><dd>${esc(displayedDimensions(patch, state.fov).width)} × ${esc(displayedDimensions(patch, state.fov).height)} aligned science pixels</dd></div>
        <div><dt>Sky footprint</dt><dd class="mono">${(state.fov === patch.baseFovArcmin && patch.grid?.skyFootprint || skyFootprint(patch.target, state.fov)).map(p => `${esc(p.ra_deg)}°, ${esc(p.dec_deg)}°`).join(' · ')}</dd></div>
        <div><dt>Brightness scale</dt><dd>${display ? `${esc(display.stretch)}; ${esc(display.vmin)} to ${esc(display.vmax)} ${esc(display.brightness_unit || '')}` : 'Not provided'}</dd></div>
        <div><dt>Pipeline</dt><dd>${esc(frame.pipelineVersion || 'Not provided')}</dd></div>${patch.releaseDate ? `<div><dt>Release date</dt><dd>${esc(patch.releaseDate)}</dd></div>` : ''}<div><dt>Archive</dt><dd>${frame.archiveUrl ? `<a target="_blank" rel="noopener noreferrer" href="${esc(frame.archiveUrl)}">Open IRSA cutout ↗</a>` : 'Not provided'}</dd></div></dl>${patch.warnings?.length ? `<p class="notice">${patch.warnings.map(esc).join(' ')}</p>` : ''}`;
}
function settingsPanel(section = 'all', scope = 'desktop') {
  const patch = state.patch;
  const band = selectedBand();
  const frame = currentFrame();
  const maxFov = Math.min(20, patch.fovMaxArcmin || 20);
  const datesInBand = n => patch.frames.filter(f => f.band === n && inRange(f.dateId)).length;
  const bandOptions = state.prepared?.live ? [ALL_WAVELENGTHS, ...patch.bands] : patch.bands;
  const bands = bandOptions.map(b => {
    const prepared = state.prepared?.patches.get(b.number);
    const entry = flipbookEntry(state.prepared, b.number);
    const count = prepared ? prepared.frames.filter(frame => inRange(frame.dateId)).length : datesInBand(b.number);
    const progressive = state.prepared?.live;
    const available = b.number === state.band || !!prepared || !!progressive;
    const label = state.prepared?.queueWaiting?.has(b.number) ? 'Waiting…' :
      progressive && !prepared ? entry?.status === 'empty' ? 'No frames' :
      entry?.status === 'queued' ? 'Queued' :
      ACTIVE_BAND_STATUSES.has(entry?.status) ? 'Loading…' :
      entry?.status === 'unavailable' ? 'Retry' : 'Load'
      : !available ? entry?.status === 'empty' ? 'No frames' :
      entry?.status === 'queued' || entry?.status === 'running' ? 'Not ready' : 'Unavailable'
      : `${count} frame${count === 1 ? '' : 's'}`;
    return `<button type="button" class="band-option ${state.band === b.number ? 'active' : ''}" style="--band-color:${bandColor(b)}" data-action="band" data-band="${b.number}" aria-pressed="${state.band === b.number}" ${available ? '' : 'disabled'}><i class="color-chip" aria-hidden="true"></i> <strong>${esc(rangeLabel(b))}</strong><small>${esc(bandName(b.number))}</small><span class="band-count">${label}</span></button>`;
  }).join('');
  const fovs = [20,10,5].map(n => `<button class="pill-button ${state.fov === n && !state.customFov ? 'active' : ''}" type="button" data-action="fov" data-fov="${n}" aria-pressed="${state.fov === n && !state.customFov}" ${n > maxFov ? 'disabled' : ''}>${{20:'Wide',10:'Medium',5:'Close'}[n]} ${n}′</button>`).join('');
  const detail = `<details class="about" id="frame-details-${scope}" ${state.aboutOpen || section === 'details' ? 'open' : ''}><summary>About this frame <span data-frame-notes>${frameNoteCount(frame)}</span></summary>
    <div data-frame-details>${frameDetailsBody(frame, band)}</div></details>`;
  const wavelengths = `<section class="settings-section"><h2>Wavelength</h2><p>SPHEREx observes infrared light in six detector bands. Choose a range to see its available frames. Different bands may have different dates and wavelengths within the range.</p>
      <div class="spectrum-ruler" aria-hidden="true">${['#7aa7ff','#4fc3d9','#6dd08a','#e2c54a','#f2994a','#f0776c'].map((color,i) => `<i style="--band-color:${color}" class="${patch.bands.some(b=>b.number===i+1) ? 'present' : ''}"></i>`).join('')}</div>
      <div class="spectrum-ends"><span>0.744 µm</span><span>5.002 µm</span></div><div class="band-list">${bands}</div>
      <p class="band-footnote">${state.band === 'all' ? 'All wavelengths shows individual exposures from the six detectors in time order. Each detector has its own fixed brightness scale.' : state.prepared?.live ? 'Select a band to load its real exposures. Ready bands switch immediately.' : state.prepared ? 'Verified bands were prepared with 20′ master images. Switching prepared bands and zooming within a loaded image use the images already loaded.' : 'This saved example has Band 4 images.'} The wavelength at the target can change from frame to frame.</p>
      ${state.prepared && !state.prepared.live && Object.values(state.prepared.bands).some(entry => !['ready', 'partial'].includes(entry.status)) ? '<button class="pill-button" type="button" data-action="retry-preparation">Recheck missing bands</button>' : ''}</section>`;
  const field = `<section class="settings-section"><h2>Field of view</h2><div class="fov-grid">${fovs}<button class="pill-button ${state.customFov ? 'active' : ''}" type="button" data-action="fov-custom" aria-pressed="${state.customFov}">Custom</button></div>
      ${state.customFov ? `<div class="custom-fov"><label for="custom-width-${scope}">Width <output>${state.fov}′</output></label><input id="custom-width-${scope}" data-range="fov" type="range" min="1" max="${Math.floor(maxFov)}" value="${state.fov}" step="1" /></div>` : ''}
      <p class="band-footnote">Zoom keeps these same observations and their brightness scale. ${patch.bundledRealExample || patch.fovMaxArcmin < 20 ? `This saved view covers up to ${patch.fovMaxArcmin}′; wider pixels are unavailable.` : 'The 20′ master image was prepared before this viewer opened.'}</p>
      ${state.zoomRequest ? `<p class="band-footnote" role="status">Preparing ${state.zoomRequest.fov}′ from the same exposures…</p>` : ''}
      ${state.zoomError ? `<p class="form-error" role="alert">${esc(state.zoomError)}</p>` : ''}</section>`;
  return `<aside class="settings" aria-label="Settings and details"><div class="panel-eyebrow">${scope === 'desktop' ? 'Settings and details' : ''}</div>
    ${section === 'details' ? '' : wavelengths}${section === 'bands' ? '' : field + detail}</aside>`;
}
function recoveryBanner() {
  if (!state.recovery) return '';
  const attempted = state.recovery.attempted;
  const switchedBand = attempted.band !== state.band;
  const heading = switchedBand
    ? `${bandName(attempted.band)} did not load · still showing ${bandName(state.band)}`
    : `Latest search did not load · still showing ${bandName(state.band)}`;
  return `<div class="recovery-banner" role="alert"><div><strong>${esc(heading)}</strong>
    <p>${esc(state.recovery.error.message)} The attempted search used ${esc(bandName(attempted.band))} at ${esc(attempted.fov)}′. The visible frames are from your previous search at ${esc(state.fov)}′.</p></div>
    <button type="button" class="pill-button" data-action="retry-failed-query">${switchedBand ? `Retry ${esc(bandName(attempted.band))}` : 'Retry search'}</button></div>`;
}
function bandName(number) {
  return number === 'all' ? 'All wavelengths' : `Band ${number}`;
}

function flipbookEntry(group, number) {
  if (number !== 'all') return group?.bands?.[`D${number}`];
  if (!group?.allRequested) return { status: group?.allError ? 'unavailable' : 'idle', error: group?.allError };
  const entries = Object.values(group.bands);
  const reported = entries.filter(entry => Number.isFinite(entry.candidate_count));
  const sum = field => entries.reduce((total, entry) => total + (entry[field] || 0), 0);
  const active = group.jobStatus === 'running' || entries.some(entry => ACTIVE_BAND_STATUSES.has(entry.status));
  const queued = entries.some(entry => entry.status === 'queued') && !entries.some(entry => entry.status === 'running');
  const hasFrames = entries.some(entry => entry.manifest?.frames?.length);
  const incomplete = entries.some(entry => entry.status === 'unavailable' || entry.status === 'partial');
  const firstBandError = !hasFrames && entries.find(entry =>
    entry.status === 'unavailable' && typeof entry.error === 'string' && entry.error.trim())?.error;
  return {
    status: queued ? 'queued' : active ? 'running' : group.allError ? 'unavailable' : incomplete ? hasFrames ? 'partial' : 'unavailable'
      : hasFrames ? 'ready' : 'empty',
    candidate_count: reported.length ? sum('candidate_count') : null,
    attempted_count: sum('attempted_count'),
    error: group.allError || firstBandError,
  };
}

function sharedCatalogFailure(group) {
  if (!group?.live || !group.allRequested) return false;
  const entries = Object.values(group.bands);
  return entries.length === 6 && entries.every(entry =>
    entry.status === 'unavailable' && !entry.manifest && !entry.candidate_count &&
    typeof entry.error === 'string' && /IRSA|archive|image search|catalog/i.test(entry.error)) &&
    group.records.size === 0 && group.failedPreviews.size === 0;
}

function unreadyFlipbookPreviews(group, number, type = 'pending') {
  const records = number === 'all' ? [...group.records.values()] : [group.records.get(number)];
  return records.flatMap(record => record?.patch.frames || []).filter(frame =>
    !group.previews.has(frame.imageUrl) && (type === 'failed'
      ? group.failedPreviews.has(frame.imageUrl) : !group.failedPreviews.has(frame.imageUrl)));
}

function retryFailedFlipbookPreviews(group, number) {
  const failed = unreadyFlipbookPreviews(group, number, 'failed');
  for (const frame of failed) {
    group.failedPreviews.delete(frame.imageUrl);
    group.previewErrors.delete(frame.imageUrl);
  }
  if (failed.length) pumpFlipbookPreviews(group);
  return failed.length;
}

function firstPreviewError(group, number) {
  const failed = unreadyFlipbookPreviews(group, number, 'failed');
  return failed.map(frame => group.previewErrors.get(frame.imageUrl)).find(Boolean) || '';
}

function flipbookNotice() {
  const group = state.prepared;
  if (!group?.live) return '';
  if (group.bandError) return `<p class="notice" role="alert" data-flipbook-progress>${esc(group.bandError)}</p>`;
  const number = group.wantedBand ?? state.band;
  const entry = flipbookEntry(group, number);
  const loadingPreviews = unreadyFlipbookPreviews(group, number).length;
  if (group.queueWaiting.has(number)) {
    const currentBand = number !== state.band ? ` · ${bandName(state.band)} is still selected` : '';
    return `<p class="notice" role="status" data-flipbook-progress>${esc(`Waiting to start ${bandName(number)}. The search will retry automatically.${currentBand}`)}</p>`;
  }
  if (!group.scheduling.has(number) && !ACTIVE_BAND_STATUSES.has(entry?.status) && !loadingPreviews) {
    if (group.pinnedBand === number) {
      return '<div class="saved-real-banner"><div><strong>Shared observation sequence</strong><p>Search this patch to build a longer sequence from the archive.</p></div><button class="pill-button" type="button" data-action="expand-flipbook">Load more observations</button></div>';
    }
    const failures = unreadyFlipbookPreviews(group, number, 'failed').length;
    if (failures) {
      const reason = firstPreviewError(group, number);
      return `<p class="notice" role="status" data-flipbook-progress>${esc(failures)} image previews could not load in this browser.${reason ? ` First error: ${esc(reason)}` : ''}</p>`;
    }
    return '';
  }
  let message = entry?.status === 'queued' ? `${bandName(number)} queued and waiting for a worker`
    : number === 'all' ? 'Loading exposures across all wavelengths' : `Loading Band ${number} exposures`;
  if (number !== state.band) message += ` · ${bandName(state.band)} is still selected`;
  else if (entry?.status !== 'queued' && entry?.candidate_count != null) {
    message += ` · ${group.patches.get(number)?.frames.length || 0} ready · ${entry.attempted_count || 0} of ${entry.candidate_count} candidates checked`;
  }
  return `<p class="notice" role="status" data-flipbook-progress>${esc(message)}</p>`;
}
function lightCurveEnabled() {
  return Boolean(activeFlipbook?.jobId && state.prepared === activeFlipbook && currentFrame());
}
function lightCurveMarkerPosition() {
  const target = state.lightCurveTarget;
  return target && skyToImagePosition(state.patch.target, state.fov, target);
}
function lightCurveMarkup() {
  return lightCurveContents(state.lightCurve, {
    enabled: lightCurveEnabled(),
    currentProduct: currentFrame()?.productId,
    target: state.lightCurveTarget,
    esc,
  });
}
function lightCurveFocus(focused) {
  const { range, action } = focused?.dataset || {};
  if (['light-curve-channel', 'light-curve-exposure'].includes(range)) return `[data-range="${range}"]`;
  if (['measure-light-curve', 'close-light-curve', 'measure-crosshair'].includes(action)) return `[data-action="${action}"]`;
  return null;
}
function paintLightCurve() {
  const panel = app.querySelector('[data-light-curve]');
  if (!panel) { render(); return; }
  const focus = lightCurveFocus(document.activeElement);
  const openDetails = ['.light-curve-exclusions', '.light-curve-spectrum'].filter(selector => panel.querySelector(selector)?.open);
  panel.innerHTML = lightCurveMarkup();
  for (const selector of openDetails) {
    const details = panel.querySelector(selector);
    if (details) details.open = true;
  }
  const marker = app.querySelector('[data-light-curve-marker]');
  if (marker) {
    const position = lightCurveMarkerPosition();
    marker.hidden = !position;
    if (position) {
      marker.style.left = `${position.x * 100}%`;
      marker.style.top = `${position.y * 100}%`;
    }
  }
  if (focus) (panel.querySelector(focus) || panel.querySelector('button'))?.focus({ preventScroll: true });
}
function cancelLightCurve() {
  state.lightCurve?.controller.abort();
  state.lightCurve = null;
}
async function measureLightCurve({ replace = false } = {}) {
  const group = activeFlipbook;
  if (!lightCurveEnabled() || state.view !== 'viewer' || !replace && state.lightCurve?.measuring) return;
  const curve = {
    controller: new AbortController(), measuring: true,
    channel: replace ? null : state.lightCurve?.channel,
    product: replace ? null : state.lightCurve?.product,
  };
  cancelLightCurve();
  state.lightCurve = curve;
  paintLightCurve();
  const { signal } = curve.controller;
  try {
    let payload = await startLightCurve(group.jobId, {
      from: state.from, to: state.to, target: state.lightCurveTarget, signal,
      onBusy: () => {
        if (state.lightCurve !== curve || curve.waiting) return;
        curve.waiting = true;
        paintLightCurve();
      },
    });
    while (state.lightCurve === curve && !signal.aborted) {
      const changed = curve.payload?.processed_count !== payload.processed_count || curve.payload?.status !== payload.status;
      curve.payload = payload;
      curve.measuring = payload.status !== 'complete';
      if (changed) paintLightCurve();
      if (!curve.measuring) return;
      await waitForFlipbookUpdate(signal);
      payload = await readLightCurve(payload.light_curve_id, signal);
    }
  } catch (error) {
    if (state.lightCurve !== curve || signal.aborted) return;
    curve.measuring = false;
    curve.error = error.message || 'The light curve service is temporarily unavailable.';
    paintLightCurve();
  }
}
function pickLightCurveTarget(image, event) {
  if (!lightCurveEnabled()) return;
  const view = image.closest('.image-view');
  if (!view?.clientWidth || !view.clientHeight) return;
  const rect = view.getBoundingClientRect();
  const x = (event.clientX - rect.left - view.clientLeft) / view.clientWidth;
  const y = (event.clientY - rect.top - view.clientTop) / view.clientHeight;
  if (!(x >= 0 && x <= 1 && y >= 0 && y <= 1)) return;
  state.lightCurveTarget = imagePositionToSky(state.patch.target, state.fov, x, y);
  stopPlayback();
  updatePlaybackButton();
  void measureLightCurve({ replace: true });
}
function openLightCurveExposure(product, fromPreviewUpdate = false) {
  const curve = state.lightCurve;
  const group = activeFlipbook;
  const point = curve?.payload?.points.find(item => item.product_id === product);
  if (!point || !group || state.prepared !== group) return;
  curve.channel = lightCurveChannelKey(point);
  curve.product = product;
  curve.pendingProduct = null;
  curve.frameNotice = null;

  const frameMatches = frame => frame.productId === product && inRange(frame.dateId);
  let index = renderedFrames.findIndex(frameMatches);
  if (index < 0) {
    const destination = ['all', Number(point.detector.slice(1))].find(band =>
      group.patches.get(band)?.frames.some(frameMatches));
    if (destination != null) {
      void chooseLiveBand(group, destination);
      index = renderedFrames.findIndex(frameMatches);
    }
  }
  if (index >= 0) {
    stopPlayback();
    selectFrame(index);
  } else {
    const preview = group.records.get(Number(point.detector.slice(1)))?.patch.frames.find(frameMatches);
    const pending = preview && unreadyFlipbookPreviews(group, preview.band).includes(preview);
    if (pending || !preview && group.jobStatus === 'running') {
      curve.pendingProduct = product;
      curve.frameNotice = 'Waiting for this exposure’s preview.';
    } else {
      curve.frameNotice = 'The exact exposure preview is unavailable in this view.';
    }
    if (fromPreviewUpdate && curve.pendingProduct) return;
  }
  paintLightCurve();
}
function viewer() {
  const patch = state.patch;
  const frames = visibleFrames();
  const band = selectedBand();
  const frame = currentFrame();
  const namedTarget = TEST_TARGETS.find(target => Number(target.ra) === patch.target.raDeg &&
    Number(target.dec) === patch.target.decDeg);
  const stillBuilding = state.prepared?.live && ACTIVE_BAND_STATUSES.has(flipbookEntry(state.prepared, state.band)?.status);
  return `<main class="viewer-layout"><div class="viewer-main"><div class="stage">
    <div class="provenance"><span class="status-badge ${patch.bundledRealExample ? 'saved-real' : 'real'}" data-real-badge>${patch.bundledRealExample ? 'Saved real SPHEREx observations' : frame ? 'Loading real image…' : 'Real SPHEREx observations'}</span>
    <span class="target-name">${esc(namedTarget?.name || patch.target.name || 'Your patch')} · <span class="mono muted">RA ${esc(state.ra)} · Dec ${esc(state.dec)}</span></span>
    ${patch.backendStatus === 'partial' && !stillBuilding ? `<span class="partial-note" role="status">${patch.zoomMissingOuterCount
      ? `Outer sky pixels unavailable in ${patch.zoomMissingOuterCount} selected frame${patch.zoomMissingOuterCount === 1 ? '' : 's'}; dates and products unchanged`
      : patch.failureCount ? `Partial sequence · ${patch.failureCount} exposure${patch.failureCount === 1 ? '' : 's'} could not be used` : 'Partial date coverage · the archive search was incomplete'}</span>` : ''}</div>
    ${patch.bundledRealExample ? `<div class="saved-real-banner" role="status"><div><strong>Saved real observations · RA 180°, Dec −30° · Band 4 · 5′</strong><p>${staticPreview ? 'This preview plays a saved sequence. Use the full viewer to search other sky coordinates.' : `${state.sampleFallback ? `${esc(state.sampleFallback.message)} ` : ''}These SPHEREx frames were prepared earlier. The saved set may omit newly released observations.`}</p></div>${staticPreview ? '' : '<button type="button" class="pill-button" data-action="retry">Search the archive</button>'}</div>` : ''}
    ${patch.backendStatus === 'partial' && !patch.bundledRealExample && !stillBuilding ? `<div class="saved-real-banner" role="status"><div><strong>${patch.zoomMissingOuterCount ? 'Incomplete outer sky coverage' : patch.cachedPartial ? 'Cached partial real observations' : 'Partial real observations'}</strong><p>${patch.zoomMissingOuterCount
      ? `The ${patch.zoomMissingOuterCount} affected frames retain the same original products, dates, and central pixels; unavailable outer pixels remain transparent.`
      : `${patch.cachedPartial ? 'These actual SPHEREx frames were reused from a recent search. ' : ''}Some observations or dates may be missing. Refresh to check the archive again.`}</p></div><button type="button" class="pill-button" data-action="${patch.zoomMissingOuterCount ? 'retry-same-parent' : 'refresh-observations'}">${patch.zoomMissingOuterCount ? 'Retry same-parent pixels' : 'Refresh observations'}</button></div>` : ''}
    ${recoveryBanner()}
    ${flipbookNotice()}
    ${frame ? imageStage(frame, band, frames.indexOf(frame), frames) : `<div class="state-card" role="status"><h2>No loaded frames in this range</h2>${staticPreview ? '' : '<p>The archive may have other observations.</p><button class="pill-button" data-action="search-date-range">Search this date range</button>'}<button class="pill-button" data-action="clear-range">All dates</button></div>`}
    ${timeline(frames, band)}
    <div class="light-curve-slot" data-light-curve>${lightCurveMarkup()}</div>
    <div class="mobile-actions"><button class="soft-button" type="button" data-action="open-bands">Wavelength · ${esc(rangeLabel(band))}</button><button class="soft-button" type="button" data-action="open-details">Details &amp; width</button></div>
    </div></div>${settingsPanel()}</main>${state.drawer ? `<dialog class="mobile-drawer" aria-label="${state.drawer === 'bands' ? 'Wavelength settings' : 'Frame details and field of view'}"><div class="drawer-head"><h2>${state.drawer === 'bands' ? 'Wavelength' : 'Details'}</h2><button class="soft-button" type="button" data-action="close-drawer" aria-label="Close panel">Close ✕</button></div>${settingsPanel(state.drawer, 'mobile')}</dialog>` : ''}`;
}
function render() {
  const focused = document.activeElement;
  const range = focused?.dataset?.range;
  const action = focused?.dataset?.action;
  const playbackFocus = ['frame', 'playback-rate'].includes(range) ? `[data-range="${range}"]`
    : ['prev', 'play', 'next'].includes(action) ? `[data-action="${action}"]` : null;
  if (state.view !== 'viewer') renderedFrames = [];
  const contents = state.view === 'home' ? home() : state.view === 'viewer' ? viewer() : statusCard(state.view);
  app.innerHTML = `<div class="app-shell ${state.view === 'home' ? 'home-shell' : ''}">${header()}${contents}${footer()}</div>`;
  const focus = playbackFocus || lightCurveFocus(focused);
  if (focus) app.querySelector(focus)?.focus({ preventScroll: true });
  if (state.drawer) {
    const dialog = app.querySelector('.mobile-drawer');
    if (dialog) { dialog.showModal(); dialog.querySelector('[data-action="close-drawer"]')?.focus(); }
  }
}
function readForm(form, preset) {
  const data = new FormData(form);
  const ra = preset?.ra ?? String(data.get('ra') || '').trim();
  const dec = preset?.dec ?? String(data.get('dec') || '').trim();
  const previousQuery = [state.ra, state.dec, state.from, state.to].join('|');
  const headerForm = form.id === 'header-form';
  const previousCoords = parseCoordinates(state.ra, state.dec);
  if (headerForm) { state.headerRa = ra; state.headerDec = dec; }
  else { state.ra = ra; state.dec = dec; }
  if (form.id === 'home-form') { state.from = String(data.get('from') || ''); state.to = String(data.get('to') || ''); }
  if (state.from && state.to && state.from > state.to) [state.from,state.to] = [state.to,state.from];
  const coords = parseCoordinates(ra, dec);
  if (headerForm && coords.isValid && (!previousCoords.isValid ||
      Math.abs(coords.raDeg - previousCoords.raDeg) > 1e-8 ||
      Math.abs(coords.decDeg - previousCoords.decDeg) > 1e-8)) {
    state.from = '';
    state.to = '';
    if (liveFlipbook) state.band = 'all';
  }
  state.raError = coords.raError;
  state.decError = coords.decError;
  state.dateError = (state.from && !validDate(state.from) || state.to && !validDate(state.to)) ? 'Enter valid dates.' : '';
  if (!coords.isValid || state.dateError) { render(); (state.raError ? app.querySelector('[name="ra"]') : state.decError ? app.querySelector('[name="dec"]') : null)?.focus(); return; }
  state.ra = ra; state.dec = dec; state.headerRa = null; state.headerDec = null;
  if (preset && liveFlipbook) state.band = 'all';
  const sameQuery = [state.ra, state.dec, state.from, state.to].join('|') === previousQuery;
  if (!sameQuery) state.activeDateId = '';
  if (sameQuery && state.view === 'viewer' && !state.recovery &&
      state.prepared?.pinnedBand !== state.band) { syncUrl(); render(); return; }
  loadPatch();
}
async function showSavedRealExample({ restoreView = false } = {}) {
  stopFlipbook();
  cancelZoom();
  state.request?.controller.abort();
  const controller = new AbortController();
  const sequence = ++loadSequence;
  stopPlayback();
  state.drawer = '';
  state.ra = '180'; state.dec = '-30'; state.band = 4;
  state.fov = restoreView ? Math.min(state.fov, DEFAULT_FOV) : DEFAULT_FOV;
  state.customFov = state.fov !== DEFAULT_FOV;
  if (!restoreView) {
    state.from = staticPreview ? '' : '2025-05-01';
    state.to = staticPreview ? '' : '2026-11-15';
    state.activeDateId = '';
  }
  state.linkedSequenceId = null;
  state.prepared = null;
  state.error = null; state.recovery = null; state.sampleFallback = null;
  state.request = { controller, ra: state.ra, dec: state.dec, savedExample: true };
  state.view = 'loading';
  syncUrl(); render();
  try {
    const patch = await loadBundledRealPatch({ signal: controller.signal });
    if (sequence !== loadSequence) return;
    state.patch = patch;
    state.view = 'viewer';
    settleFrame();
    lastRealView = realViewSnapshot();
  } catch (error) {
    if (sequence !== loadSequence || error?.name === 'AbortError') return;
    state.patch = null;
    state.error = { code: String(error.code || 'example_unavailable'), message: String(error.message || 'Could not open the saved real observations.') };
    state.view = 'error';
  } finally {
    if (sequence === loadSequence) { state.request = null; prunePreparedImages(); syncUrl(); render(); }
  }
}
async function preloadPreparedImage(url, signal, refresh = false) {
  if (!refresh && preparedImageCache.has(url)) return preparedImageCache.get(url).blobUrl;
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) abort();
  const timeout = setTimeout(() => controller.abort(), 20000);
  let blobUrl = null;
  try {
    // The proxy can use Cache-Control: no-store. A decoded HTTP <img> alone
    // does not guarantee the next band click is local; a Blob URL does.
    const response = await fetch(url, {
      signal: controller.signal, cache: refresh ? 'reload' : 'default',
    });
    if (!response.ok) throw new Error(`Image preview returned HTTP ${response.status}.`);
    blobUrl = URL.createObjectURL(await response.blob());
    const image = new Image();
    image.src = blobUrl;
    await image.decode();
    if (controller.signal.aborted) throw new DOMException('Image preview cancelled.', 'AbortError');
    if (refresh) return { blobUrl, image };
    preparedImageCache.set(url, { blobUrl, image });
    return blobUrl;
  } catch (error) {
    if (blobUrl) URL.revokeObjectURL(blobUrl);
    throw error;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener('abort', abort);
  }
}
async function preloadPreparedPatches(patches, bands, signal, request, refresh = false) {
  if (typeof Image !== 'function' || typeof URL.createObjectURL !== 'function') return; // DOM-free tests.
  const total = [...patches.values()].reduce((count, patch) => count + patch.frames.length, 0);
  request.preloadedImages = { loaded: 0, total };
  const results = await Promise.all([...patches].map(async ([band, patch]) => {
    const loaded = await Promise.allSettled(patch.frames.map(frame =>
      preloadPreparedImage(frame.imageUrl, signal, refresh)));
    const failed = loaded.find(result => result.status === 'rejected');
    if (refresh && failed) {
      for (const result of loaded) {
        if (result.status === 'fulfilled') URL.revokeObjectURL(result.value.blobUrl);
      }
    } else {
      loaded.forEach((result, index) => {
        if (result.status !== 'fulfilled') return;
        if (refresh) {
          const url = patch.frames[index].imageUrl;
          const previous = preparedImageCache.get(url);
          preparedImageCache.set(url, result.value);
          if (previous) retiredPreviewUrls.add(previous.blobUrl);
        }
        patch.frames[index].previewUrl = refresh ? result.value.blobUrl : result.value;
      });
    }
    request.preloadedImages.loaded += loaded.filter(result => result.status === 'fulfilled').length;
    if (state.request === request) render();
    return { band, failed };
  }));
  if (signal.aborted) throw new DOMException('Preparation cancelled.', 'AbortError');
  for (const { band, failed } of results) {
    if (!failed) continue;
    patches.delete(band);
    bands[`D${band}`] = { status: 'unavailable',
      error: `Band ${band} has genuine observations, but one or more image previews did not load in this browser. Recheck the missing bands.` };
  }
}
async function preparePatchSet(coords, restoreSequenceId, signal, request, forceRefresh) {
  const key = preparationKey(coords, state.from, state.to);
  const existing = state.prepared;
  if (!forceRefresh && existing?.key === key &&
      (!restoreSequenceId || existing.patches.get(state.band)?.sequenceId === restoreSequenceId)) {
    return existing;
  }
  // A shared link identifies an exact selected sequence. Restore its original
  // parent products at the canonical 20′ width before preparing other bands.
  // The batch may find newer products; it cannot replace this linked band.
  let linked = null;
  if (restoreSequenceId) {
    try {
      linked = await restoreRealSequence({ sequenceId: restoreSequenceId,
        ra: state.ra, dec: state.dec, band: state.band, fov: 20, signal });
    } catch (error) {
      if (error?.name === 'AbortError' || state.fov >= 20) throw error;
      // A saved original width can still be shown honestly if the SAME parent
      // FITS cannot supply an expanded field right now. Disable wider zoom.
      linked = await restoreRealSequence({ sequenceId: restoreSequenceId,
        ra: state.ra, dec: state.dec, band: state.band, fov: state.fov, signal });
      linked.fovMaxArcmin = state.fov;
      linked.warnings.push(`Wider pixels could not be fetched from these same parent exposures: ${error.message}`);
    }
  }
  let prepared;
  try {
    prepared = await prepareRealBands({
      ra: state.ra, dec: state.dec, band: state.band, from: state.from, to: state.to,
      refresh: forceRefresh, signal, onProgress(progress) {
        if (state.request !== request) return;
        request.preparationProgress = progress;
        render();
      },
    });
  } catch (error) {
    if (!linked || error?.name === 'AbortError') throw error;
    const bands = Object.fromEntries(Array.from({ length: 6 }, (_, index) =>
      [`D${index + 1}`, { status: 'unavailable', error: error.message }]));
    bands[`D${state.band}`] = { status: linked.backendStatus === 'partial' ? 'partial' : 'ready',
      restored_from_link: true };
    linked.warnings.push(`Other bands could not be prepared: ${error.message}`);
    return { key, patches: new Map([[state.band, linked]]), bands,
      timedOut: false, requestedBand: state.band, jobId: null };
  }
  const patches = new Map();
  const bands = { ...prepared.payload.bands };
  for (let n = 1; n <= 6; n++) {
    const entry = bands[`D${n}`];
    try {
      const patch = preparedBandPatch(entry, { target: prepared.target,
        band: n, queryRange: prepared.queryRange });
      if (patch) patches.set(n, patch);
    } catch (error) {
      bands[`D${n}`] = { status: 'unavailable', error: `Band ${n} returned invalid observation metadata: ${error.message}` };
    }
  }
  if (linked) {
    patches.set(state.band, linked);
    bands[`D${state.band}`] = { status: linked.backendStatus === 'partial' ? 'partial' : 'ready',
      manifest: null, restored_from_link: true };
  }
  await preloadPreparedPatches(patches, bands, signal, request);
  return { key, patches, bands, timedOut: prepared.timedOut,
    requestedBand: state.band, jobId: prepared.payload.job_id };
}

function stopFlipbook() {
  cancelLightCurve();
  state.lightCurveTarget = null;
  const group = activeFlipbook;
  if (!group) return;
  activeFlipbook = null;
  clearTimeout(group.paintTimer);
  group.controller.abort();
}

function scheduleFlipbookPaint(group) {
  if (activeFlipbook !== group || group.paintTimer != null) return;
  group.paintTimer = setTimeout(() => {
    group.paintTimer = null;
    if (activeFlipbook === group) presentFlipbook(group);
  }, state.view === 'loading' ? 0 : 160);
}

function pumpFlipbookPreviews(group) {
  if (activeFlipbook !== group) return;
  const order = [...new Set([group.wantedBand, state.band, ...group.records.keys()])];
  for (const number of order) {
    const record = group.records.get(number);
    if (!record) continue;
    const { frames } = record.patch;
    const preferred = !group.opened && frames.find(frame => frame.dateId === state.activeDateId);
    for (const frame of preferred ? [preferred, ...frames] : frames) {
      if (group.pendingPreviews.size >= FLIPBOOK_IMAGE_WORKERS) return;
      const url = frame.imageUrl;
      if (group.previews.has(url) || group.pendingPreviews.has(url) || group.failedPreviews.has(url)) continue;
      if (typeof Image !== 'function' || typeof URL.createObjectURL !== 'function') {
        group.previews.set(url, url);
        continue;
      }
      group.pendingPreviews.add(url);
      preloadPreparedImage(url, group.controller.signal).then(preview => {
        if (activeFlipbook === group) group.previews.set(url, preview);
      }).catch(error => {
        if (activeFlipbook === group && error?.name !== 'AbortError') {
          group.failedPreviews.add(url);
          group.previewErrors.set(url, String(error?.message || 'Image request failed.'));
        }
        // The per-image deadline can also abort a preview. It must be allowed
        // to finish as a failed preview while the wider search is still active.
        if (activeFlipbook === group && !group.controller.signal.aborted && error?.name === 'AbortError') {
          group.failedPreviews.add(url);
          group.previewErrors.set(url, 'Image preview timed out after 20 seconds.');
        }
      }).finally(() => {
        group.pendingPreviews.delete(url);
        pumpFlipbookPreviews(group);
        scheduleFlipbookPaint(group);
      });
    }
  }
}

function considerFlipbookSnapshot(group, payload) {
  if (activeFlipbook !== group || payload.job_id !== group.jobId ||
      Number.isFinite(payload.revision) && payload.revision <= group.revision) return;
  if (Number.isFinite(payload.revision)) group.revision = payload.revision;
  group.jobStatus = payload.status;
  group.bands = { ...payload.bands };
  if (group.pinnedBand != null) group.bands[`D${group.pinnedBand}`] = { status: 'ready' };
  for (const [name, entry] of Object.entries(group.bands)) {
    const number = Number(name.slice(1));
    if (!entry.manifest) continue;
    const key = [entry.manifest.sequence_id, entry.manifest.frames?.length, entry.status].join('|');
    if (group.records.get(number)?.key === key) continue;
    try {
      const patch = flipbookBandPatch(entry, { target: group.target, band: number, queryRange: group.queryRange });
      if (patch) group.records.set(number, { key, patch });
    } catch (error) {
      group.bands[name] = { ...entry, status: 'unavailable', error: error.message };
    }
  }
  pumpFlipbookPreviews(group);
  scheduleFlipbookPaint(group);
}

function mixedFlipbookPatch(group) {
  if (!group.allRequested) return null;
  const patches = [...group.patches.entries()].filter(([number]) => typeof number === 'number').map(([, patch]) => patch);
  const first = patches[0];
  if (!first) return null;
  const progress = flipbookEntry(group, 'all');
  const key = [progress.status, ...patches.map(patch =>
    `${patch.sequenceId}:${patch.frames.length}:${patch.backendStatus}`)].join('|');
  if (group.mixedKey === key) return group.patches.get('all');
  group.mixedKey = key;
  const frames = patches.flatMap(patch => patch.frames)
    .sort((a, b) => a.observedAt.localeCompare(b.observedAt) || a.productId.localeCompare(b.productId));
  const datesById = new Map(patches.flatMap(patch => patch.dates).map(date => [date.id, date]));
  const warnings = [...new Set(patches.flatMap(patch => patch.warnings))];
  if (progress.status === 'partial') warnings.unshift('Some detector exposures could not be prepared.');
  return {
    ...first, frames, dates: frames.map(frame => datesById.get(frame.dateId)),
    sequenceId: null, display: null,
    displayByBand: Object.fromEntries(patches.map(patch => [patch.frames[0].band, patch.display])),
    backendStatus: progress.status === 'ready' ? 'ready' : 'partial',
    warnings,
    failureCount: Object.values(group.bands).reduce((count, entry) => count + (entry.failure_count || 0), 0),
    candidateCount: progress.candidate_count,
    attemptedCount: progress.attempted_count,
  };
}

function presentFlipbook(group) {
  let changed = false;
  for (const [number, record] of group.records) {
    const frames = record.patch.frames.filter(frame => group.previews.has(frame.imageUrl));
    if (!frames.length || record.presented === record.key && record.readyCount === frames.length) continue;
    record.presented = record.key;
    record.readyCount = frames.length;
    const usableDates = new Set(frames.map(frame => frame.dateId));
    const patch = { ...record.patch,
      frames: frames.map(frame => ({ ...frame, previewUrl: group.previews.get(frame.imageUrl) })),
      dates: record.patch.dates.filter(date => usableDates.has(date.id)),
    };
    group.patches.set(number, patch);
    changed = true;
  }
  const mixed = mixedFlipbookPatch(group);
  if (mixed && mixed !== group.patches.get('all')) {
    group.patches.set('all', mixed);
    changed = true;
  }

  const number = group.wantedBand;
  if (number != null) {
    const waiting = unreadyFlipbookPreviews(group, number);
    const patch = group.patches.get(number);
    const waitingForSharedFrame = !group.opened && waiting.some(frame => frame.dateId === state.activeDateId);
    if (patch && !waitingForSharedFrame && !group.scheduling.has(number)) {
      const sameBand = state.band === number;
      stopPlayback(); cancelZoom();
      state.patch = patch;
      state.prepared = group;
      state.band = number;
      state.fov = Math.min(state.fov, patch.fovMaxArcmin || 20);
      if (!sameBand || group.opened) state.activeDateId = '';
      state.linkedSequenceId = patch.sequenceId;
      state.view = 'viewer';
      state.request = null;
      state.error = null;
      state.recovery = null;
      state.drawer = '';
      state.imageError = false;
      group.opened = true;
      group.wantedBand = null;
      settleFrame();
      lastRealView = realViewSnapshot();
      prunePreparedImages();
      syncUrl();
      render();
      return;
    }
    const entry = flipbookEntry(group, number);
    if (!group.scheduling.has(number) && !group.queueWaiting.has(number) && !waiting.length &&
        ['ready', 'partial', 'empty', 'unavailable'].includes(entry?.status)) {
      const reason = entry.error || (entry.status === 'empty'
        ? `No usable images were found for ${bandName(number)} at these coordinates and dates.`
        : `Image previews could not load for ${bandName(number)}.${firstPreviewError(group, number) ? ` First error: ${firstPreviewError(group, number)}` : ''}`);
      group.wantedBand = null;
      if (group.opened) group.bandError = reason;
      else if (entry.status !== 'empty' && lastRealView && sameSkyPosition(state, lastRealView)) {
        failOpeningFlipbook(group, { code: entry.status, message: reason });
        return;
      }
      else {
        state.band = number;
        state.patch = null;
        state.request = null;
        state.linkedSequenceId = null;
        state.error = { code: entry.status, message: reason };
        state.view = 'band-unavailable';
        syncUrl();
      }
      changed = true;
    }
  }

  if (state.view === 'viewer' && state.prepared === group) {
    const patch = group.patches.get(state.band);
    if (patch && patch !== state.patch) {
      state.patch = patch;
      settleFrame();
      lastRealView = realViewSnapshot();
      changed = true;
    }
  }
  const stateKey = [state.view, group.wantedBand, group.bandError,
    group.jobStatus, group.allRequested, [...group.scheduling].join(','), [...group.queueWaiting].join(','),
    ...Object.values(group.bands).map(entry => `${entry.status}:${entry.candidate_count ?? ''}`)].join('|');
  if (changed || group.lastPaint !== stateKey) {
    group.lastPaint = stateKey;
    render();
    if (state.lightCurve?.pendingProduct && state.prepared === group) {
      openLightCurveExposure(state.lightCurve.pendingProduct, true);
    }
  } else {
    // A progress poll without fresh images only updates the status sentence.
    const status = app.querySelector('[data-flipbook-progress]');
    if (status) {
      const note = flipbookNotice().match(/>([^<]*)<\/p>$/)?.[1];
      if (note && status.textContent !== note) status.textContent = note;
    }
  }
}

async function pollFlipbook(group) {
  if (group.polling || group.jobStatus !== 'running' || activeFlipbook !== group) return;
  group.polling = true;
  let failures = 0;
  try {
    while (activeFlipbook === group && group.jobStatus === 'running') {
      await waitForFlipbookUpdate(group.controller.signal);
      try {
        const payload = await readFlipbook(group.jobId, { signal: group.controller.signal });
        considerFlipbookSnapshot(group, payload);
        failures = 0;
      } catch (error) {
        if (error?.name === 'AbortError' || error?.code === 'flipbook_unavailable' || ++failures >= 3) throw error;
      }
    }
  } catch (error) {
    if (activeFlipbook !== group || error?.name === 'AbortError') return;
    group.jobStatus = 'complete';
    const reason = error.message || 'Could not check for more SPHEREx images.';
    group.bandError = reason;
    if (group.wantedBand === 'all') group.allError = reason;
    else if (group.wantedBand != null) group.bands[`D${group.wantedBand}`] = { status: 'unavailable', error: reason };
    scheduleFlipbookPaint(group);
  } finally {
    group.polling = false;
  }
}

function waitForFlipbookQueue(group, band) {
  group.queueWaiting.add(band);
  scheduleFlipbookPaint(group);
}

async function startFlipbookJob(group, band, refresh = false) {
  const { payload, target, queryRange } = await startFlipbook({
    ...group.query, band, refresh, signal: group.controller.signal,
    onBusy: () => waitForFlipbookQueue(group, band),
  }).finally(() => group.queueWaiting.delete(band));
  if (activeFlipbook !== group) return;
  if (band === 'all') group.allRequested = true;
  group.jobId = payload.job_id;
  group.target = target;
  group.queryRange = queryRange;
  group.revision = -1;
  considerFlipbookSnapshot(group, payload);
  void pollFlipbook(group);
}

function failOpeningFlipbook(group, error) {
  if (activeFlipbook !== group || error?.name === 'AbortError') return;
  const attempted = {
    ...group.query, band: state.band, fov: state.fov,
    customFov: state.customFov, activeDateId: state.activeDateId,
  };
  const failure = { code: error.code || 'backend_unavailable', message: error.message || 'The data service did not answer.' };
  stopFlipbook();
  state.request = null;
  if (lastRealView && sameSkyPosition(state, lastRealView)) {
    restoreRealView();
    state.recovery = { error: failure, attempted };
  } else {
    state.patch = null;
    state.error = failure;
    state.view = 'error';
  }
  prunePreparedImages();
  syncUrl();
  render();
}

async function loadLiveFlipbook(coords, { restoreSequenceId, forceRefresh }) {
  stopFlipbook(); cancelZoom(); stopPlayback();
  state.request?.controller.abort();
  ++loadSequence;
  const group = {
    live: true,
    controller: new AbortController(),
    query: { ra: state.ra, dec: state.dec, from: state.from, to: state.to },
    queryRange: null,
    target: coords,
    wantedBand: state.band,
    allRequested: state.band === 'all',
    opened: false,
    revision: -1,
    bands: Object.fromEntries([1, 2, 3, 4, 5, 6].map(number => [`D${number}`, { status: 'idle' }])),
    patches: new Map(), records: new Map(), previews: new Map(),
    pendingPreviews: new Set(), failedPreviews: new Set(), previewErrors: new Map(),
    scheduling: new Set(), queueWaiting: new Set(),
  };
  const band = state.band;
  activeFlipbook = group;
  state.prepared = group;
  state.request = { controller: group.controller, ...group.query, flipbook: group };
  state.linkedSequenceId = restoreSequenceId;
  state.view = 'loading';
  state.drawer = '';
  state.error = null;
  state.recovery = null;
  state.sampleFallback = null;
  prunePreparedImages();
  syncUrl(); render();
  try {
    if (!restoreSequenceId) {
      await startFlipbookJob(group, band, forceRefresh);
      return;
    }
    const restore = fov => restoreRealSequence({ sequenceId: restoreSequenceId,
      ...group.query, band, fov, signal: group.controller.signal });
    let patch;
    try {
      patch = await restore(20);
    } catch (error) {
      if (error?.name === 'AbortError' || state.fov >= 20) throw error;
      patch = await restore(state.fov);
      patch.fovMaxArcmin = state.fov;
      patch.warnings.push(`Wider pixels could not be fetched from these same parent exposures: ${error.message}`);
    }
    if (activeFlipbook !== group) return;
    group.pinnedBand = band;
    group.bands[`D${band}`] = { status: 'ready' };
    group.records.set(band, { key: restoreSequenceId, patch });
    pumpFlipbookPreviews(group);
    scheduleFlipbookPaint(group);
  } catch (error) {
    failOpeningFlipbook(group, error);
  }
}

async function chooseLiveBand(group, number) {
  if (activeFlipbook !== group) return;
  group.bandError = null;
  if (number === 'all' && group.pinnedBand != null) {
    state.band = 'all';
    state.activeDateId = '';
    await loadPatch();
    return;
  }
  if (state.view === 'viewer' && state.band === number) {
    group.wantedBand = null;
    scheduleFlipbookPaint(group);
    return;
  }
  group.wantedBand = number;
  if (group.patches.has(number) && (number !== 'all' || group.allRequested)) {
    presentFlipbook(group);
    return;
  }
  // A failed browser PNG fetch does not invalidate the backend's real frames.
  // Retry those exact image URLs before asking the archive to build again.
  const retryExisting = number !== 'all' || group.allRequested;
  const retried = retryExisting ? retryFailedFlipbookPreviews(group, number) : 0;
  if (retryExisting && (retried || unreadyFlipbookPreviews(group, number).length)) {
    if (!group.opened) {
      state.band = number;
      state.view = 'loading';
      state.request = { controller: group.controller, ...group.query, flipbook: group };
    }
    scheduleFlipbookPaint(group);
    return;
  }
  const status = flipbookEntry(group, number)?.status;
  if (group.scheduling.has(number) || ACTIVE_BAND_STATUSES.has(status)) {
    scheduleFlipbookPaint(group);
    return;
  }
  const refresh = ['empty', 'unavailable'].includes(status);
  if (number === 'all') group.allError = null;
  group.scheduling.add(number);
  if (!group.opened) {
    state.band = number;
    state.view = 'loading';
    state.request = { controller: group.controller, ...group.query, flipbook: group };
  }
  scheduleFlipbookPaint(group);
  try {
    if (!group.jobId) await startFlipbookJob(group, number, refresh);
    else {
      try {
        const payload = await requestFlipbookBand(group.jobId, number, {
          signal: group.controller.signal, refresh,
          onBusy: () => waitForFlipbookQueue(group, number),
        });
        if (number === 'all') group.allRequested = true;
        considerFlipbookSnapshot(group, payload);
        void pollFlipbook(group);
      } catch (error) {
        if (error?.code !== 'flipbook_unavailable') throw error;
        await startFlipbookJob(group, number, refresh);
      }
    }
  } catch (error) {
    if (activeFlipbook === group && error?.name !== 'AbortError') {
      if (number === 'all') group.allError = error.message;
      else group.bands[`D${number}`] = { status: 'unavailable', error: error.message };
      if (group.opened && group.wantedBand === number && error.code === 'busy_timeout') {
        group.wantedBand = null;
        group.bandError = `${bandName(number)} could not start. ${error.message}`;
      }
    }
  } finally {
    group.scheduling.delete(number);
    group.queueWaiting.delete(number);
    scheduleFlipbookPaint(group);
  }
}

async function loadPatch({ forceRefresh = false, restoreSequenceId = null } = {}) {
  if (staticPreview) return showSavedRealExample({ restoreView: true });
  const coords = parseCoordinates(state.ra, state.dec);
  if (!coords.isValid) { state.raError = coords.raError; state.decError = coords.decError; state.view = 'home'; render(); return; }
  state.raError = ''; state.decError = ''; state.dateError = '';
  if (liveFlipbook) return loadLiveFlipbook(coords, { restoreSequenceId, forceRefresh });
  cancelZoom();
  stopPlayback();
  state.drawer = '';
  state.request?.controller.abort();
  const controller = new AbortController();
  const sequence = ++loadSequence;
  state.linkedSequenceId = restoreSequenceId;
  state.request = { controller, ra: state.ra, dec: state.dec };
  state.view = 'loading';
  state.error = null;
  state.recovery = null;
  state.sampleFallback = null;
  syncUrl(); render();
  try {
    const band = state.band || 4;
    const cacheKey = realCacheKey(coords, band);
    const prepared = await preparePatchSet(coords, restoreSequenceId, controller.signal, state.request, forceRefresh);
    const patch = prepared.patches.get(band);
    if (sequence !== loadSequence) return;
    state.prepared = prepared;
    if (!patch) {
      const entry = prepared.bands[`D${band}`];
      const status = entry?.status || 'unavailable';
      state.patch = null;
      state.linkedSequenceId = null;
      state.error = { code: status, message: status === 'empty'
        ? `The archive found no usable Band ${band} observations for this patch and date range.`
        : status === 'queued' || status === 'running'
          ? `Band ${band} has not finished preparing within the time limit.`
          : String(entry?.error || `Band ${band} could not be prepared from the archive.`) };
      state.view = 'band-unavailable';
      return;
    }
    if (!restoreSequenceId && patch.status === 'ok' && patch.backendStatus === 'ready') {
      realCache.set(cacheKey, patch);
    }
    state.patch = patch;
    state.linkedSequenceId = patch.bundledRealExample ? null : patch.sequenceId;
    state.view = patch.status === 'empty' ? 'empty' : 'viewer';
    if (patch.status === 'ok') {
      if (!patch.bands.some(b => b.number === state.band)) state.band = patch.bands.find(b => b.number === 4)?.number || patch.bands[0]?.number || 0;
      settleFrame();
      lastRealView = realViewSnapshot();
    }
  } catch (error) {
    if (sequence !== loadSequence || error?.name === 'AbortError') return;
    const failure = { code: String(error.code || 'unavailable'), message: String(error.message || 'The data service did not answer.') };
    let savedPatch = null;
    if (failure.code === 'archive_unavailable' && matchesBundledExample(state) &&
        !(lastRealView && !lastRealView.patch.bundledRealExample && sameRealQuery(state, lastRealView))) {
      try { savedPatch = await loadBundledRealPatch({ signal: controller.signal }); }
      catch { /* The archive error remains the honest result if the saved sample is unavailable. */ }
    }
    if (sequence !== loadSequence) return;
    if (savedPatch) {
      state.from = '2025-05-01'; state.to = '2026-11-15';
      state.patch = savedPatch;
      state.prepared = null;
      state.linkedSequenceId = null;
      state.view = 'viewer';
      state.sampleFallback = failure;
      settleFrame();
      lastRealView = realViewSnapshot();
    } else if (lastRealView && sameSkyPosition(state, lastRealView)) {
      const attempted = {
        ra: state.ra, dec: state.dec, from: state.from, to: state.to,
        band: state.band, fov: state.fov, customFov: state.customFov,
        activeDateId: state.activeDateId,
      };
      restoreRealView();
      state.recovery = { error: failure, attempted };
    } else {
      state.patch = null;
      state.error = failure;
      state.view = 'error';
    }
  } finally {
    if (sequence === loadSequence) { state.request = null; prunePreparedImages(); syncUrl(); render(); }
  }
}
function resetHome() {
  stopFlipbook();
  cancelZoom();
  state.request?.controller.abort(); ++loadSequence; stopPlayback();
  state.view = 'home'; state.patch = null; state.drawer = ''; state.request = null;
  state.recovery = null;
  state.sampleFallback = null;
  state.fov = DEFAULT_FOV; state.customFov = false;
  state.band = liveFlipbook ? 'all' : 4; state.activeDateId = '';
  state.linkedSequenceId = null;
  state.prepared = null;
  state.raError = ''; state.decError = ''; state.dateError = ''; state.headerRa = null; state.headerDec = null;
  syncUrl(); render();
}
function setBand(number) {
  if (state.prepared?.live) {
    if (state.prepared === activeFlipbook) void chooseLiveBand(state.prepared, number);
    else if (number !== state.band) { state.band = number; state.activeDateId = ''; void loadPatch(); }
    return;
  }
  if (number === state.band || !state.patch?.bands.some(b => b.number === number)) return;
  const next = state.prepared?.patches.get(number);
  if (!next) return;
  state.prepared.patches.set(state.band, state.patch);
  stopPlayback();
  state.patch = next;
  state.band = number;
  state.activeDateId = '';
  state.linkedSequenceId = next.bundledRealExample ? null : next.sequenceId;
  state.drawer = '';
  state.recovery = null;
  settleFrame();
  lastRealView = realViewSnapshot();
  syncUrl(); render();
}
function viewPreparedBand(number) {
  const patch = state.prepared?.patches.get(number);
  if (!patch || patch.source !== 'real') return;
  stopPlayback(); cancelZoom();
  state.patch = patch;
  state.band = number;
  state.activeDateId = '';
  state.linkedSequenceId = patch.bundledRealExample ? null : patch.sequenceId;
  state.view = 'viewer'; state.error = null; state.recovery = null;
  settleFrame(); lastRealView = realViewSnapshot(); syncUrl(); render();
}
function setFov(value, custom = false) {
  const max = Math.min(20, state.patch?.fovMaxArcmin || 20);
  const fov = Number(value);
  if (!Number.isInteger(fov) || fov < 1 || fov > max) return;
  // If a wider raster is already loading, a narrower requested view can use
  // that exact raster on arrival. Avoid a duplicate server build.
  if (state.zoomRequest && fov > (state.patch?.baseFovArcmin || state.fov) &&
      fov <= state.zoomRequest.fov) {
    state.zoomRequest.desiredFov = fov;
    state.zoomRequest.desiredCustom = custom;
    render();
    return;
  }
  if (fov === state.fov) {
    cancelZoom();
    state.customFov = custom;
    syncUrl(); render();
    return;
  }
  const patch = state.patch;
  const beforeFrame = currentFrame();
  const beforeFov = state.fov;
  cancelZoom();
  if (state.view !== 'viewer' || fov <= (patch.baseFovArcmin || beforeFov)) {
    state.fov = fov; state.customFov = custom;
    syncUrl(); render();
    if (patch?.source === 'real') logFovTransition(patch, beforeFrame, beforeFov, patch, currentFrame(), fov);
    if (patch?.source === 'real') {
      state.prepared?.patches.set(state.band, patch);
      const coords = parseCoordinates(state.ra, state.dec);
      if (coords.isValid) realCache.set(realCacheKey(coords, state.band), patch);
      lastRealView = realViewSnapshot();
    }
    return;
  }

  // Expansion needs pixels beyond the currently loaded raster. Fetch only
  // from the exact persisted parent exposures, with no new archive selection.
  const controller = new AbortController();
  const request = { controller, fov };
  state.zoomRequest = request;
  render();
  fetchZoomedRealPatch({ patch, fov, signal: controller.signal }).then(zoomed => {
    if (state.zoomRequest !== request || state.patch !== patch || state.view !== 'viewer') return;
    const selectedBeforeCommit = currentFrame();
    state.patch = zoomed;
    state.prepared?.patches.set(state.band, zoomed);
    state.fov = request.desiredFov || fov;
    state.customFov = request.desiredCustom ?? custom;
    state.zoomRequest = null;
    // A user may select another timeline frame while this expansion runs.
    // Keep their latest selection; the response has the same ordered frames.
    settleFrame();
    state.linkedSequenceId = zoomed.sequenceId;
    const coords = parseCoordinates(state.ra, state.dec);
    if (zoomed.backendStatus === 'ready' && coords.isValid) {
      realCache.set(realCacheKey(coords, state.band), zoomed);
    }
    lastRealView = realViewSnapshot();
    syncUrl(); render();
    logFovTransition(patch, selectedBeforeCommit, beforeFov, zoomed, currentFrame(), state.fov);
  }).catch(error => {
    if (state.zoomRequest !== request || error?.name === 'AbortError') return;
    state.zoomRequest = null;
    state.zoomError = String(error.message || 'This width could not be loaded from the same exposures.');
    render();
  });
}
function retrySameParentPixels() {
  const patch = state.patch;
  if (patch?.source !== 'real' || !patch.zoomMissingOuterCount || !patch.sequenceId) return;
  cancelZoom();
  const controller = new AbortController();
  const request = { controller, fov: patch.baseFovArcmin };
  state.zoomRequest = request;
  render();
  fetchZoomedRealPatch({ patch, fov: patch.baseFovArcmin, refresh: true,
    signal: controller.signal }).then(async updated => {
    if (state.zoomRequest !== request || state.patch !== patch) return;
    const ready = new Map([[state.band, updated]]);
    await preloadPreparedPatches(ready, {}, controller.signal, request, true);
    if (!ready.has(state.band)) throw new Error('The refreshed same-parent preview did not load. The old view remains visible.');
    if (state.zoomRequest !== request || state.patch !== patch) return;
    state.patch = updated;
    state.prepared?.patches.set(state.band, updated);
    state.zoomRequest = null;
    settleFrame(); prunePreparedImages(); syncUrl(); render();
    lastRealView = realViewSnapshot();
  }).catch(error => {
    if (state.zoomRequest !== request || error?.name === 'AbortError') return;
    state.zoomRequest = null;
    state.zoomError = String(error.message || 'The same parent pixels are still unavailable.');
    render();
  });
}
function changeRange(key, value) {
  if (value && !validDate(value)) return;
  if (value !== state[key]) cancelLightCurve();
  state[key] = value;
  if (state.from && state.to && state.from > state.to) [state.from, state.to] = [state.to, state.from];
  stopPlayback();
  if (rangeNeedsRefresh()) loadPatch();
  else { settleFrame(); syncUrl(); render(); }
}
function openDrawer(name, opener) {
  restoreDrawerAction = opener.dataset.action;
  state.drawer = name; render();
}
function closeDrawer() {
  state.drawer = ''; render();
  if (restoreDrawerAction) app.querySelector(`[data-action="${restoreDrawerAction}"]`)?.focus();
  restoreDrawerAction = null;
}

app.addEventListener('submit', event => {
  if (!['home-form','header-form'].includes(event.target.id)) return;
  event.preventDefault(); readForm(event.target);
});
app.addEventListener('change', event => {
  if (event.target.dataset.range === 'from' || event.target.dataset.range === 'to') changeRange(event.target.dataset.range, event.target.value);
  if (event.target.dataset.range === 'fov') setFov(Number(event.target.value), true);
  if (event.target.dataset.range === 'frame') syncUrl();
  if (event.target.dataset.range === 'light-curve-channel' && state.lightCurve) {
    state.lightCurve.channel = event.target.value;
    state.lightCurve.product = null;
    state.lightCurve.pendingProduct = null;
    state.lightCurve.frameNotice = null;
    paintLightCurve();
  }
  if (event.target.dataset.range === 'light-curve-exposure') openLightCurveExposure(event.target.value);
  if (event.target.dataset.range === 'playback-rate') {
    const rate = Number(event.target.value);
    if (PLAYBACK_RATES.includes(rate)) {
      state.playbackRate = rate;
      if (state.playing) setPlayback(true);
    }
  }
});
app.addEventListener('input', event => {
  if (event.target.dataset.range !== 'frame') return;
  stopPlayback();
  selectFrame(Number(event.target.value) - 1, { share: false });
});
app.addEventListener('click', event => {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  if (target.tagName === 'A') event.preventDefault();
  const action = target.dataset.action;
  if (action === 'home') {
    if (staticPreview) showSavedRealExample();
    else resetHome();
  }
  else if (action === 'retry') loadPatch(state.view === 'error' && state.linkedSequenceId
    ? { restoreSequenceId: state.linkedSequenceId } : { forceRefresh: true });
  else if (action === 'refresh-observations') loadPatch({ forceRefresh: true });
  else if (action === 'expand-flipbook') loadPatch();
  else if (action === 'retry-preparation') {
    if (activeFlipbook && unreadyFlipbookPreviews(activeFlipbook, state.band, 'failed').length) {
      void chooseLiveBand(activeFlipbook, state.band);
    } else loadPatch({ forceRefresh: true });
  }
  else if (action === 'view-prepared-band') viewPreparedBand(Number(target.dataset.band));
  else if (action === 'retry-same-parent') retrySameParentPixels();
  else if (action === 'search-date-range') loadPatch({ forceRefresh: true });
  else if (action === 'target-preset') {
    const preset = TEST_TARGETS.find(item => item.id === target.dataset.target);
    const form = target.closest('#home-form');
    if (preset && form) readForm(form, preset);
  }
  else if (action === 'saved-real') showSavedRealExample();
  else if (action === 'retry-failed-query' && state.recovery) {
    Object.assign(state, state.recovery.attempted);
    loadPatch({ forceRefresh: true });
  }
  else if (action === 'return-real' && lastRealView) {
    stopFlipbook();
    restoreRealView(); state.recovery = null; syncUrl(); render();
  }
  else if (action === 'cancel') resetHome();
  else if (action === 'clear-and-retry') { state.from = ''; state.to = ''; loadPatch(); }
  else if (action === 'clear-range') {
    cancelLightCurve();
    state.from = ''; state.to = ''; stopPlayback();
    if (rangeNeedsRefresh()) loadPatch(); else { settleFrame(); syncUrl(); render(); }
  }
  else if (action === 'prev') { stopPlayback(); stepFrame(-1); }
  else if (action === 'next') { stopPlayback(); stepFrame(1); }
  else if (action === 'play') setPlayback(!state.playing);
  else if (action === 'date') {
    stopPlayback();
    selectFrame(renderedFrames.findIndex(frame => frame.dateId === target.dataset.date));
  }
  else if (action === 'measure-light-curve') void measureLightCurve();
  else if (action === 'pick-light-curve') pickLightCurveTarget(target, event);
  else if (action === 'measure-crosshair') { state.lightCurveTarget = null; void measureLightCurve({ replace: true }); }
  else if (action === 'close-light-curve') { cancelLightCurve(); paintLightCurve(); }
  else if (action === 'light-curve-point') openLightCurveExposure(target.dataset.product);
  else if (action === 'band') setBand(target.dataset.band === 'all' ? 'all' : Number(target.dataset.band));
  else if (action === 'fov') setFov(Number(target.dataset.fov));
  else if (action === 'fov-custom') { state.customFov = true; render(); }
  else if (action === 'open-bands') openDrawer('bands', target);
  else if (action === 'open-details') openDrawer('details', target);
  else if (action === 'close-drawer') closeDrawer();
  else if (action === 'show-details') {
    if (matchMedia('(max-width: 760px)').matches) openDrawer('details', target);
    else { const details = app.querySelector('#frame-details-desktop'); if (details) { details.open = true; state.aboutOpen = true; details.scrollIntoView({ behavior: 'smooth', block: 'center' }); } }
  }
});
app.addEventListener('toggle', event => {
  if (event.target.id?.startsWith('frame-details-')) {
    state.aboutOpen = event.target.open;
    if (state.aboutOpen) updateFrameDetails();
  }
}, true);
app.addEventListener('load', event => {
  if (!event.target.matches?.('[data-frame-image]')) return;
  const badge = app.querySelector('[data-real-badge]');
  if (state.patch?.source === 'real' && badge) badge.textContent =
    state.patch.bundledRealExample ? 'Saved real SPHEREx observations' : 'Real SPHEREx observation';
}, true);
app.addEventListener('error', event => {
  if (!event.target.matches?.('[data-frame-image]')) return;
  const badge = app.querySelector('[data-real-badge]');
  if (state.patch?.source === 'real' && badge) badge.textContent = 'Image unavailable';
  const view = event.target.closest('.image-view');
  if (view) { view.innerHTML = '<div class="state-card" role="alert"><h2>Image could not be displayed</h2><p>The cutout URL did not load. Check the backend image service or try again.</p><button type="button" class="pill-button" data-action="retry">Try again</button></div>'; }
}, true);
app.addEventListener('cancel', event => { if (event.target.matches('.mobile-drawer')) { event.preventDefault(); closeDrawer(); } });
app.addEventListener('click', event => { if (event.target.matches('.mobile-drawer')) closeDrawer(); });
document.addEventListener('visibilitychange', () => { if (document.hidden && state.playing) setPlayback(false); });
window.addEventListener('popstate', () => {
  const p = new URLSearchParams(location.search);
  state.ra = p.get('ra') || ''; state.dec = p.get('dec') || '';
  state.from = validDate(p.get('from')) ? p.get('from') : '';
  state.to = validDate(p.get('to')) ? p.get('to') : '';
  state.band = bandFromQuery(p); state.activeDateId = p.get('date') || '';
  state.fov = fovFromQuery(p);
  state.customFov = ![5, 10, 20].includes(state.fov);
  state.linkedSequenceId = /^[a-f0-9]{32}$/.test(p.get('sequence') || '') ? p.get('sequence') : null;
  if (staticPreview) showSavedRealExample({ restoreView: true });
  else if (state.ra && state.dec) loadPatch({ restoreSequenceId: state.linkedSequenceId });
  else resetHome();
});

if (staticPreview) showSavedRealExample({ restoreView: true });
else {
  render();
  if (state.ra && state.dec) loadPatch({ restoreSequenceId: state.linkedSequenceId });
  else if (params.has('source')) syncUrl();
}
