"""Read calibrated SPHEREx IMAGE, FLAGS and WCS-WAVE from official S3 parents.

The FITS range and compressed row-table strategy comes from Henry Lin's
Ingress backend (henrylin1039/Ingress, ab96faf, backend/spherex.py). This port
validates bounded reads and leaves science values and quality flags intact.
"""

from io import BytesIO
from math import isfinite, prod
from operator import index
import re
from threading import local
from time import monotonic
from urllib.parse import urlsplit

from astropy import units as u
from astropy.io import fits
from astropy.wcs import WCS
import numpy as np
from requests import Session

from saved_sources import _trusted_source


S3_ROOT = "https://nasa-irsa-spherex.s3.amazonaws.com/"
IBE_PREFIX = "/ibe/data/spherex/"
FITS_BLOCK = 2880
MAX_HEADER = 64 * FITS_BLOCK
MAX_RANGE = 16 * 1024 * 1024
MAX_PRODUCT = 512 * 1024 * 1024
FLAG_NAMES = {
    0: "TRANSIENT", 1: "OVERFLOW", 2: "SUR_ERROR", 6: "NONFUNC", 7: "DICHROIC",
    9: "MISSING_DATA", 10: "HOT", 11: "COLD", 12: "FULLSAMPLE", 14: "PHANMISS",
    15: "NONLINEAR", 17: "PERSIST", 19: "OUTLIER", 20: "CROSSTALK",
    21: "SOURCE", 22: "GHOST", 23: "GHOST_FPA", 24: "GHOST_EXT", 25: "STREAK",
    26: "BLOOM", 27: "SNOWBALL", 28: "HALO",
}
BAD_FLAG_BITS = (0, 2, 6, 9, 10, 11, 17, 27)
BAD_FLAG_MASK = sum(1 << bit for bit in BAD_FLAG_BITS)
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}")
_BYTE_DESCRIPTOR = re.compile(r"1?PB(?:\([0-9]+\))?")
_local = local()


class ParentReadUnavailable(ValueError):
    """The public parent did not provide an intact, recognized FITS range."""


def _session():
    if not hasattr(_local, "session"):
        _local.session = Session()
    return _local.session


def _bucket_product(source_url):
    if not _trusted_source(source_url):
        raise ParentReadUnavailable("The archive parent URL is not trusted.")
    path = urlsplit(source_url).path.removeprefix(IBE_PREFIX)
    if not all(_PATH_SEGMENT.fullmatch(part) for part in path.split("/")):
        raise ParentReadUnavailable("The archive parent path is invalid.")
    return S3_ROOT + path


class _RangeReader:
    def __init__(self, source_url, deadline):
        self.url = _bucket_product(source_url)
        self.deadline = deadline
        self.size = None
        self.etag = None

    def read(self, start, stop, *, suffix=False):
        """Read a half-open range, or the last `stop` bytes when suffix is set."""
        if not 0 <= start < stop or stop - start > MAX_RANGE:
            raise ParentReadUnavailable("FITS range exceeds the supported read size.")
        if self.size is not None and start >= self.size:
            raise ParentReadUnavailable("FITS range starts outside the product.")
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("The FITS range budget was exhausted.")
        headers = {
            "Range": f"bytes=-{stop}" if suffix else f"bytes={start}-{stop - 1}",
            "Accept-Encoding": "identity",
        }
        if self.etag:
            headers["If-Match"] = self.etag
        with _session().get(
            self.url, headers=headers, allow_redirects=False, stream=True,
            timeout=(min(4, remaining / 2), min(7, remaining / 2)),
        ) as response:
            content_range = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
            if response.status_code != 206 or not content_range:
                raise ParentReadUnavailable(f"The S3 parent rejected a FITS range (HTTP {response.status_code}).")
            first, last, total = map(int, content_range.groups())
            expected = (max(0, total - stop), total - 1) if suffix else (start, min(total, stop) - 1)
            etag = response.headers.get("ETag")
            if (not 0 < total <= MAX_PRODUCT or total % FITS_BLOCK or (first, last) != expected
                    or not 0 <= first <= last < total or (self.size is not None and total != self.size)
                    or (self.etag and etag != self.etag)
                    or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                raise ParentReadUnavailable("S3 returned an inconsistent FITS range.")
            length = last - first + 1
            advertised_length = response.headers.get("Content-Length")
            if advertised_length is not None and advertised_length != str(length):
                raise ParentReadUnavailable("S3 returned the wrong FITS range length.")
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if monotonic() >= self.deadline:
                    raise TimeoutError("The FITS range budget was exhausted.")
                if len(data) + len(chunk) > length:
                    raise ParentReadUnavailable("S3 returned bytes outside the requested FITS range.")
                data.extend(chunk)
            if len(data) != length or monotonic() >= self.deadline:
                raise ParentReadUnavailable("S3 did not finish returning the requested FITS range.")
        self.size = total
        self.etag = etag
        return bytes(data)


def _layout(data, offset=0):
    """Return (header, padded header stop, padded data size) when END is present."""
    if data[offset:offset + 8] not in (b"SIMPLE  ", b"XTENSION"):
        raise ParentReadUnavailable("The FITS header does not start at the expected HDU.")
    for stop in range(offset + FITS_BLOCK, min(len(data), offset + MAX_HEADER) + 1, FITS_BLOCK):
        block = data[stop - FITS_BLOCK:stop]
        if not any(block[i:i + 8] == b"END     " for i in range(0, FITS_BLOCK, 80)):
            continue
        header = fits.Header.fromstring(data[offset:stop])
        axes, bitpix = header["NAXIS"], header["BITPIX"]
        if type(axes) is not int or not 0 <= axes <= 4 or bitpix not in (8, 16, 32, 64, -32, -64):
            raise ParentReadUnavailable("The FITS HDU dimensions or encoding are unsupported.")
        dimensions = [header[f"NAXIS{i}"] for i in range(1, axes + 1)]
        extra, groups = header.get("PCOUNT", 0), header.get("GCOUNT", 1)
        if (any(type(dim) is not int or dim < 0 for dim in dimensions)
                or type(extra) is not int or extra < 0 or groups != 1):
            raise ParentReadUnavailable("Invalid FITS HDU dimensions.")
        size = (abs(bitpix) // 8) * (prod(dimensions) + extra) if axes else 0
        padded = size + (-size % FITS_BLOCK)
        if padded > MAX_PRODUCT:
            raise ParentReadUnavailable("FITS HDU exceeds the supported layout size.")
        return header, stop, padded
    return None


def _header(reader, start, initial=b"", *, first_read=5 * FITS_BLOCK):
    buf = initial
    while True:
        if buf:
            layout = _layout(buf)
            if layout:
                header, header_size, data_size = layout
                data_start = start + header_size
                if data_start + data_size > reader.size:
                    raise ParentReadUnavailable("A FITS HDU extends beyond the product.")
                return header, data_start, data_size, buf[header_size:]
        if len(buf) >= MAX_HEADER or (reader.size is not None and start + len(buf) >= reader.size):
            raise ParentReadUnavailable("The FITS header had no valid END card within the read budget.")
        extra = first_read if not buf else 5 * FITS_BLOCK
        buf += reader.read(start + len(buf), start + min(MAX_HEADER, len(buf) + extra))


def _flag_rows(reader, start, nx, ny, y0, y1):
    hdr, table, _, prefix = _header(reader, start, first_read=12 * FITS_BLOCK)
    if (hdr.get("EXTNAME") != "FLAGS" or hdr.get("XTENSION", "").strip() != "BINTABLE"
            or hdr.get("BITPIX") != 8 or hdr.get("NAXIS") != 2 or hdr.get("ZIMAGE") is not True
            or hdr.get("ZNAXIS") != 2 or hdr.get("ZBITPIX") != 32
            or (hdr.get("ZNAXIS1"), hdr.get("ZNAXIS2")) != (nx, ny)
            or (hdr.get("ZTILE1"), hdr.get("ZTILE2")) != (nx, 1)
            or hdr.get("NAXIS1") != 8 or hdr.get("NAXIS2") != ny or hdr.get("TFIELDS") != 1
            or hdr.get("TTYPE1") != "COMPRESSED_DATA"
            or not _BYTE_DESCRIPTOR.fullmatch(hdr.get("TFORM1", ""))):
        raise ParentReadUnavailable("The archive parent's FLAGS layout is unsupported.")

    table_size = ny * 8
    heap_offset = hdr.get("THEAP", table_size)
    data_size = table_size + hdr["PCOUNT"]
    if type(heap_offset) is not int or not table_size <= heap_offset < data_size:
        raise ParentReadUnavailable("The compressed FLAGS heap is invalid.")
    first_row, last_row = y0 * 8, y1 * 8
    raw_table = (prefix[first_row:last_row] if len(prefix) >= last_row else
                 reader.read(table + first_row, table + last_row))
    entries = np.frombuffer(raw_table, dtype=">i4").reshape(y1 - y0, 2)
    lengths, offsets = entries.T.astype(np.int64)
    last = offsets + lengths
    if (np.any(lengths <= 0) or np.any(offsets < 0) or int(last.max()) > data_size - heap_offset):
        raise ParentReadUnavailable("A compressed FLAGS row is outside its heap.")
    first, stop = int(offsets.min()), int(last.max())
    heap = reader.read(table + heap_offset + first, table + heap_offset + stop)
    rows = entries.copy()
    rows[:, 1] -= first

    # Reassemble only the selected row tiles for Astropy's lossless decoder.
    mini = hdr.copy()
    mini["NAXIS2"] = mini["ZNAXIS2"] = y1 - y0
    mini["PCOUNT"] = len(heap)
    for key in ("THEAP", "CHECKSUM", "DATASUM"):
        mini.remove(key, ignore_missing=True)
    payload = rows.tobytes() + heap
    raw = (fits.PrimaryHDU().header.tostring().encode("ascii")
           + mini.tostring().encode("ascii") + payload + bytes(-len(payload) % FITS_BLOCK))
    with fits.open(BytesIO(raw), memmap=False) as hdus:
        flags = np.asarray(hdus[1].data, dtype=np.int64)
        if flags.shape != (y1 - y0, nx):
            raise ParentReadUnavailable("Decoded FLAGS rows have the wrong shape.")
        return flags


def read_parent(source_url, want, *, deadline):
    """Return original header, science, flags, x0, y0. `want` gives half-open bounds."""
    reader = _RangeReader(source_url, deadline)
    head = reader.read(0, 11 * FITS_BLOCK)
    primary, start, size, prefix = _header(reader, 0, head)
    if primary.get("SIMPLE") is not True or size:
        raise ParentReadUnavailable("The SPHEREx parent primary HDU is unsupported.")
    hdr, image, size, _ = _header(reader, start, prefix)
    nx, ny = hdr.get("NAXIS1", 0), hdr.get("NAXIS2", 0)
    if (hdr.get("EXTNAME") != "IMAGE" or hdr.get("XTENSION", "").strip() != "IMAGE"
            or hdr.get("NAXIS") != 2 or hdr.get("BITPIX") != -32 or hdr.get("PCOUNT", 0)
            or not 1 <= nx <= 4096 or not 1 <= ny <= 4096
            or hdr.get("BSCALE", 1) != 1 or hdr.get("BZERO", 0) != 0):
        raise ParentReadUnavailable("The archive parent's IMAGE layout is unsupported.")
    x0, x1, y0, y1 = (index(value) for value in want(hdr))
    if not (0 <= x0 < x1 <= nx and 0 <= y0 < y1 <= ny):
        raise ParentReadUnavailable("The requested sky patch is outside the parent IMAGE.")
    row = nx * 4
    raw_science = reader.read(image + y0 * row, image + y1 * row)
    science = np.frombuffer(raw_science, dtype=">f4").reshape(y1 - y0, nx)[:, x0:x1].astype(np.float64)
    flags = _flag_rows(reader, image + size, nx, ny, y0, y1)[:, x0:x1].copy()
    return hdr, science, flags, x0, y0


def read_wavelength(source_url, x, y, header, *, deadline):
    """Evaluate the parent's alternate WCS-WAVE table at original 0-based pixels."""
    if not (isfinite(x) and isfinite(y) and -0.5 <= x < header["NAXIS1"] - 0.5
            and -0.5 <= y < header["NAXIS2"] - 0.5):
        raise ParentReadUnavailable("The target is outside the wavelength calibration grid.")
    reader = _RangeReader(source_url, deadline)
    tail = reader.read(0, 8 * FITS_BLOCK, suffix=True)
    for start in range(len(tail) - FITS_BLOCK, -1, -FITS_BLOCK):
        if tail[start:start + 8] != b"XTENSION":
            continue
        layout = _layout(tail, start)
        if not layout:
            continue
        hdr, data_start, size = layout
        if (hdr.get("EXTNAME") != "WCS-WAVE" or hdr.get("XTENSION", "").strip() != "BINTABLE"
                or data_start + size != len(tail)):
            continue
        mini_fits = fits.PrimaryHDU().header.tostring().encode("ascii") + tail[start:]
        with fits.open(BytesIO(mini_fits), memmap=False) as hdus:
            wcs = WCS(header, fobj=hdus, key="W")
            wcs.sip = None
            wave, bandwidth = (float(value.to_value(u.um)) for value in wcs.pixel_to_world(x, y))
        if not (isfinite(wave) and isfinite(bandwidth) and wave > 0 and bandwidth > 0):
            raise ParentReadUnavailable("The target wavelength calibration is invalid.")
        return wave, bandwidth
    raise ParentReadUnavailable("The parent FITS tail did not contain a readable WCS-WAVE table.")
