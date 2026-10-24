"""mesh.py — triangle meshes as numpy arrays: STL read/write (binary and ASCII), boxes, transforms,
bounding box, volume and area.
Version: 2026.10.24.2

A mesh is a float64 array of shape (n, 3, 3): n triangles, 3 vertices, xyz in mm.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def read_stl(path: str | Path) -> np.ndarray:
    """Triangles from a binary or ASCII STL."""
    data = Path(path).read_bytes()
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + 50 * n == len(data):
            rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
                                count=n, offset=84)
            return rec["v"].astype(np.float64)
    text = data.decode("ascii", errors="replace")
    verts = [list(map(float, line.split()[1:4])) for line in text.splitlines()
             if line.strip().startswith("vertex")]
    if not verts or len(verts) % 3:
        raise ValueError(f"{path}: not a valid STL (binary size mismatch, or no ASCII facets)")
    return np.asarray(verts, dtype=np.float64).reshape(-1, 3, 3)


def normals(tri: np.ndarray, unit: bool = True) -> np.ndarray:
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    if unit:
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        n = np.divide(n, ln, out=np.zeros_like(n), where=ln > 0)
    return n


def write_stl(path: str | Path, tri: np.ndarray, name: str = "p3dt") -> Path:
    """Binary STL (float32), deterministic bytes for the same triangles."""
    tri = np.asarray(tri, dtype=np.float64)
    rec = np.zeros(len(tri), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    rec["n"] = normals(tri)
    rec["v"] = tri
    header = name.encode("ascii", "replace")[:80].ljust(80, b" ")
    p = Path(path)
    p.write_bytes(header + struct.pack("<I", len(tri)) + rec.tobytes())
    return p


def box(sx: float, sy: float, sz: float, origin=(0.0, 0.0, 0.0)) -> np.ndarray:
    """A closed box [origin, origin + size], outward normals (12 triangles)."""
    x0, y0, z0 = origin
    x1, y1, z1 = x0 + sx, y0 + sy, z0 + sz
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], dtype=np.float64)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v[np.array(f)]


def bbox(tri: np.ndarray) -> np.ndarray:
    """[[xmin, ymin, zmin], [xmax, ymax, zmax]]"""
    pts = tri.reshape(-1, 3)
    return np.array([pts.min(axis=0), pts.max(axis=0)])


def volume(tri: np.ndarray) -> float:
    """Signed volume (divergence theorem); positive for a closed mesh with outward normals."""
    return float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6.0)


def area(tri: np.ndarray) -> float:
    return float(np.linalg.norm(normals(tri, unit=False), axis=1).sum() / 2.0)


def rotation(rx: float = 0.0, ry: float = 0.0, rz: float = 0.0) -> np.ndarray:
    """3x3 rotation, degrees, applied about X, then Y, then Z (R = Rz @ Ry @ Rx)."""
    a, b, c = np.radians([rx, ry, rz])
    Rx = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    Ry = np.array([[np.cos(b), 0, np.sin(b)], [0, 1, 0], [-np.sin(b), 0, np.cos(b)]])
    Rz = np.array([[np.cos(c), -np.sin(c), 0], [np.sin(c), np.cos(c), 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def transform(tri: np.ndarray, R: np.ndarray | None = None, t=(0.0, 0.0, 0.0)) -> np.ndarray:
    out = tri if R is None else tri @ np.asarray(R).T
    return out + np.asarray(t, dtype=np.float64)


def place(tri: np.ndarray, center_xy) -> np.ndarray:
    """Translates so the bounding box is centred on (x, y) and its bottom is on z = 0."""
    lo, hi = bbox(tri)
    return transform(tri, None, (center_xy[0] - (lo[0] + hi[0]) / 2, center_xy[1] - (lo[1] + hi[1]) / 2, -lo[2]))
