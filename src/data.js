const REAL_EXAMPLE_URL = './public/real-example/ra180-dec-30-d4/manifest.json';
const REAL_EXAMPLE_IMAGE = /^\/public\/real-example\/ra180-dec-30-d4\/frame_\d{3}\.png$/;
const MISSION_START = '2025-05-01';

const BAND_INFO = [
  // SPHEREx Explanatory Supplement v2.1, Table 2 (November 2026).
  { number: 1, rangeUm: [0.744, 1.116], color: '#7aa7ff' },
  { number: 2, rangeUm: [1.099, 1.651], color: '#4fc3d9' },
  { number: 3, rangeUm: [1.636, 2.421], color: '#6dd08a' },
  { number: 4, rangeUm: [2.423, 3.822], color: '#e2c54a' },
  { number: 5, rangeUm: [3.809, 4.420], color: '#f2994a' },
  { number: 6, rangeUm: [4.412, 5.002], color: '#f0776c' },
];

export class PatchDataError extends Error {
  constructor(code, message, cause) {
    super(message, cause ? { cause } : undefined);
    this.name = 'PatchDataError';
    this.code = code;
  }
}

function waitForAvailableBuild(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('The request was cancelled.', 'AbortError'));
      return;
    }
    const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, ms);
    function abort() {
      clearTimeout(timer);
      reject(new DOMException('The request was cancelled.', 'AbortError'));
    }
    signal?.addEventListener('abort', abort, { once: true });
  });
}

// The Python service can prepare only one sequence at once. A browser abort does
// not necessarily stop a build already running there, so wait and retry a 409.
export async function retryWhileBusy(operation, { signal, onBusy = () => {}, wait = waitForAvailableBuild,
  delayMs = 5000, maxRetries = 30 } = {}) {
  for (let retries = 0; ; retries++) {
    if (signal?.aborted) throw new DOMException('The request was cancelled.', 'AbortError');
    try {
      return await operation();
    } catch (error) {
      if (error?.code !== 'busy') throw error;
      if (retries >= maxRetries) {
        throw new PatchDataError('busy_timeout', 'Another archive search is still running. Try again in a few minutes.', error);
      }
      onBusy(retries + 1);
      await wait(delayMs, signal);
    }
  }
}

export function parseRA(input) {
  const value = String(input ?? '').trim().toLowerCase();
  if (!value) return { error: 'Enter an RA, like 08h55m10.831s.' };
  const rangeError = 'RA must be between 0h and 24h, or 0° and 360°.';
  const sexagesimal = value.match(/^(\d{1,2})\s*(?:h|:|\s)\s*(\d{1,2})\s*(?:m|:|\s)\s*(\d{1,2}(?:\.\d+)?)\s*s?$/);
  if (sexagesimal) {
    const [, h, m, s] = sexagesimal.map(Number);
    if (h >= 24 || m >= 60 || s >= 60) return { error: rangeError };
    return { degrees: 15 * (h + m / 60 + s / 3600) };
  }
  const decimal = value.match(/^(\d+(?:\.\d+)?)\s*(h|d|deg|°)?$/);
  if (!decimal) return { error: 'Try 08h55m10.831s or 133.795 degrees.' };
  const n = Number(decimal[1]);
  const inHours = decimal[2] === 'h';
  if (inHours ? n >= 24 : n >= 360) return { error: rangeError };
  return { degrees: inHours ? n * 15 : n };
}

export function parseDec(input) {
  const value = String(input ?? '').trim().toLowerCase().replace(/[−–]/g, '-');
  if (!value) return { error: 'Enter a Dec, like -07d14m42.53s.' };
  const rangeError = 'Dec must be between −90° and +90°.';
  const sexagesimal = value.match(/^([+-]?)\s*(\d{1,2})\s*(?:d|°|:|\s)\s*(\d{1,2})\s*(?:m|'|′|:|\s)\s*(\d{1,2}(?:\.\d+)?)\s*(?:s|"|″)?$/);
  if (sexagesimal) {
    const d = Number(sexagesimal[2]);
    const m = Number(sexagesimal[3]);
    const s = Number(sexagesimal[4]);
    if (m >= 60 || s >= 60 || d + m / 60 + s / 3600 > 90) return { error: rangeError };
    return { degrees: (sexagesimal[1] === '-' ? -1 : 1) * (d + m / 60 + s / 3600) };
  }
  const decimal = value.match(/^([+-]?)\s*(\d+(?:\.\d+)?)\s*(?:d|deg|°)?$/);
  if (!decimal) return { error: 'Try -07d14m42.53s or -7.2451 degrees.' };
  const n = Number(decimal[2]);
  if (n > 90) return { error: rangeError };
  return { degrees: decimal[1] === '-' ? -n : n };
}

export function parseCoordinates(raText, decText) {
  const ra = parseRA(raText);
  const dec = parseDec(decText);
  const raDeg = ra.degrees;
  const decDeg = dec.degrees;
  return {
    raDeg,
    decDeg,
    raError: ra.error ?? '',
    decError: dec.error ?? '',
    isValid: !ra.error && !dec.error,
  };
}

function isoDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

function positiveFov(value) {
  const fov = Number(value);
  if (!Number.isFinite(fov) || fov < 3 || fov > 20) {
    throw new PatchDataError('invalid_request', 'Field of view must be from 3′ to 20′.');
  }
  return fov;
}

function orderedRange(from, to) {
  if (from && !isoDate(from) || to && !isoDate(to)) {
    throw new PatchDataError('invalid_request', 'Dates must use YYYY-MM-DD.');
  }
  return from && to && from > to ? [to, from] : [from, to];
}

function cleanFlags(flags) {
  if (!Array.isArray(flags)) return [];
  return flags.filter(flag => flag && typeof flag.label === 'string').map(flag => ({
    code: String(flag.code ?? 'quality'),
    label: flag.label,
  }));
}

function validCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function utcObservationTime(value) {
  if (typeof value !== 'string' ||
      !/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(value)) {
    throw new PatchDataError('invalid_response', 'The backend returned an invalid observation time.');
  }
  const observedAt = new Date(`${value.replace(' ', 'T')}Z`);
  if (Number.isNaN(observedAt.getTime())) {
    throw new PatchDataError('invalid_response', 'The backend returned an invalid observation time.');
  }
  return observedAt.toISOString();
}

function stableSourceKey(url) {
  // The backend's frame index changes when the requested date range changes.
  // Derive a compact timeline key from the exposure URL instead.
  let hash = 0xcbf29ce484222325n;
  for (const char of url) {
    hash = (hash ^ BigInt(char.codePointAt(0))) * 0x100000001b3n & 0xffffffffffffffffn;
  }
  return hash.toString(16).padStart(16, '0');
}

export function normalizeSequencePatch(manifest, requestedTarget, requestedBand, queryRange, requestedFov = null, { bundledExample = false, preserveSelectedFrames = false } = {}) {
  if (!manifest || typeof manifest !== 'object' ||
      !['ready', 'partial', 'empty'].includes(manifest.status) ||
      !manifest.request || manifest.request.band !== `D${requestedBand}` ||
      !Array.isArray(manifest.frames)) {
    throw new PatchDataError('invalid_response', 'The SPHEREx backend returned an unknown sequence.');
  }
  if (Math.abs(Number(manifest.request.ra_deg) - requestedTarget.raDeg) > 1e-5 ||
      Math.abs(Number(manifest.request.dec_deg) - requestedTarget.decDeg) > 1e-5 ||
      !Number.isFinite(Number(manifest.request.ra_deg)) ||
      !Number.isFinite(Number(manifest.request.dec_deg))) {
    throw new PatchDataError('invalid_response', 'The backend returned a sequence for another sky position.');
  }
  if (requestedFov != null && Number(manifest.request.fov_arcmin) !== requestedFov ||
      queryRange?.from && manifest.request.start_date !== queryRange.from ||
      queryRange?.to && manifest.request.end_date !== queryRange.to) {
    throw new PatchDataError('invalid_response', 'The backend returned a sequence for a different field or date range.');
  }
  if (manifest.status === 'empty' && manifest.frames.length ||
      manifest.status !== 'empty' && !manifest.frames.length) {
    throw new PatchDataError('invalid_response', 'The backend returned inconsistent image counts.');
  }

  const failures = Array.isArray(manifest.failures) ? manifest.failures.length : 0;
  if (manifest.status === 'empty' && failures) {
    throw new PatchDataError('unavailable', 'The archive found observations, but none could be loaded. Try again shortly.');
  }
  const warnings = (Array.isArray(manifest.limitations) ? manifest.limitations : [])
    .filter(value => typeof value === 'string' && value.trim())
    .map(value => value.trim());
  const zoomMissingOuterCount = validCount(manifest.zoom_missing_outer_count) || 0;
  if (zoomMissingOuterCount) warnings.unshift(`Outer sky pixels could not be loaded from ${zoomMissingOuterCount} selected parent exposure${zoomMissingOuterCount === 1 ? '' : 's'}. The transparent mask is retained.`);
  const qualityWarning = 'Instrument quality flags have not yet been applied.';
  if (!warnings.includes(qualityWarning)) warnings.unshift(qualityWarning);
  if (failures) warnings.unshift(`${failures} observation${failures === 1 ? '' : 's'} could not be used (load or coverage issue).`);
  // Older saved sequences can include a sliver of an exposure at the edge of
  // the patch. A missing target wavelength plus low coverage also indicates
  // an edge-only cutout whose center may be blank. New builds validate the
  // center explicitly; keep old snapshots from presenting a black crosshair.
  const usableFrames = manifest.frames.filter(frame => {
    // A zoom retains its previously selected parent exposures, including
    // uncovered edges in a wider field. Their transparent masks remain visible.
    if (preserveSelectedFrames) return true;
    const coverage = frame?.coverage_fraction;
    return typeof coverage !== 'number' ||
      coverage >= 0.25 && !(coverage < 0.5 && frame.center_wavelength_um == null);
  });
  const edgeOnlyCount = manifest.frames.length - usableFrames.length;
  if (edgeOnlyCount) warnings.unshift(`${edgeOnlyCount} edge-only observation${edgeOnlyCount === 1 ? ' was' : 's were'} omitted because the requested target is not reliably covered.`);
  if (manifest.status !== 'empty' && !usableFrames.length) {
    throw new PatchDataError('low_coverage', 'The archive images only clip the edge of this patch. Try a smaller field or another band.');
  }
  const frames = usableFrames.map(frame => {
    const isBundledImage = bundledExample && REAL_EXAMPLE_IMAGE.test(frame?.image_url);
    if (!frame || frame.band !== `D${requestedBand}` ||
        typeof frame.image_url !== 'string' ||
        !(/^\/outputs\/[a-f0-9]{32}\/(?:zoom_\d{3}\/)?frame_\d+\.png$/.test(frame.image_url) || isBundledImage) ||
        typeof frame.source_url !== 'string' || !/^https:\/\//.test(frame.source_url)) {
      throw new PatchDataError('invalid_response', 'The backend returned incomplete image metadata.');
    }
    const observedAt = utcObservationTime(frame.date_utc);
    const flags = [{ code: 'quality_unchecked', label: qualityWarning }];
    if (manifest.status === 'partial' && (failures || manifest.archive_listing_partial || !zoomMissingOuterCount)) {
      flags.push({ code: 'partial_sequence', label: failures
        ? 'Some observations could not be used for this sequence.'
        : manifest.archive_listing_partial
          ? 'The archive returned only part of the requested date range.'
          : 'This sequence has incomplete date coverage.' });
    }
    const coverage = Number(frame.coverage_fraction);
    if (Number.isFinite(coverage) && coverage >= 0 && coverage < 0.999) {
      flags.push({ code: 'partial_coverage', label: `Loaded ${manifest.request.fov_arcmin}′ raster coverage: ${Math.round(coverage * 100)}%. The displayed zoom may cover a different fraction.` });
    }
    if (frame.zoom_outer_region_status === 'unavailable') {
      flags.push({ code: 'zoom_outer_unavailable', label:
        typeof frame.zoom_note === 'string' && frame.zoom_note.trim()
          ? frame.zoom_note.trim() : 'The same parent exposure could not supply outer pixels; they remain transparent.' });
    }
    const wavelength = frame.center_wavelength_um == null ? NaN : Number(frame.center_wavelength_um);
    const sourceUrl = frame.source_url;
    const productId = typeof frame.product_id === 'string' && frame.product_id
      ? frame.product_id : new URL(sourceUrl).pathname.split('/').pop() || sourceUrl;
    return {
      dateId: `${observedAt}#${stableSourceKey(frame.source_url)}`,
      band: requestedBand,
      observedAt,
      sourceUrl,
      productId,
      detector: frame.detector || `SPHEREx-D${requestedBand}`,
      frameIndex: frame.index,
      pixelDimensions: frame.pixel_dimensions || null,
      coverageFraction: Number.isFinite(coverage) && coverage >= 0 && coverage <= 1 ? coverage : null,
      nativeCutoutDimensions: frame.zoom_native_cutout_dimensions || frame.native_cutout_dimensions || null,
      skyFootprint: frame.sky_footprint || null,
      wavelengthUm: Number.isFinite(wavelength) && wavelength > 0 ? wavelength : null,
      imageUrl: isBundledImage ? `.${frame.image_url}` : frame.image_url,
      flags,
      observationId: productId,
      pipelineVersion: '',
      archiveUrl: typeof frame.zoom_cutout_url === 'string' && /^https:\/\//.test(frame.zoom_cutout_url)
        ? frame.zoom_cutout_url : typeof frame.cutout_url === 'string' && /^https:\/\//.test(frame.cutout_url)
          ? frame.cutout_url : frame.source_url,
    };
  });
  const ids = new Set(frames.map(frame => frame.dateId));
  if (ids.size !== frames.length) {
    throw new PatchDataError('invalid_response', 'The backend returned duplicate observation times.');
  }
  const dates = frames.map(frame => {
    const observedAt = frame.dateId.split('#')[0];
    return {
      id: frame.dateId,
      observedAt,
      timeUtc: observedAt.slice(11, 16),
      visit: null,
    };
  });
  const pixelCount = Number(manifest.grid?.width_pixels);
  const pixelHeight = Number(manifest.grid?.height_pixels);
  const pixelScale = Number(manifest.grid?.pixel_scale_arcsec);
  return {
    source: 'real',
    bundledRealExample: bundledExample,
    status: manifest.status === 'empty' ? 'empty' : 'ok',
    backendStatus: manifest.status,
    archiveListingPartial: manifest.archive_listing_partial === true,
    cachedPartial: manifest.cached_partial === true && manifest.status === 'partial',
    target: { ...requestedTarget, name: requestedTarget.name || 'Your patch' },
    dates,
    bands: BAND_INFO.map(band => ({ ...band })),
    frames,
    sequenceId: typeof manifest.sequence_id === 'string' && /^[a-f0-9]{32}$/.test(manifest.sequence_id)
      ? manifest.sequence_id : null,
    baseFovArcmin: Number(manifest.request.fov_arcmin),
    grid: {
      widthPixels: Number.isFinite(pixelCount) && pixelCount > 0 ? pixelCount : null,
      heightPixels: Number.isFinite(pixelHeight) && pixelHeight > 0 ? pixelHeight : null,
      wcsHeader: manifest.grid?.wcs_header || '',
      skyFootprint: manifest.grid?.sky_footprint || null,
    },
    display: manifest.display || null,
    sizePx: Number.isFinite(pixelCount) && pixelCount > 0 ? pixelCount : null,
    pixelScaleArcsec: Number.isFinite(pixelScale) && pixelScale > 0 ? pixelScale : null,
    fovMaxArcmin: bundledExample ? Number(manifest.request.fov_arcmin) : 20,
    orientation: manifest.grid?.orientation || 'north up, east left',
    releaseDate: '',
    queryRange,
    warnings,
    failureCount: failures,
    zoomMissingOuterCount,
    candidateCount: validCount(manifest.candidate_count),
    attemptedCount: validCount(manifest.attempted_count),
    skippedCount: validCount(manifest.skipped_count),
  };
}

// Retrieve a different display field from the same persisted parent exposures.
// This endpoint performs no archive search or candidate selection. Reject any
// unexpected replacement rather than silently showing a different observation.
async function fetchZoomManifest(sequenceId, fov, signal, fetchImpl, refresh = false) {
  if (!/^[a-f0-9]{32}$/.test(sequenceId || '')) {
    throw new PatchDataError('zoom_unavailable', 'The parent exposure sequence is unavailable.');
  }
  const requestedFov = positiveFov(fov);
  let response;
  try {
    response = await fetchImpl(refresh ? '/api/zoom?refresh=1' : '/api/zoom', {
      method: 'POST', signal,
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ sequence_id: sequenceId, fov_arcmin: requestedFov }),
    });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new PatchDataError('zoom_unavailable', 'Could not load this width from the same exposures. Your current image remains selected.', error);
  }
  if (!response.ok) {
    let message = '';
    try { const failure = await response.json(); message = failure?.message || failure?.detail || ''; }
    catch { /* The proxy may send HTML. */ }
    throw new PatchDataError('zoom_unavailable', typeof message === 'string' && message ||
      'Could not load this width from the same exposures. Your current image remains selected.');
  }
  let manifest;
  try { manifest = await response.json(); }
  catch (error) { throw new PatchDataError('zoom_unavailable', 'The zoom response was incomplete.', error); }
  if (manifest?.sequence_id !== sequenceId) {
    throw new PatchDataError('zoom_changed_selection', 'The zoom response used another exposure sequence.');
  }
  return manifest;
}

export async function fetchZoomedRealPatch({ patch, fov, refresh = false, signal, fetchImpl = fetch }) {
  if (patch?.source !== 'real' || patch.bundledRealExample || !patch.sequenceId) {
    throw new PatchDataError('zoom_unavailable', 'This saved view cannot expand without its parent exposures.');
  }
  const requestedFov = positiveFov(fov);
  const manifest = await fetchZoomManifest(patch.sequenceId, requestedFov, signal, fetchImpl, refresh);
  const zoomed = normalizeSequencePatch(manifest, patch.target,
    Number(patch.frames[0]?.band), patch.queryRange, requestedFov,
    { preserveSelectedFrames: true });
  if (zoomed.sequenceId !== patch.sequenceId || zoomed.frames.length !== patch.frames.length ||
      zoomed.frames.some((frame, i) => frame.dateId !== patch.frames[i].dateId ||
        frame.sourceUrl !== patch.frames[i].sourceUrl ||
        frame.detector !== patch.frames[i].detector ||
        frame.wavelengthUm !== patch.frames[i].wavelengthUm) ||
      JSON.stringify(zoomed.display) !== JSON.stringify(patch.display)) {
    throw new PatchDataError('zoom_changed_selection',
      'The zoom result changed the selected observations or brightness scale. The original view is still selected.');
  }
  return zoomed;
}

// Shared URLs carry their selected sequence ID so reloading a zoomed patch
// cannot quietly start a new archive search at a different field size.
export async function restoreRealSequence({ sequenceId, ra, dec, band, fov, signal, fetchImpl = fetch }) {
  const coords = parseCoordinates(ra, dec);
  if (!coords.isValid) throw new PatchDataError('invalid_request', coords.raError || coords.decError);
  if (!Number.isInteger(band) || band < 1 || band > 6) {
    throw new PatchDataError('invalid_request', 'Choose a detector band from 1 through 6.');
  }
  const manifest = await fetchZoomManifest(sequenceId, fov, signal, fetchImpl);
  const queryRange = { from: manifest.request?.start_date, to: manifest.request?.end_date };
  return normalizeSequencePatch(manifest, coords, band, queryRange, positiveFov(fov),
    { preserveSelectedFrames: true });
}

const PREPARATION_BANDS = Object.freeze(['D1', 'D2', 'D3', 'D4', 'D5', 'D6']);

function preparationPayload(payload, jobId) {
  if (!payload || payload.job_id !== jobId ||
      !['running', 'complete'].includes(payload.status) ||
      payload.canonical_fov_arcmin !== 20 ||
      !payload.bands || typeof payload.bands !== 'object' ||
      !PREPARATION_BANDS.every(band => payload.bands[band] &&
        ['queued', 'running', 'ready', 'partial', 'empty', 'unavailable'].includes(payload.bands[band].status))) {
    throw new PatchDataError('invalid_response', 'The detector preparation response is incomplete.');
  }
  return payload;
}

export async function prepareRealBands({ ra, dec, band = 4, from = '', to = '', refresh = false, signal,
  onProgress = () => {}, fetchImpl = fetch, timeoutMs = 130_000, wait = waitForAvailableBuild } = {}) {
  const target = parseCoordinates(ra, dec);
  if (!target.isValid) throw new PatchDataError('invalid_request', target.raError || target.decError);
  if (!Number.isInteger(band) || band < 1 || band > 6) {
    throw new PatchDataError('invalid_request', 'Choose a detector band from 1 through 6.');
  }
  const [start, end] = orderedRange(from || MISSION_START, to || new Date().toISOString().slice(0, 10));
  const body = { ra_deg: target.raDeg, dec_deg: target.decDeg,
    start_date: start, end_date: end, band: `D${band}` };
  let response;
  try {
    response = await fetchImpl(refresh ? '/api/prepare?refresh=1' : '/api/prepare', { method: 'POST', signal,
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body) });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new PatchDataError('backend_unavailable', 'Could not start six-band preparation.', error);
  }
  if (!response.ok) {
    let message = '';
    try { message = (await response.json())?.detail || ''; } catch { /* Proxy may send HTML. */ }
    const code = response.status === 502 && /IRSA|archive|SPHEREx image search/i.test(message)
      ? 'archive_unavailable' : 'backend_unavailable';
    throw new PatchDataError(code, typeof message === 'string' && message ||
      `Six-band preparation returned HTTP ${response.status}.`);
  }
  let payload;
  try { payload = await response.json(); }
  catch (error) { throw new PatchDataError('invalid_response', 'Could not read six-band preparation.', error); }
  const jobId = payload?.job_id;
  if (!/^[a-f0-9]{32}$/.test(jobId || '')) {
    throw new PatchDataError('invalid_response', 'The preparation job did not return an ID.');
  }
  payload = preparationPayload(payload, jobId);
  onProgress(payload);
  const deadline = Date.now() + timeoutMs;
  while (payload.status !== 'complete' && Date.now() < deadline) {
    await wait(Math.min(1000, Math.max(1, deadline - Date.now())), signal);
    try {
      response = await fetchImpl(`/api/prepare/${jobId}`, { signal, headers: { Accept: 'application/json' } });
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      throw new PatchDataError('backend_unavailable', 'Could not check detector preparation.', error);
    }
    if (!response.ok) throw new PatchDataError('backend_unavailable', `Detector preparation returned HTTP ${response.status}.`);
    try { payload = preparationPayload(await response.json(), jobId); }
    catch (error) { throw error instanceof PatchDataError ? error :
      new PatchDataError('invalid_response', 'Could not read detector preparation.', error); }
    onProgress(payload);
  }
  return { payload, timedOut: payload.status !== 'complete', target,
    queryRange: { from: start, to: end } };
}

export function preparedBandPatch(entry, { target, band, queryRange }) {
  if (!entry || !['ready', 'partial'].includes(entry.status) || !entry.manifest) return null;
  const patch = normalizeSequencePatch(entry.manifest, target, band, queryRange, 20);
  return patch.status === 'ok' ? patch : null;
}

// A fixed, clearly identified sample of actual SPHEREx cutouts. This is an
// offline example for one sky patch, not a replacement for an arbitrary query.
export async function loadBundledRealPatch({ signal, fetchImpl = fetch } = {}) {
  let response;
  try {
    response = await fetchImpl(REAL_EXAMPLE_URL, { signal });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new PatchDataError('example_unavailable', 'Could not open the saved real observations.', error);
  }
  if (!response.ok) throw new PatchDataError('example_unavailable', 'Could not open the saved real observations.');
  let manifest;
  try { manifest = await response.json(); }
  catch (error) { throw new PatchDataError('example_unavailable', 'The saved real example is incomplete.', error); }
  if (manifest?.bundled_real_example !== true || manifest.request?.ra_deg !== 180 ||
      manifest.request?.dec_deg !== -30 || manifest.request?.fov_arcmin !== 5 ||
      manifest.request?.band !== 'D4' || manifest.request?.start_date !== '2025-05-01' ||
      manifest.request?.end_date !== '2026-11-15') {
    throw new PatchDataError('example_unavailable', 'The saved real example is incomplete.');
  }
  try {
    return normalizeSequencePatch(manifest, parseCoordinates('180', '-30'), 4,
      { from: '2025-05-01', to: '2026-11-15' }, 5, { bundledExample: true });
  } catch (error) {
    throw new PatchDataError('example_unavailable', 'The saved real example is incomplete.', error);
  }
}

export async function fetchRealPatch({ ra, dec, fov = 5, from = '', to = '', band = 4, refresh = false, signal, fetchImpl = fetch }) {
  const coords = parseCoordinates(ra, dec);
  if (!coords.isValid) {
    throw new PatchDataError('invalid_request', coords.raError || coords.decError);
  }
  if (!Number.isInteger(band) || band < 1 || band > 6) {
    throw new PatchDataError('invalid_request', 'Choose a detector band from 1 through 6.');
  }
  const todayUtc = new Date().toISOString().slice(0, 10);
  const [first, last] = orderedRange(from || MISSION_START, to || todayUtc);
  const body = {
    ra_deg: coords.raDeg,
    dec_deg: coords.decDeg,
    fov_arcmin: positiveFov(fov),
    start_date: first,
    end_date: last,
    band: `D${band}`,
  };
  let response;
  try {
    response = await fetchImpl(refresh ? '/api/sequence?refresh=1' : '/api/sequence', {
      method: 'POST', signal,
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new PatchDataError('backend_unavailable', 'Could not reach the SPHEREx data service. Try again shortly.', error);
  }
  if (!response.ok) {
    let message = '';
    try {
      const failure = await response.json();
      if (typeof failure?.message === 'string') message = failure.message;
      else if (typeof failure?.detail === 'string') message = failure.detail;
    } catch { /* A proxy can return an HTML error page. */ }
    const code = response.status === 409 ? 'busy'
      : response.status === 502 && /IRSA|archive|SPHEREx image search/i.test(message) ? 'archive_unavailable'
      : response.status === 504 ? 'backend_timeout' : 'backend_unavailable';
    throw new PatchDataError(code,
      message || `The SPHEREx data service returned HTTP ${response.status}.`);
  }
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new PatchDataError('invalid_response', 'The SPHEREx data service did not return JSON.', error);
  }
  return normalizeSequencePatch(payload, coords, band, { from: first, to: last }, body.fov_arcmin);
}
