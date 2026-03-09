from __future__ import annotations

import math
from typing import Iterable

from app.models.state import AirfoilState, Planform2D, WingMesh, WingParams


def build_wing_mesh(airfoil: AirfoilState, params: WingParams) -> tuple[WingMesh, Planform2D]:
    if not airfoil.upper or not airfoil.lower:
        raise ValueError('Airfoil is empty. SetAirfoil before BuildWingMesh.')

    profile = _closed_profile(airfoil.upper, airfoil.lower)

    span = max(0.05, float(params.span_m))
    ar = max(1.2, float(params.aspect_ratio))
    taper = max(0.1, min(1.2, float(params.taper_ratio)))
    sweep = math.radians(float(params.sweep_deg))
    dihedral = math.radians(float(params.dihedral_deg))
    twist_tip = math.radians(float(params.twist_deg))

    area = span * span / ar
    c_root = (2.0 * area) / (span * (1.0 + taper))
    c_tip = c_root * taper
    semi = span * 0.5
    tip_blend = 0.88
    tip_end_chord = max(c_tip * 0.32, c_root * 0.06)

    vertices: list[list[float]] = []
    triangles: list[list[int]] = []
    pressure_overlay: list[float] = []

    y_root = 0.0
    x_root = 0.0
    z_root = 0.0

    root_ring = _section_ring(
        profile=profile,
        chord=c_root,
        y=y_root,
        x_offset=x_root,
        z_offset=z_root,
        twist=0.0,
    )
    _append_ring(vertices, pressure_overlay, root_ring, span)

    root_start = 0
    n = len(profile)

    for side in (-1.0, 1.0):
        y_tip_mid = side * semi * tip_blend
        y_tip_end = side * semi

        x_tip_mid = abs(y_tip_mid) * math.tan(sweep)
        x_tip_end = abs(y_tip_end) * math.tan(sweep)

        z_tip_mid = abs(y_tip_mid) * math.tan(dihedral)
        z_tip_end = abs(y_tip_end) * math.tan(dihedral)

        twist_mid = twist_tip * tip_blend

        tip_mid_ring = _section_ring(
            profile=profile,
            chord=c_tip,
            y=y_tip_mid,
            x_offset=x_tip_mid,
            z_offset=z_tip_mid,
            twist=twist_mid,
        )
        tip_end_ring = _section_ring(
            profile=profile,
            chord=tip_end_chord,
            y=y_tip_end,
            x_offset=x_tip_end,
            z_offset=z_tip_end,
            twist=twist_tip,
            thickness_scale=0.55,
        )

        base = len(vertices)
        _append_ring(vertices, pressure_overlay, tip_mid_ring, span)
        _append_ring(vertices, pressure_overlay, tip_end_ring, span)

        mid_start = base
        end_start = base + n
        _append_strip(triangles, root_start, mid_start, n)
        _append_strip(triangles, mid_start, end_start, n)

        _cap_ring(vertices, triangles, end_start, n, reverse=(side > 0), pressure_overlay=pressure_overlay)

    _finalize_pressure_len(pressure_overlay, len(vertices))

    planform = _build_planform(c_root, c_tip, semi, sweep)

    return (
        WingMesh(
            vertices=[[round(v[0], 6), round(v[1], 6), round(v[2], 6)] for v in vertices],
            triangles=triangles,
            pressure_overlay=[round(float(x), 6) for x in pressure_overlay],
        ),
        planform,
    )


def _closed_profile(upper: list[list[float]], lower: list[list[float]]) -> list[list[float]]:
    # profile loop from TE upper -> LE -> TE lower
    up = [list(p) for p in upper[::-1]]
    lo = [list(p) for p in lower[1:]]
    profile = up + lo
    if len(profile) < 6:
        raise ValueError('Airfoil profile has insufficient points')
    return profile


def _section_ring(
    profile: Iterable[Iterable[float]],
    chord: float,
    y: float,
    x_offset: float,
    z_offset: float,
    twist: float,
    thickness_scale: float = 1.0,
) -> list[list[float]]:
    ring: list[list[float]] = []
    ct = math.cos(twist)
    st = math.sin(twist)

    for x_norm, z_norm in profile:
        x_local = (float(x_norm) - 0.25) * chord
        z_local = float(z_norm) * chord * max(0.2, thickness_scale)

        x_tw = x_local * ct - z_local * st
        z_tw = x_local * st + z_local * ct

        ring.append([x_tw + x_offset, y, z_tw + z_offset])

    return ring


def _append_strip(triangles: list[list[int]], a_start: int, b_start: int, n: int) -> None:
    for i in range(n):
        i2 = (i + 1) % n
        a0 = a_start + i
        a1 = a_start + i2
        b0 = b_start + i
        b1 = b_start + i2
        triangles.append([a0, b0, a1])
        triangles.append([a1, b0, b1])


def _append_ring(
    vertices: list[list[float]],
    pressure_overlay: list[float],
    ring: Iterable[Iterable[float]],
    span: float,
) -> None:
    for p in ring:
        point = [float(p[0]), float(p[1]), float(p[2])]
        vertices.append(point)
        pressure_overlay.append(_mock_pressure(point[0], point[1], point[2], span))


def _cap_ring(
    vertices: list[list[float]],
    triangles: list[list[int]],
    start: int,
    n: int,
    reverse: bool,
    pressure_overlay: list[float],
) -> None:
    cx = 0.0
    cy = 0.0
    cz = 0.0
    for i in range(n):
        v = vertices[start + i]
        cx += v[0]
        cy += v[1]
        cz += v[2]
    cx /= n
    cy /= n
    cz /= n

    c_idx = len(vertices)
    vertices.append([cx, cy, cz])
    pressure_overlay.append(_mock_pressure(cx, cy, cz, max(1.0, abs(cy) * 2.0)))

    for i in range(n):
        i2 = (i + 1) % n
        a = start + i
        b = start + i2
        if reverse:
            triangles.append([c_idx, b, a])
        else:
            triangles.append([c_idx, a, b])


def _build_planform(c_root: float, c_tip: float, semi: float, sweep: float) -> Planform2D:
    dx = abs(semi) * math.tan(sweep)
    right_poly = [
        [0.0, 0.0],
        [c_root, 0.0],
        [dx + c_tip, semi],
        [dx, semi],
    ]
    left_poly = [[x, -y] for x, y in reversed(right_poly)]

    q_right = [[0.25 * c_root, 0.0], [dx + 0.25 * c_tip, semi]]
    q_left = [[0.25 * c_root, 0.0], [dx + 0.25 * c_tip, -semi]]

    return Planform2D(
        polygon=[[round(x, 6), round(y, 6)] for x, y in right_poly + left_poly],
        quarter_chord=[[round(x, 6), round(y, 6)] for x, y in q_left + q_right],
    )


def _mock_pressure(x: float, y: float, z: float, span: float) -> float:
    y_norm = 1.0 - min(1.0, abs(y) / max(0.1, span * 0.5))
    x_term = math.exp(-((x - 0.05) ** 2) / 0.08)
    z_term = 1.0 + 0.2 * z
    return 0.4 + 0.6 * y_norm * x_term * z_term


def _finalize_pressure_len(overlay: list[float], target: int) -> None:
    if len(overlay) >= target:
        del overlay[target:]
        return
    overlay.extend([overlay[-1] if overlay else 0.5] * (target - len(overlay)))


