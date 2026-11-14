// The boundary between the sky viewer and its data sources. The demo is a
// bundled illustration; only /api/patch is permitted to return real images.

export const WISE_0855 = Object.freeze({
  name: 'WISE 0855',
  ra: '08h55m10.831s',
  dec: '-07d14m42.53s',
  raDeg: 133.79512917,
  decDeg: -7.24514722,
});

const DEMO_MANIFEST_URL = './public/demo/frames.json';
const DEMO_FRAMES_URL = './public/demo/frames/';

const BAND_INFO = [
  { number: 1, rangeUm: [0.75, 1.09], color: '#7aa7ff' },
  { number: 2, rangeUm: [1.10, 1.62], color: '#4fc3d9' },
  { number: 3, rangeUm: [1.63, 2.41], color: '#6dd08a' },
  { number: 4, rangeUm: [2.42, 3.82], color: '#e2c54a' },
  { number: 5, rangeUm: [3.83, 4.41], color: '#f2994a' },
  { number: 6, rangeUm: [4.42, 5.0], color: '#f0776c' },
];

export class PatchDataError extends Error {
  constructor(code, message, cause) {
    super(message, cause ? { cause } : undefined);
    this.name = 'PatchDataError';
    this.code = code;
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

function isSafeImagePath(url) {
  return typeof url === 'string' && url.length > 0 &&
    !url.startsWith('//') && !url.includes('\\') &&
    (url.startsWith('/') || url.startsWith('./') || url.startsWith('public/'));
}

function cleanFlags(flags) {
  if (!Array.isArray(flags)) return [];
  return flags.filter(flag => flag && typeof flag.label === 'string').map(flag => ({
    code: String(flag.code ?? 'quality'),
    label: flag.label,
  }));
}

function normalizeFrame(frame, knownDates) {
  if (!frame || typeof frame !== 'object' ||
      typeof frame.dateId !== 'string' || !knownDates.has(frame.dateId) ||
      !Number.isInteger(frame.band) || frame.band < 1 || frame.band > 6 ||
      !isSafeImagePath(frame.imageUrl)) {
    throw new PatchDataError('invalid_response', 'The archive returned incomplete image metadata.');
  }
  const wavelengthUm = frame.wavelengthUm == null || frame.wavelengthUm === ''
    ? NaN : Number(frame.wavelengthUm);
  return {
    dateId: frame.dateId,
    band: frame.band,
    wavelengthUm: Number.isFinite(wavelengthUm) ? wavelengthUm : null,
    imageUrl: frame.imageUrl,
    flags: cleanFlags(frame.flags),
    observationId: String(frame.observationId ?? ''),
    pipelineVersion: String(frame.pipelineVersion ?? ''),
    archiveUrl: typeof frame.archiveUrl === 'string' && /^https:\/\//.test(frame.archiveUrl)
      ? frame.archiveUrl : '',
  };
}

export function normalizeRealPatch(payload, requestedTarget) {
  if (!payload || typeof payload !== 'object') {
    throw new PatchDataError('invalid_response', 'The archive returned a response we could not read.');
  }
  if (payload.status === 'unavailable') {
    throw new PatchDataError('unavailable', String(payload.message || 'The archive is unavailable right now.'));
  }
  if (payload.status !== 'ok' && payload.status !== 'empty') {
    throw new PatchDataError('invalid_response', 'The archive returned an unknown result.');
  }
  if (payload.source !== 'real') {
    throw new PatchDataError('invalid_response', 'The archive did not verify these images as real observations.');
  }
  const target = {
    raDeg: Number(payload.target?.raDeg ?? requestedTarget.raDeg),
    decDeg: Number(payload.target?.decDeg ?? requestedTarget.decDeg),
    name: String(payload.target?.name ?? 'Your patch'),
  };
  if (!Number.isFinite(target.raDeg) || !Number.isFinite(target.decDeg)) {
    throw new PatchDataError('invalid_response', 'The archive returned invalid coordinates.');
  }
  const dates = Array.isArray(payload.dates) ? payload.dates.map(date => {
    if (!date || typeof date.id !== 'string' || !isoDate(date.id.slice(0, 10)) ||
        typeof date.observedAt !== 'string' || Number.isNaN(Date.parse(date.observedAt))) {
      throw new PatchDataError('invalid_response', 'The archive returned an invalid observation date.');
    }
    return {
      id: date.id,
      observedAt: date.observedAt,
      timeUtc: new Date(date.observedAt).toISOString().slice(11, 16),
      visit: date.visit ?? null,
    };
  }) : [];
  const knownDates = new Set(dates.map(date => date.id));
  if (knownDates.size !== dates.length || !Array.isArray(payload.frames)) {
    throw new PatchDataError('invalid_response', 'The archive returned inconsistent image dates.');
  }
  const frames = payload.frames.map(frame => normalizeFrame(frame, knownDates));
  if (payload.status === 'empty' && frames.length || payload.status === 'ok' && !frames.length) {
    throw new PatchDataError('invalid_response', 'The archive returned inconsistent image counts.');
  }
  const usedBands = new Set(frames.map(frame => frame.band));
  const responseBands = Array.isArray(payload.bands) ? payload.bands : [];
  const bands = BAND_INFO.filter(info => usedBands.has(info.number)).map(info => {
    const fromServer = responseBands.find(band => band.number === info.number);
    return {
      ...info,
      rangeUm: Array.isArray(fromServer?.rangeUm) && fromServer.rangeUm.length === 2 &&
        fromServer.rangeUm.every(Number.isFinite) ? fromServer.rangeUm : info.rangeUm,
    };
  });
  return {
    source: 'real',
    status: payload.status,
    target,
    dates: dates.sort((a, b) => a.observedAt.localeCompare(b.observedAt)),
    bands,
    frames,
    sizePx: Number(payload.sizePx) || null,
    pixelScaleArcsec: Number(payload.pixelScaleArcsec) || null,
    fovMaxArcmin: Number(payload.fovMaxArcmin) || 20,
    orientation: String(payload.orientation || 'North up, East left'),
    releaseDate: isoDate(payload.releaseDate) ? payload.releaseDate : '',
  };
}

export async function fetchRealPatch({ ra, dec, fov = 10, from = '', to = '', signal, fetchImpl = fetch }) {
  const coords = parseCoordinates(ra, dec);
  if (!coords.isValid) {
    throw new PatchDataError('invalid_request', coords.raError || coords.decError);
  }
  const [first, last] = orderedRange(from, to);
  const query = new URLSearchParams({
    ra: String(coords.raDeg), dec: String(coords.decDeg), fov: String(positiveFov(fov)),
  });
  if (first) query.set('from', first);
  if (last) query.set('to', last);
  let response;
  try {
    response = await fetchImpl(`/api/patch?${query}`, { signal, headers: { Accept: 'application/json' } });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new PatchDataError('unavailable', 'Could not reach the SPHEREx data service. Try again shortly.', error);
  }
  if (!response.ok) {
    let message = '';
    try {
      const failure = await response.json();
      if (typeof failure?.message === 'string') message = failure.message;
    } catch { /* A proxy can return an HTML error page. */ }
    throw new PatchDataError('unavailable', message || `The SPHEREx data service returned HTTP ${response.status}.`);
  }
  let payload;
  try {
    payload = await response.json();
  } catch (error) {
    throw new PatchDataError('invalid_response', 'The SPHEREx data service did not return JSON.', error);
  }
  return normalizeRealPatch(payload, coords);
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
    bands.push({ ...info, rangeUm: data.range_um });
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
