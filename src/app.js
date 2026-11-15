import { WISE_0855, parseCoordinates, fetchRealPatch, loadBundledRealPatch, loadDemoPatch, retryWhileBusy } from './data.js';
import { homeStory, heroArt, heroOrnaments } from './story.js';

const app = document.querySelector('#app');
const params = new URLSearchParams(location.search);
const defaultFov = source => source === 'synthetic' ? 10 : 5;
function fovFromQuery(query, source) {
  const value = Number(query.get('fov'));
  if (Number.isInteger(value) && value >= 3 && value <= 20) return value;
  // Earlier shared coordinate links omitted fov and displayed a 10′ field.
  return query.get('ra') && query.get('dec') ? 10 : defaultFov(source);
}
const initialSource = params.get('source') === 'demo' ? 'synthetic' : 'real';
const initialFov = fovFromQuery(params, initialSource);
const state = {
  view: 'home', source: initialSource,
  ra: params.get('ra') || '', dec: params.get('dec') || '',
  from: validDate(params.get('from')) ? params.get('from') : '',
  to: validDate(params.get('to')) ? params.get('to') : '',
  fov: initialFov,
  customFov: ![5, 10, 20].includes(initialFov) && initialFov >= 3 && initialFov <= 20,
  band: Number(params.get('band')) || 4, activeDateId: params.get('date') || '',
  patch: null, error: null, raError: '', decError: '', dateError: '',
  headerRa: null, headerDec: null,
  playing: false, drawer: '', aboutOpen: false, request: null, imageError: false,
  recovery: null,
  sampleFallback: null,
};
let playbackTimer = null;
let loadSequence = 0;
let restoreDrawerAction = null;
const realCache = new Map();
let lastRealView = null;

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}
function validDate(value) {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}
function rangeLabel(band) { return band?.rangeUm?.map(n => Number(n).toFixed(band.approximateRange ? 2 : 3)).join('–') + ' µm'; }
function displayDate(iso) {
  const date = new Date(`${String(iso).slice(0, 10)}T12:00:00Z`);
  return Number.isNaN(date.getTime()) ? String(iso).slice(0, 10) :
    new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', day: 'numeric', month: 'short', year: 'numeric' }).format(date);
}
function displayMonth(iso) {
  const date = new Date(`${String(iso).slice(0, 10)}T12:00:00Z`);
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', year: 'numeric' }).format(date);
}
function bandColor(band) { return band?.color || '#e2c54a'; }
function selectedBand() { return state.patch?.bands.find(b => b.number === state.band); }
function allBandFrames() {
  if (!state.patch) return [];
  const order = new Map(state.patch.dates.map((d, i) => [d.id, i]));
  return state.patch.frames.filter(f => f.band === state.band)
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
function realViewSnapshot() {
  return {
    patch: state.patch, ra: state.ra, dec: state.dec, from: state.from, to: state.to,
    fov: state.fov, customFov: state.customFov, band: state.band,
    activeDateId: state.activeDateId,
  };
}
function sameSkyPosition(first, second) {
  const a = parseCoordinates(first.ra, first.dec);
  const b = parseCoordinates(second.ra, second.dec);
  return a.isValid && b.isValid && Math.abs(a.raDeg - b.raDeg) < 1e-5 &&
    Math.abs(a.decDeg - b.decDeg) < 1e-5;
}
function matchesBundledExample(query) {
  return sameSkyPosition(query, { ra: '180', dec: '-30' }) &&
    query.band === 4 && query.fov === 5 &&
    (query.from || '2025-05-01') === '2025-05-01' &&
    (query.to || new Date().toISOString().slice(0, 10)) === '2026-11-15';
}
function restoreRealView() {
  if (!lastRealView) return;
  Object.assign(state, lastRealView);
  state.source = 'real';
  state.view = 'viewer';
  state.error = null;
  state.headerRa = null;
  state.headerDec = null;
  state.imageError = false;
  settleFrame();
}
function rangeNeedsRefresh() {
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
function settleFrame() {
  const frame = currentFrame();
  state.activeDateId = frame?.dateId || '';
  state.imageError = false;
  if (!frame) stopPlayback();
}
function stopPlayback() {
  if (playbackTimer) clearInterval(playbackTimer);
  playbackTimer = null;
  state.playing = false;
}
function stepFrame(direction) {
  const frames = visibleFrames();
  if (!frames.length) return;
  const index = Math.max(0, frames.findIndex(f => f.dateId === state.activeDateId));
  state.activeDateId = frames[(index + direction + frames.length) % frames.length].dateId;
  state.imageError = false;
  syncUrl(); render();
}
function setPlayback(on) {
  stopPlayback();
  if (on && visibleFrames().length > 1) {
    state.playing = true;
    playbackTimer = setInterval(() => {
      if (document.hidden) return;
      stepFrame(1);
    }, 900);
  }
  render();
}
function syncUrl() {
  const url = new URL(location.href);
  for (const key of ['ra', 'dec', 'source', 'band', 'date', 'fov', 'from', 'to']) url.searchParams.delete(key);
  if (state.view !== 'home' && state.ra && state.dec) {
    url.searchParams.set('ra', state.ra);
    url.searchParams.set('dec', state.dec);
    if (state.source === 'synthetic') url.searchParams.set('source', 'demo');
    if (state.band) url.searchParams.set('band', String(state.band));
    if (state.activeDateId) url.searchParams.set('date', state.activeDateId);
    url.searchParams.set('fov', String(state.fov));
    if (state.from) url.searchParams.set('from', state.from);
    if (state.to) url.searchParams.set('to', state.to);
  }
  history.replaceState(null, '', url);
}
function headingMark() {
  return '<svg class="brand-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="7"/><path d="M12 0v5m0 14v5M0 12h5m14 0h5"/></svg>';
}
function sourceChoice() {
  return `<div class="source-choice" role="group" aria-label="Data source">
    <button type="button" data-action="source-real" class="${state.source === 'real' ? 'active' : ''}" aria-pressed="${state.source === 'real'}">Real observations</button>
    <button type="button" data-action="source-demo" class="${state.source === 'synthetic' ? 'active' : ''}" aria-pressed="${state.source === 'synthetic'}">Synthetic demo</button>
  </div>`;
}
function header() {
  const compact = state.view !== 'home';
  return `<header class="site-header">
    <button class="brand" type="button" data-action="home" aria-label="Ingress: go to the start page">${headingMark()}<span class="brand-name">Ingress</span><span class="brand-subtitle">SPHEREx sky-patch time viewer</span></button>
    ${compact ? `<form class="header-form" id="header-form" novalidate>
      <label>RA <input class="coord-input" name="ra" aria-label="Right ascension" aria-invalid="${!!state.raError}" placeholder="08h55m10.831s" value="${esc(state.headerRa ?? state.ra)}" autocomplete="off"></label>
      <label>Dec <input class="coord-input" name="dec" aria-label="Declination" aria-invalid="${!!state.decError}" placeholder="-07d14m42.53s" value="${esc(state.headerDec ?? state.dec)}" autocomplete="off"></label>
      <button class="soft-button" type="submit">Show patch</button>
    </form><div class="header-source">${sourceChoice()}</div>
    <button class="mobile-change" data-action="home" type="button">Change patch</button>` : `<div class="home-source-label">Data ${sourceChoice()}</div>`}
  </header>${compact && (state.raError || state.decError) ? `<div class="header-validation" role="alert">${esc(state.raError || state.decError)}</div>` : ''}<div class="spectrum-rule ${compact ? '' : 'sc-prog'}" aria-hidden="true"></div>`;
}
function footer() {
  return `<footer class="site-footer">Team Ingress · NASA Space Apps Challenge 2026 project · Independent of NASA.
    When real observations are shown, available archive links and data notes appear under “About this frame.”
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
          <div class="search-actions"><button type="submit" class="primary-button">✦ &nbsp;Show this patch</button><span class="example-prompt">No coordinates handy?</span><button type="button" class="pill-button" data-action="try-wise">Try synthetic WISE 0855</button></div>
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
  if (kind === 'loading') return `<main class="state-main"><div class="state-card" role="status" aria-live="polite"><span class="eyebrow">Searching the sky</span><h1>${state.request?.waiting ? 'Waiting for the current search' : 'Gathering this patch'}</h1>
    <p>RA ${esc(request.ra)} · Dec ${esc(request.dec)} ${state.source === 'synthetic' ? '· Synthetic example' : '· Real observations'}</p>
    <ol class="loading-steps"><li>Checking coordinates</li><li>Finding available observations</li><li>Preparing aligned images</li></ol><div class="progress-bar" aria-hidden="true"><span></span></div>
    <p class="small">${state.request?.waiting ? 'The server prepares one sequence at a time. This search will start automatically when it is free.' : 'Large areas and many exposures may take longer.'}</p><button type="button" class="pill-button" data-action="cancel">Cancel</button></div></main>`;
  if (kind === 'empty') {
    const demoOnly = state.error?.code === 'demo_target_unavailable';
    const real = state.patch?.source === 'real';
    return `<main class="state-main"><div class="state-card" role="status"><span class="state-icon">◎</span><h1>${demoOnly ? 'The example covers one sky patch' : 'No usable images for this search'}</h1>
      <p>${demoOnly ? 'The synthetic example is centered on WISE 0855. Real observations can be searched at other coordinates.' : `No usable frames were found in ${real ? `Band ${state.band}` : 'this patch'} for the requested dates. Try ${real ? 'another band, ' : ''}another patch, or a wider date range.`}</p>
      <p class="small mono">RA ${esc(state.ra)} · Dec ${esc(state.dec)}</p><div class="actions"><button class="primary-button" data-action="home">Change coordinates</button>
      ${state.from || state.to ? '<button class="pill-button" data-action="clear-and-retry">Search all dates</button>' : ''}
      <button class="pill-button" data-action="try-wise">Try WISE 0855 demo</button></div>
      ${real ? `<div class="empty-band-list" aria-label="Search another detector band">${state.patch.bands.map(b => `<button type="button" class="pill-button ${state.band === b.number ? 'active' : ''}" data-action="band" data-band="${b.number}" ${state.band === b.number ? 'disabled' : ''}>Search Band ${b.number}</button>`).join('')}</div>` : ''}</div></main>`;
  }
  return `<main class="state-main"><div class="state-card" role="alert"><span class="state-icon">⚠</span><h1>Could not load this patch</h1>
    <p>${esc(state.error?.message || 'The data service did not answer. Please try again.')}</p><p>Your search has not been replaced with simulated data.</p>
    <div class="actions"><button class="primary-button" data-action="retry">Try again</button><button class="pill-button" data-action="home">Change coordinates</button>${state.source === 'real' ? '<button class="pill-button" data-action="saved-real">View saved real observations (5′)</button>' : ''}${lastRealView && state.source === 'real' ? '<button class="pill-button" data-action="return-real">Return to last real view</button>' : ''}<button class="pill-button" data-action="try-wise">View WISE 0855 demo</button></div>
    <details><summary>Technical details</summary><p>${esc(state.error?.code || 'request_failed')}</p></details></div></main>`;
}
function imageStage(frame, band, index, frames) {
  const patch = state.patch;
  const synthetic = patch.source === 'synthetic';
  const date = patch.dates.find(d => d.id === frame.dateId);
  const zoom = synthetic ? Math.max(1, (patch.fovMaxArcmin || 20) / state.fov) : 1;
  const pixelScale = patch.pixelScaleArcsec;
  const scaleLength = state.fov >= 12 ? 5 : state.fov >= 6 ? 2 : 1;
  const mover = synthetic && Array.isArray(frame.moverPct) ? `<div class="demo-marker" style="left:${Number(frame.moverPct[0])}%;top:${Number(frame.moverPct[1])}%"><span>Simulated moving source</span></div>` : '';
  const variable = synthetic && Array.isArray(frame.variablePct) ? `<div class="demo-marker variable" style="left:${Number(frame.variablePct[0])}%;top:${Number(frame.variablePct[1])}%"><span>Simulated variable source</span></div>` : '';
  const coverageNote = frame.flags?.find(flag => flag.code === 'partial_coverage');
  return `<div class="stage-top"><div><span class="frame-date">${esc(displayDate(frame.dateId))}</span><span class="frame-ordinal">Frame ${index + 1} of ${frames.length} · ${esc(date?.timeUtc || '')} UTC</span></div>
    <span class="wavelength-chip" style="--band-color:${bandColor(band)}"><i class="color-chip"></i>${frame.wavelengthUm == null ? 'Wavelength unavailable' : '≈' + esc(Number(frame.wavelengthUm).toFixed(2)) + ' µm at target'} · Band ${band.number}</span></div>
    <div class="image-view ${synthetic ? 'demo' : ''}" aria-label="${synthetic ? 'Synthetic illustration' : 'SPHEREx observation'} at ${esc(displayDate(frame.dateId))}">
      <div class="image-layer" style="--zoom:${zoom}"><img data-frame-image src="${esc(frame.imageUrl)}" alt="${synthetic ? 'Synthetic sky patch illustration' : 'SPHEREx sky patch image'}, band ${band.number}, ${esc(displayDate(frame.dateId))}" />${mover}${variable}<div class="crosshair" title="Entered coordinates"></div></div>
      ${synthetic ? '<div class="image-watermark">SYNTHETIC DEMO — NOT SPHEREx OBSERVATIONS</div>' : ''}
      <div class="compass"><span>N</span><span>↑</span><span>E ←</span></div>
      ${pixelScale ? `<div class="scale-bar" style="--scale-width:${(scaleLength / state.fov * 100).toFixed(1)}%">${scaleLength}′</div>` : ''}<div class="fov-label">${state.fov}′ wide</div>
    </div><div class="image-caption"><span>Crosshair marks the entered coordinates${pixelScale ? ` · 1 pixel ≈ ${esc(pixelScale)}″ of sky` : ''}</span><a href="#frame-details-desktop" data-action="show-details">Source and details</a></div>
    ${coverageNote ? `<p class="coverage-note">${esc(coverageNote.label)} Missing pixels are dark.</p>` : ''}`;
}
function wavelengthComparisonNote(frames, source) {
  if (source !== 'real' || frames.length < 2) return '';
  const wavelengths = frames.map(frame => frame.wavelengthUm).filter(value => Number.isFinite(value) && value > 0);
  if (wavelengths.length < 2) return '';
  const first = Math.min(...wavelengths);
  const last = Math.max(...wavelengths);
  if (last - first < Math.max(0.02, first * 0.02)) return '';
  return `<p class="wavelength-caution">These frames span approximately ${first.toFixed(2)}–${last.toFixed(2)} µm at the target. Brightness changes may reflect different wavelengths; compare positions separately.</p>`;
}
function timeline(frames, band) {
  const patch = state.patch;
  const all = allBandFrames();
  const curr = currentFrame();
  const lookup = new Map(all.map(f => [f.dateId, f]));
  const dayCounts = new Map();
  const sampled = patch.source === 'real' && Number.isInteger(patch.candidateCount) && Number.isInteger(patch.attemptedCount)
    && patch.candidateCount > patch.attemptedCount
    ? ` · sampled ${patch.attemptedCount} of ${patch.candidateCount} candidate exposures` : '';
  for (const date of patch.dates) {
    const day = date.id.slice(0, 10);
    dayCounts.set(day, (dayCounts.get(day) || 0) + 1);
  }
  let lastMonth = '';
  const dots = patch.dates.map(date => {
    const month = displayMonth(date.id);
    const showMonth = month !== lastMonth;
    lastMonth = month;
    const frame = lookup.get(date.id);
    const inside = inRange(date.id);
    const frameNote = frame?.flags?.some(flag => !['quality_unchecked', 'partial_sequence'].includes(flag.code));
    const cls = `${!frame ? 'missing' : ''} ${frameNote ? 'flagged' : ''} ${curr?.dateId === date.id ? 'selected' : ''} ${!inside ? 'outside' : ''}`;
    const repeated = dayCounts.get(date.id.slice(0, 10)) > 1;
    return `<button type="button" class="date-dot ${cls}" data-action="date" data-date="${esc(date.id)}" ${!frame || !inside ? 'disabled' : ''} aria-label="${esc(displayDate(date.id))}, ${esc(date.timeUtc)} UTC, ${frame ? `image in band ${state.band}` : `no image in band ${state.band}`}${inside ? '' : ', outside date filter'}" aria-current="${curr?.dateId === date.id ? 'true' : 'false'}"><i aria-hidden="true"></i>${esc(date.id.slice(8, 10).replace(/^0/, ''))}<span class="month">${repeated ? esc(date.timeUtc) : showMonth ? esc(month) : '&nbsp;'}</span></button>`;
  }).join('');
  return `<section class="timeline-card" aria-label="Observation timeline"><div class="timeline-head"><span class="timeline-title">◷ &nbsp; Time · observation dates</span>
      <div class="timeline-range"><label for="range-from">From</label><input class="date-input" id="range-from" data-range="from" type="date" value="${esc(state.from)}" min="2025-05-01" max="${new Date().toISOString().slice(0, 10)}" aria-label="Earliest UTC date">
      <label for="range-to">to</label><input class="date-input" id="range-to" data-range="to" type="date" value="${esc(state.to)}" min="2025-05-01" max="${new Date().toISOString().slice(0, 10)}" aria-label="Latest UTC date">
      <button type="button" class="pill-button" data-action="clear-range" ${!state.from && !state.to ? 'disabled' : ''}>All dates</button></div></div>
      <div class="timeline-summary ${frames.length ? '' : 'empty'}">${frames.length ? `Showing ${frames.length} of ${all.length} loaded frames at ${esc(rangeLabel(band))}${sampled}` : `No frames at ${esc(rangeLabel(band))} in this date range. Widen the range or choose another wavelength.`}</div>
      ${wavelengthComparisonNote(frames, patch.source)}
      <div class="timeline-content"><div class="playback"><button type="button" class="soft-button" data-action="prev" ${frames.length < 2 ? 'disabled' : ''} aria-label="Previous frame">Ⅰ◀</button>
      <button type="button" class="primary-button" data-action="play" ${frames.length < 2 ? 'disabled' : ''} aria-label="${state.playing ? 'Pause' : 'Play'} timeline">${state.playing ? '❚❚ Pause' : '▶ Play'}</button>
      <button type="button" class="soft-button" data-action="next" ${frames.length < 2 ? 'disabled' : ''} aria-label="Next frame">▶Ⅰ</button></div>
      <div class="dots-wrap"><div class="date-dots">${dots}</div></div></div>
      <div class="legend"><span><i></i> image in this wavelength</span>${patch.source === 'synthetic' ? '<span><i class="hollow"></i> no image in this wavelength</span>' : ''}<span><i class="flag"></i> frame note</span></div>
    </section>`;
}
function settingsPanel(section = 'all', scope = 'desktop') {
  const patch = state.patch;
  const band = selectedBand();
  const frame = currentFrame();
  const maxFov = patch.source === 'synthetic' ? 20 : Math.min(20, patch.fovMaxArcmin || 20);
  const datesInBand = n => patch.frames.filter(f => f.band === n && inRange(f.dateId)).length;
  const bands = patch.bands.map(b => `<button type="button" class="band-option ${state.band === b.number ? 'active' : ''}" style="--band-color:${bandColor(b)}" data-action="band" data-band="${b.number}" aria-pressed="${state.band === b.number}"><i class="color-chip" aria-hidden="true"></i> <strong>${esc(rangeLabel(b))}</strong><small>Band ${b.number}</small><span class="band-count">${patch.source === 'real' && state.band !== b.number ? 'Search' : `${datesInBand(b.number)} frame${datesInBand(b.number) === 1 ? '' : 's'}`}</span></button>`).join('');
  const fovs = [20,10,5].map(n => `<button class="pill-button ${state.fov === n && !state.customFov ? 'active' : ''}" type="button" data-action="fov" data-fov="${n}" aria-pressed="${state.fov === n && !state.customFov}" ${n > maxFov ? 'disabled' : ''}>${{20:'Wide',10:'Medium',5:'Close'}[n]} ${n}′</button>`).join('');
  const detail = `<details class="about" id="frame-details-${scope}" ${state.aboutOpen || section === 'details' ? 'open' : ''}><summary>About this frame <span>${patch.source === 'synthetic' ? 'Synthetic' : 'Real'} · ${frame?.flags.length ? `${frame.flags.length} note(s)` : 'No notes'}</span></summary>
    ${frame ? `<dl><div><dt>Date (UTC)</dt><dd>${esc(displayDate(frame.dateId))} ${esc(patch.dates.find(d => d.id === frame.dateId)?.timeUtc || '')}</dd></div>
      <div><dt>Wavelength range</dt><dd>${esc(rangeLabel(band))} · Band ${band.number}</dd></div><div><dt>At target (approx.)</dt><dd>${frame.wavelengthUm == null ? 'Not provided' : `${esc(Number(frame.wavelengthUm).toFixed(2))} µm`}</dd></div>
      <div><dt>Field of view</dt><dd>${state.fov}′ wide</dd></div><div><dt>Orientation</dt><dd>${esc(patch.orientation)}</dd></div>
      <div><dt>Data notes</dt><dd>${frame.flags.length ? frame.flags.map(f => esc(f.label)).join('; ') : 'None reported'}</dd></div>
      ${patch.source === 'real' ? `<div><dt>Observation ID</dt><dd>${esc(frame.observationId || 'Not provided')}</dd></div><div><dt>Pipeline</dt><dd>${esc(frame.pipelineVersion || 'Not provided')}</dd></div>${patch.releaseDate ? `<div><dt>Release date</dt><dd>${esc(patch.releaseDate)}</dd></div>` : ''}<div><dt>Archive</dt><dd>${frame.archiveUrl ? `<a target="_blank" rel="noopener noreferrer" href="${esc(frame.archiveUrl)}">Open IRSA cutout ↗</a>` : 'Not provided'}</dd></div>` : ''}</dl>${patch.source === 'synthetic' ? '<p class="notice">All demo images, observation times, motion and flags are invented. Motion is exaggerated for illustration.</p>' : patch.warnings?.length ? `<p class="notice">${patch.warnings.map(esc).join(' ')}</p>` : ''}` : '<p class="notice">No frame in this wavelength and date range.</p>'}</details>`;
  const wavelengths = `<section class="settings-section"><h2>Wavelength</h2><p>SPHEREx observes infrared light in six detector bands. Choose a range to see its available frames. Different bands may have different dates and wavelengths within the range.</p>
      <div class="spectrum-ruler" aria-hidden="true">${['#7aa7ff','#4fc3d9','#6dd08a','#e2c54a','#f2994a','#f0776c'].map((color,i) => `<i style="--band-color:${color}" class="${patch.bands.some(b=>b.number===i+1) ? 'present' : ''}"></i>`).join('')}</div>
      <div class="spectrum-ends"><span>${patch.source === 'real' ? '0.744' : '0.75'} µm</span><span>${patch.source === 'real' ? '5.002' : '5.0'} µm</span></div><div class="band-list">${bands}</div>
      <p class="band-footnote">${patch.source === 'real' ? 'DR1 and QR3 wide-survey images only; deep-field-only exposures are not searched. Each detector band is searched separately.' : 'Only bands with images of this patch are listed.'} The wavelength at the target can change from frame to frame.</p></section>`;
  const field = `<section class="settings-section"><h2>Field of view</h2><div class="fov-grid">${fovs}<button class="pill-button ${state.customFov ? 'active' : ''}" type="button" data-action="fov-custom" aria-pressed="${state.customFov}">Custom</button></div>
      ${state.customFov ? `<div class="custom-fov"><label for="custom-width-${scope}">Width <output>${state.fov}′</output></label><input id="custom-width-${scope}" data-range="fov" type="range" min="3" max="${Math.floor(maxFov)}" value="${state.fov}" step="1" /></div>` : ''}
      <p class="band-footnote">A smaller patch magnifies the image. For real observations, changing width requests a new cutout.</p></section>`;
  return `<aside class="settings" aria-label="Settings and details"><div class="panel-eyebrow">${scope === 'desktop' ? 'Settings and details' : ''}</div>
    ${section === 'details' ? '' : wavelengths}${section === 'bands' ? '' : field + detail}</aside>`;
}
function viewer() {
  const patch = state.patch;
  const frames = visibleFrames();
  const band = selectedBand();
  const frame = currentFrame();
  const synthetic = patch.source === 'synthetic';
  return `<main class="viewer-layout"><div class="viewer-main"><div class="stage">
    <div class="provenance"><span class="status-badge ${synthetic ? 'synthetic' : patch.bundledRealExample ? 'saved-real' : 'real'}" data-real-badge>${synthetic ? '⚠ Synthetic demo — not SPHEREx observations' : patch.bundledRealExample ? 'Saved real SPHEREx observations' : 'Loading real image…'}</span>
    <span class="target-name">${esc(patch.target.name || 'Your patch')} · <span class="mono muted">RA ${esc(state.ra)} · Dec ${esc(state.dec)}</span></span>
    ${patch.backendStatus === 'partial' ? `<span class="partial-note" role="status">${patch.failureCount ? `Partial sequence · ${patch.failureCount} exposure${patch.failureCount === 1 ? '' : 's'} could not be loaded` : 'Partial date coverage · the archive search was incomplete'}</span>` : ''}</div>
    ${patch.bundledRealExample ? `<div class="saved-real-banner" role="status"><div><strong>Saved real observations · RA 180°, Dec −30° · Band 4 · 5′</strong><p>${state.sampleFallback ? `${esc(state.sampleFallback.message)} ` : ''}These SPHEREx frames were prepared earlier. They are a fixed snapshot, not a live search or simulated images; newly released observations may be missing.</p></div><button type="button" class="pill-button" data-action="retry">Retry live search</button></div>` : ''}
    ${state.recovery && !synthetic ? `<div class="recovery-banner" role="status"><div><strong>Showing previously loaded real observations</strong><p>${esc(state.recovery.error.message)} The new search (Band ${state.recovery.attempted.band}, ${state.recovery.attempted.fov}′ wide) did not load. The images below are from your last successful view of this sky position; they are not simulated or new results.</p></div><button type="button" class="pill-button" data-action="retry-failed-query">Retry new search</button></div>` : ''}
    ${frame ? imageStage(frame, band, frames.indexOf(frame), frames) : `<div class="state-card" role="status"><h2>No frames in this range</h2><p>There are no ${esc(rangeLabel(band))} images between the selected dates. Clear dates or pick another wavelength.</p><button class="pill-button" data-action="clear-range">All dates</button></div>`}
    ${timeline(frames, band)}
    <div class="mobile-actions"><button class="soft-button" type="button" data-action="open-bands">Wavelength · ${esc(rangeLabel(band))}</button><button class="soft-button" type="button" data-action="open-details">Details &amp; width</button></div>
    </div></div>${settingsPanel()}</main>${state.drawer ? `<dialog class="mobile-drawer" aria-label="${state.drawer === 'bands' ? 'Wavelength settings' : 'Frame details and field of view'}"><div class="drawer-head"><h2>${state.drawer === 'bands' ? 'Wavelength' : 'Details'}</h2><button class="soft-button" type="button" data-action="close-drawer" aria-label="Close panel">Close ✕</button></div>${settingsPanel(state.drawer, 'mobile')}</dialog>` : ''}`;
}
function render() {
  const contents = state.view === 'home' ? home() : state.view === 'viewer' ? viewer() : statusCard(state.view);
  app.innerHTML = `<div class="app-shell ${state.view === 'home' ? 'home-shell' : ''}">${header()}${contents}${footer()}</div>`;
  if (state.drawer) {
    const dialog = app.querySelector('.mobile-drawer');
    if (dialog) { dialog.showModal(); dialog.querySelector('[data-action="close-drawer"]')?.focus(); }
  }
}
function readForm(form) {
  const data = new FormData(form);
  const ra = String(data.get('ra') || '').trim();
  const dec = String(data.get('dec') || '').trim();
  const headerForm = form.id === 'header-form';
  if (headerForm) { state.headerRa = ra; state.headerDec = dec; }
  else { state.ra = ra; state.dec = dec; }
  if (form.id === 'home-form') { state.from = String(data.get('from') || ''); state.to = String(data.get('to') || ''); }
  if (state.from && state.to && state.from > state.to) [state.from,state.to] = [state.to,state.from];
  const coords = parseCoordinates(ra, dec);
  state.raError = coords.raError;
  state.decError = coords.decError;
  state.dateError = (state.from && !validDate(state.from) || state.to && !validDate(state.to)) ? 'Enter valid dates.' : '';
  if (!coords.isValid || state.dateError) { render(); (state.raError ? app.querySelector('[name="ra"]') : state.decError ? app.querySelector('[name="dec"]') : null)?.focus(); return; }
  state.ra = ra; state.dec = dec; state.headerRa = null; state.headerDec = null;
  loadPatch();
}
async function showSavedRealExample() {
  state.request?.controller.abort();
  const controller = new AbortController();
  const sequence = ++loadSequence;
  stopPlayback();
  state.drawer = '';
  state.source = 'real';
  state.ra = '180'; state.dec = '-30'; state.band = 4; state.fov = 5;
  state.from = '2025-05-01'; state.to = '2026-11-15';
  state.customFov = false; state.activeDateId = '';
  state.error = null; state.recovery = null; state.sampleFallback = null;
  state.request = { controller, ra: state.ra, dec: state.dec };
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
    if (sequence === loadSequence) { state.request = null; syncUrl(); render(); }
  }
}
async function loadPatch({ forceRefresh = false } = {}) {
  const coords = parseCoordinates(state.ra, state.dec);
  if (!coords.isValid) { state.raError = coords.raError; state.decError = coords.decError; state.view = 'home'; render(); return; }
  state.raError = ''; state.decError = ''; state.dateError = '';
  stopPlayback();
  state.drawer = '';
  state.request?.controller.abort();
  const controller = new AbortController();
  const sequence = ++loadSequence;
  state.request = { controller, ra: state.ra, dec: state.dec };
  state.view = 'loading';
  state.error = null;
  state.recovery = null;
  state.sampleFallback = null;
  syncUrl(); render();
  try {
    // The backend builds one detector sequence. Keep a successful result when revisiting a band.
    const band = state.band || 4;
    const cacheKey = realCacheKey(coords, band);
    const patch = state.source === 'synthetic'
      ? await loadDemoPatch({ ra: state.ra, dec: state.dec, signal: controller.signal })
      : (!forceRefresh && realCache.get(cacheKey)) || await retryWhileBusy(
        () => fetchRealPatch({ ra: state.ra, dec: state.dec, fov: state.fov,
          from: state.from, to: state.to, band, signal: controller.signal }),
        { signal: controller.signal, onBusy: () => {
          if (sequence !== loadSequence || state.request?.waiting) return;
          state.request.waiting = true;
          render();
        } });
    if (sequence !== loadSequence) return;
    if (state.source === 'real' && patch.status === 'ok' && patch.backendStatus === 'ready') {
      realCache.set(cacheKey, patch);
    }
    state.patch = patch;
    state.view = patch.status === 'empty' ? 'empty' : 'viewer';
    if (patch.status === 'ok') {
      if (!patch.bands.some(b => b.number === state.band)) state.band = patch.bands.find(b => b.number === 4)?.number || patch.bands[0]?.number || 0;
      settleFrame();
      if (patch.source === 'real') lastRealView = realViewSnapshot();
    }
  } catch (error) {
    if (sequence !== loadSequence || error?.name === 'AbortError') return;
    const failure = { code: String(error.code || 'unavailable'), message: String(error.message || 'The data service did not answer.') };
    let savedPatch = null;
    if (state.source === 'real' && failure.code === 'archive_unavailable' && matchesBundledExample(state)) {
      try { savedPatch = await loadBundledRealPatch({ signal: controller.signal }); }
      catch { /* The archive error remains the honest result if the saved sample is unavailable. */ }
    }
    if (sequence !== loadSequence) return;
    if (savedPatch) {
      state.from = '2025-05-01'; state.to = '2026-11-15';
      state.patch = savedPatch;
      state.view = 'viewer';
      state.sampleFallback = failure;
      settleFrame();
      lastRealView = realViewSnapshot();
    } else if (state.source === 'real' && lastRealView && sameSkyPosition(state, lastRealView)) {
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
      state.view = error.code === 'demo_target_unavailable' ? 'empty' : 'error';
    }
  } finally {
    if (sequence === loadSequence) { state.request = null; syncUrl(); render(); }
  }
}
function resetHome() {
  state.request?.controller.abort(); ++loadSequence; stopPlayback();
  state.view = 'home'; state.patch = null; state.drawer = ''; state.request = null;
  state.recovery = null;
  state.sampleFallback = null;
  state.fov = defaultFov(state.source); state.customFov = false;
  state.raError = ''; state.decError = ''; state.dateError = ''; state.headerRa = null; state.headerDec = null;
  syncUrl(); render();
}
function changeSource(source) {
  if (state.source === source) return;
  if (!state.customFov && state.fov === defaultFov(state.source)) state.fov = defaultFov(source);
  state.source = source;
  if (state.view === 'home') render();
  else loadPatch();
}
function setBand(number) {
  if (!state.patch?.bands.some(b => b.number === number)) return;
  const oldDate = state.activeDateId;
  state.band = number; stopPlayback();
  state.activeDateId = oldDate;
  state.drawer = '';
  if (state.source === 'real') loadPatch();
  else { settleFrame(); syncUrl(); render(); }
}
function setFov(value, custom = false) {
  const max = state.patch?.source === 'synthetic' ? 20 : Math.min(20, state.patch?.fovMaxArcmin || 20);
  const fov = Number(value);
  if (!Number.isInteger(fov) || fov < 3 || fov > max) return;
  state.fov = fov; state.customFov = custom;
  if (state.view === 'viewer' && state.source === 'real') loadPatch();
  else { syncUrl(); render(); }
}
function changeRange(key, value) {
  if (value && !validDate(value)) return;
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
});
app.addEventListener('click', event => {
  const target = event.target.closest('[data-action]');
  if (!target) return;
  if (target.tagName === 'A') event.preventDefault();
  const action = target.dataset.action;
  if (action === 'home') resetHome();
  else if (action === 'source-real') changeSource('real');
  else if (action === 'source-demo') changeSource('synthetic');
  else if (action === 'try-wise') {
    if (!state.customFov && state.fov === defaultFov(state.source)) state.fov = defaultFov('synthetic');
    state.ra = WISE_0855.ra; state.dec = WISE_0855.dec; state.source = 'synthetic'; state.from = ''; state.to = ''; state.band = 4; state.activeDateId = ''; loadPatch();
  } else if (action === 'retry') loadPatch({ forceRefresh: true });
  else if (action === 'saved-real') showSavedRealExample();
  else if (action === 'retry-failed-query' && state.recovery) {
    Object.assign(state, state.recovery.attempted);
    loadPatch({ forceRefresh: true });
  }
  else if (action === 'return-real' && lastRealView) {
    restoreRealView(); state.recovery = null; syncUrl(); render();
  }
  else if (action === 'cancel') resetHome();
  else if (action === 'clear-and-retry') { state.from = ''; state.to = ''; loadPatch(); }
  else if (action === 'clear-range') {
    state.from = ''; state.to = ''; stopPlayback();
    if (rangeNeedsRefresh()) loadPatch(); else { settleFrame(); syncUrl(); render(); }
  }
  else if (action === 'prev') { stopPlayback(); stepFrame(-1); }
  else if (action === 'next') { stopPlayback(); stepFrame(1); }
  else if (action === 'play') setPlayback(!state.playing);
  else if (action === 'date') { stopPlayback(); state.activeDateId = target.dataset.date; syncUrl(); render(); }
  else if (action === 'band') setBand(Number(target.dataset.band));
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
  if (event.target.id?.startsWith('frame-details-')) state.aboutOpen = event.target.open;
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
  state.source = p.get('source') === 'demo' ? 'synthetic' : 'real';
  state.from = validDate(p.get('from')) ? p.get('from') : '';
  state.to = validDate(p.get('to')) ? p.get('to') : '';
  state.band = Number(p.get('band')) || 4; state.activeDateId = p.get('date') || '';
  state.fov = fovFromQuery(p, state.source);
  state.customFov = ![5, 10, 20].includes(state.fov);
  if (state.ra && state.dec) loadPatch(); else resetHome();
});

render();
if (state.ra && state.dec) loadPatch();
