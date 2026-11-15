// The boundary between the sky viewer and its data sources. The demo is a
// bundled illustration; only the team's /api/sequence endpoint supplies real images.

export const WISE_0855 = Object.freeze({
  name: 'WISE 0855',
  ra: '08h55m10.831s',
  dec: '-07d14m42.53s',
  raDeg: 133.79512917,
  decDeg: -7.24514722,
});

const DEMO_MANIFEST_URL = './public/demo/frames.json';
const DEMO_FRAMES_URL = './public/demo/frames/';
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
    isWiseExample: !ra.error && !dec.error &&
      Math.abs(raDeg - WISE_0855.raDeg) < 0.0001 &&
      Math.abs(decDeg - WISE_0855.decDeg) < 0.0001,
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

export function normalizeSequencePatch(manifest, requestedTarget, requestedBand, queryRange, requestedFov = null, { bundledExample = false } = {}) {
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
  const qualityWarning = 'Instrument quality flags have not yet been applied.';
  if (!warnings.includes(qualityWarning)) warnings.unshift(qualityWarning);
  if (failures) warnings.unshift(`${failures} observation${failures === 1 ? '' : 's'} could not be used (load or coverage issue).`);
  // Older saved sequences can include a sliver of an exposure at the edge of
  // the patch. A missing target wavelength plus low coverage also indicates
  // an edge-only cutout whose center may be blank. New builds validate the
  // center explicitly; keep old snapshots from presenting a black crosshair.
  const usableFrames = manifest.frames.filter(frame => {
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
    if (!frame || frame.band !== `D${requestedBand}` ||
        typeof frame.image_url !== 'string' ||
        !(/^\/outputs\/[a-f0-9]{32}\/frame_\d+\.png$/.test(frame.image_url) ||
          bundledExample && REAL_EXAMPLE_IMAGE.test(frame.image_url)) ||
        typeof frame.source_url !== 'string' || !/^https:\/\//.test(frame.source_url)) {
      throw new PatchDataError('invalid_response', 'The backend returned incomplete image metadata.');
    }
    const observedAt = utcObservationTime(frame.date_utc);
    const flags = [{ code: 'quality_unchecked', label: qualityWarning }];
    if (manifest.status === 'partial') {
      flags.push({ code: 'partial_sequence', label: failures
        ? 'Some observations could not be used for this sequence.'
        : manifest.archive_listing_partial
          ? 'The archive returned only part of the requested date range.'
          : 'This sequence has incomplete date coverage.' });
    }
    const coverage = Number(frame.coverage_fraction);
    if (Number.isFinite(coverage) && coverage >= 0 && coverage < 0.999) {
      flags.push({ code: 'partial_coverage', label: `This image covers ${Math.round(coverage * 100)}% of the requested field.` });
    }
    const wavelength = frame.center_wavelength_um == null ? NaN : Number(frame.center_wavelength_um);
    return {
      dateId: `${observedAt}#${stableSourceKey(frame.source_url)}`,
      band: requestedBand,
      wavelengthUm: Number.isFinite(wavelength) && wavelength > 0 ? wavelength : null,
      imageUrl: frame.image_url,
      flags,
      observationId: '',
      pipelineVersion: '',
      archiveUrl: typeof frame.cutout_url === 'string' && /^https:\/\//.test(frame.cutout_url)
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
  const pixelScale = Number(manifest.grid?.pixel_scale_arcsec);
  return {
    source: 'real',
    bundledRealExample: bundledExample,
    status: manifest.status === 'empty' ? 'empty' : 'ok',
    backendStatus: manifest.status,
    archiveListingPartial: manifest.archive_listing_partial === true,
    target: { ...requestedTarget, name: requestedTarget.name || 'Your patch' },
    dates,
    bands: BAND_INFO.map(band => ({ ...band })),
    frames,
    sizePx: Number.isFinite(pixelCount) && pixelCount > 0 ? pixelCount : null,
    pixelScaleArcsec: Number.isFinite(pixelScale) && pixelScale > 0 ? pixelScale : null,
    fovMaxArcmin: 20,
    orientation: manifest.grid?.orientation || 'north up, east left',
    releaseDate: '',
    queryRange,
    warnings,
    failureCount: failures,
    candidateCount: validCount(manifest.candidate_count),
    attemptedCount: validCount(manifest.attempted_count),
    skippedCount: validCount(manifest.skipped_count),
  };
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

export async function fetchRealPatch({ ra, dec, fov = 5, from = '', to = '', band = 4, signal, fetchImpl = fetch }) {
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
    response = await fetchImpl('/api/sequence', {
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

export async function loadDemoPatch({ ra = WISE_0855.ra, dec = WISE_0855.dec, signal, fetchImpl = fetch } = {}) {
  const coords = parseCoordinates(ra, dec);
  if (!coords.isValid) throw new PatchDataError('invalid_request', coords.raError || coords.decError);
  if (!coords.isWiseExample) {
    throw new PatchDataError('demo_target_unavailable', 'The synthetic example exists only near WISE 0855.');
  }
  let response;
  try {
    response = await fetchImpl(DEMO_MANIFEST_URL, { signal });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new PatchDataError('demo_unavailable', 'Could not open the bundled example.', error);
  }
  if (!response.ok) throw new PatchDataError('demo_unavailable', 'Could not open the bundled example.');
  let manifest;
  try {
    manifest = await response.json();
  } catch (error) {
    throw new PatchDataError('demo_unavailable', 'The bundled example is incomplete.', error);
  }
  if (!manifest?.note?.includes('SYNTHETIC') || !Array.isArray(manifest.dates)) {
    throw new PatchDataError('demo_unavailable', 'The bundled example is incomplete.');
  }
  const dates = manifest.dates.map(date => ({
    id: date.id,
    observedAt: `${date.id}T${date.time_utc}:00Z`,
    timeUtc: date.time_utc,
    visit: date.visit,
  }));
  const frames = [];
  const bands = [];
  for (const [number, data] of Object.entries(manifest.bands)) {
    const info = BAND_INFO.find(band => band.number === Number(number));
    if (!info) continue;
    bands.push({ ...info, rangeUm: data.range_um, approximateRange: true });
    for (const [dateId, frame] of Object.entries(data.frames)) {
      frames.push({
        dateId,
        band: info.number,
        wavelengthUm: frame.wavelength_um,
        imageUrl: `${DEMO_FRAMES_URL}${frame.file}`,
        flags: cleanFlags(frame.flags),
        observationId: '',
        pipelineVersion: '',
        archiveUrl: '',
        moverPct: frame.mover_pct,
        variablePct: frame.variable_pct,
      });
    }
  }
  return {
    source: 'synthetic',
    status: 'ok',
    target: { raDeg: WISE_0855.raDeg, decDeg: WISE_0855.decDeg, name: WISE_0855.name },
    dates,
    bands,
    frames,
    sizePx: manifest.size_px,
    pixelScaleArcsec: manifest.pixel_arcsec,
    fovMaxArcmin: manifest.fov_arcmin,
    orientation: manifest.orientation,
    releaseDate: '',
  };
}
