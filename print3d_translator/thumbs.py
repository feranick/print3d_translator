"""thumbs.py — the plate pictures a Bambu .gcode.3mf carries, rendered from our own placed meshes (the
OrcaSlicer 2.4.2 CLI cannot render them), as OrcaSlicer's GUI export has them (spike S4):
Version: 2026.10.24.9

  plate_1.png          isometric view of the parts, zoomed to fit, lit (what the printer and Handy show)
  plate_1_small.png    the same, 128 x 128
  plate_no_light_1.png the isometric view, flat colours
  top_1.png            top orthographic view of the whole bed (the printer draws skip-object picks on it)
  pick_1.png           the top view with each object painted in a colour that encodes its identify_id
                       (R = id & 255, G = id >> 8 & 255, B = id >> 16 & 255, A = 255), transparent elsewhere

512 x 512 RGBA (the small one 128 x 128). Pure numpy z-buffer rasterizer; PNGs written with zlib.
"""
from __future__ import annotations

import math
import struct
import zlib

import numpy as np

SIZE = 512
LIGHT = np.array([-0.35, -0.55, 0.76])
LIGHT = LIGHT / np.linalg.norm(LIGHT)


def png(rgba: np.ndarray) -> bytes:
    """(h, w, 4) uint8 -> PNG bytes (colour type 6, filter 0)."""
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))

    def chunk(t: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _hex(c: str) -> np.ndarray:
    c = (c or "#808080").lstrip("#")
    return np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)], dtype=float)


def _raster(objects: list[dict], to_view, fit_lo, fit_hi, size: int, cull_up: bool) -> dict:
    """z-buffer render. to_view(points (n,3)) -> view coordinates (x right, y up, z toward the viewer).
    fit_lo/fit_hi: the view-space (x, y) box mapped onto the image (kept square, centred)."""
    span = max(fit_hi[0] - fit_lo[0], fit_hi[1] - fit_lo[1], 1e-9)
    scale = (size - 1) / span
    ox = (size - 1 - (fit_hi[0] - fit_lo[0]) * scale) / 2
    oy = (size - 1 - (fit_hi[1] - fit_lo[1]) * scale) / 2
    zbuf = np.full((size, size), -np.inf)
    lit = np.zeros((size, size, 3))
    flat = np.zeros((size, size, 3))
    ids = np.zeros((size, size), dtype=np.int64)
    alpha = np.zeros((size, size), dtype=np.uint8)
    for o in objects:
        tri = np.asarray(o["tri"], dtype=float)
        col = _hex(o.get("color"))
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(n, axis=1)
        ok = ln > 0
        tri, n = tri[ok], n[ok] / ln[ok, None]
        V = to_view(tri.reshape(-1, 3)).reshape(-1, 3, 3)
        vn = to_view(n + 0.0, normal=True)
        facing = vn[:, 2] > 1e-9 if cull_up else np.ones(len(V), dtype=bool)
        V, n = V[facing], n[facing]
        lam = 0.30 + 0.70 * np.clip(n @ LIGHT, 0, 1)
        px = ox + (V[:, :, 0] - fit_lo[0]) * scale
        py = (size - 1) - (oy + (V[:, :, 1] - fit_lo[1]) * scale)
        for k in range(len(V)):
            x, y, z = px[k], py[k], V[k, :, 2]
            i0, i1 = int(max(math.floor(x.min()), 0)), int(min(math.ceil(x.max()), size - 1))
            j0, j1 = int(max(math.floor(y.min()), 0)), int(min(math.ceil(y.max()), size - 1))
            if i1 < i0 or j1 < j0:
                continue
            d = (y[1] - y[2]) * (x[0] - x[2]) + (x[2] - x[1]) * (y[0] - y[2])
            if abs(d) < 1e-12:
                continue
            X, Y = np.meshgrid(np.arange(i0, i1 + 1) + 0.0, np.arange(j0, j1 + 1) + 0.0)
            w0 = ((y[1] - y[2]) * (X - x[2]) + (x[2] - x[1]) * (Y - y[2])) / d
            w1 = ((y[2] - y[0]) * (X - x[2]) + (x[0] - x[2]) * (Y - y[2])) / d
            w2 = 1 - w0 - w1
            inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            if not inside.any():
                continue
            zz = w0 * z[0] + w1 * z[1] + w2 * z[2]
            sub = zbuf[j0:j1 + 1, i0:i1 + 1]
            win = inside & (zz > sub)
            if not win.any():
                continue
            sub[win] = zz[win]
            lit[j0:j1 + 1, i0:i1 + 1][win] = col * lam[k]
            flat[j0:j1 + 1, i0:i1 + 1][win] = col
            ids[j0:j1 + 1, i0:i1 + 1][win] = int(o.get("identify_id") or 0)
            alpha[j0:j1 + 1, i0:i1 + 1][win] = 255

    def rgba(rgb):
        out = np.zeros((size, size, 4), dtype=np.uint8)
        out[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
        out[..., 3] = alpha
        return out

    pick = np.zeros((size, size, 4), dtype=np.uint8)
    pick[..., 0], pick[..., 1], pick[..., 2] = ids & 255, (ids >> 8) & 255, (ids >> 16) & 255
    pick[..., 3] = alpha
    return {"lit": rgba(lit), "flat": rgba(flat), "pick": pick}


def _iso():
    """View rotation: looking down at 35 degrees from the front-left, as OrcaSlicer's plate thumbnail."""
    az, el = math.radians(-35.0), math.radians(-55.0)
    Rz = np.array([[math.cos(az), -math.sin(az), 0], [math.sin(az), math.cos(az), 0], [0, 0, 1]])
    Rx = np.array([[1, 0, 0], [0, math.cos(el), -math.sin(el)], [0, math.sin(el), math.cos(el)]])
    R = Rx @ Rz
    return lambda p, normal=False: p @ R.T


def render(objects: list[dict], bed: np.ndarray, size: int = SIZE) -> dict:
    """objects: [{tri (placed, bed coordinates), color '#RRGGBB', identify_id}]. bed: (n, 2) printable area.
    Returns {'plate', 'plate_small', 'no_light', 'top', 'pick'}: PNG bytes."""
    lo, hi = bed.min(axis=0), bed.max(axis=0)
    top = _raster(objects, lambda p, normal=False: p.copy(), lo, hi, size, cull_up=True)
    view = _iso()
    pts = np.concatenate([np.asarray(o["tri"], dtype=float).reshape(-1, 3) for o in objects])
    vp = view(pts)
    vlo, vhi = vp[:, :2].min(axis=0), vp[:, :2].max(axis=0)
    pad = 0.08 * max(vhi - vlo)
    iso = _raster(objects, view, vlo - pad, vhi + pad, size, cull_up=True)
    small = iso["lit"].reshape(size // 4, 4, size // 4, 4, 4).mean(axis=(1, 3)).astype(np.uint8)
    return {"plate": png(iso["lit"]), "plate_small": png(small), "no_light": png(iso["flat"]),
            "top": png(top["lit"]), "pick": png(top["pick"])}
