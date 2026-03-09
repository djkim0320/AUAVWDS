from __future__ import annotations

import math
from typing import Iterable

import numpy as np


PRESET_TO_CODE = {
    'clark-y': '2412',
    'clarky': '2412',
    'naca2412': '2412',
    'sd7037': '3408',
    'naca0012': '0012',
}


def _cosine_x(n: int) -> np.ndarray:
    beta = np.linspace(0.0, math.pi, n)
    return 0.5 * (1.0 - np.cos(beta))


def _sanitize_naca_code(code: str) -> str:
    c = code.strip().lower().replace('naca', '').replace(' ', '').replace('-', '')
    c = PRESET_TO_CODE.get(c, c)
    if len(c) == 4 and c.isdigit():
        return c
        raise ValueError(f'지원하지 않는 에어포일 코드입니다: {code}')


def generate_naca4(code: str, n_points: int = 121) -> dict:
    naca = _sanitize_naca_code(code)
    m = int(naca[0]) / 100.0
    p = int(naca[1]) / 10.0
    t = int(naca[2:]) / 100.0

    x = _cosine_x(n_points)
    yt = 5.0 * t * (
        0.2969 * np.sqrt(np.maximum(x, 1e-9))
        - 0.1260 * x
        - 0.3516 * x**2
        + 0.2843 * x**3
        - 0.1015 * x**4
    )

    yc = np.zeros_like(x)
    dyc_dx = np.zeros_like(x)

    if m > 0 and p > 0:
        i1 = x < p
        i2 = ~i1
        yc[i1] = m / (p**2) * (2.0 * p * x[i1] - x[i1] ** 2)
        yc[i2] = m / ((1.0 - p) ** 2) * ((1.0 - 2.0 * p) + 2.0 * p * x[i2] - x[i2] ** 2)
        dyc_dx[i1] = 2.0 * m / (p**2) * (p - x[i1])
        dyc_dx[i2] = 2.0 * m / ((1.0 - p) ** 2) * (p - x[i2])

    theta = np.arctan(dyc_dx)
    xu = x - yt * np.sin(theta)
    yu = yc + yt * np.cos(theta)
    xl = x + yt * np.sin(theta)
    yl = yc - yt * np.cos(theta)

    upper = np.column_stack([xu, yu]).tolist()
    lower = np.column_stack([xl, yl]).tolist()
    camber = np.column_stack([x, yc]).tolist()

    # Closed loop coordinates starting from TE upper -> LE -> TE lower.
    coords = upper[::-1] + lower[1:]

    max_camber = float(np.max(yc))
    max_camber_idx = int(np.argmax(yc))

    return {
        'coords': _round2(coords),
        'upper': _round2(upper),
        'lower': _round2(lower),
        'camber': _round2(camber),
        'summary': {
            'code': f'NACA {naca}' if code.lower().strip() not in ('clark-y', 'clarky') else 'Clark Y (approx. NACA 2412)',
            'thickness_percent': round(t * 100.0, 3),
            'max_camber_percent': round(max_camber * 100.0, 3),
            'max_camber_x_percent': round(float(x[max_camber_idx]) * 100.0, 3),
        },
    }


def generate_custom_airfoil(
    max_camber_percent: float,
    max_camber_x_percent: float,
    thickness_percent: float,
    reflex_percent: float = 0.0,
    n_points: int = 121,
) -> dict:
    m = max(0.0, min(9.0, max_camber_percent)) / 100.0
    p = max(5.0, min(95.0, max_camber_x_percent)) / 100.0
    t = max(2.0, min(30.0, thickness_percent)) / 100.0
    reflex = max(-3.0, min(3.0, reflex_percent)) / 100.0

    x = _cosine_x(n_points)
    yt = 5.0 * t * (
        0.2969 * np.sqrt(np.maximum(x, 1e-9))
        - 0.1260 * x
        - 0.3516 * x**2
        + 0.2843 * x**3
        - 0.1015 * x**4
    )

    yc = np.zeros_like(x)
    dyc_dx = np.zeros_like(x)

    i1 = x < p
    i2 = ~i1
    if p > 0:
        yc[i1] = m / (p**2) * (2.0 * p * x[i1] - x[i1] ** 2)
        dyc_dx[i1] = 2.0 * m / (p**2) * (p - x[i1])
    if p < 1.0:
        yc[i2] = m / ((1.0 - p) ** 2) * ((1.0 - 2.0 * p) + 2.0 * p * x[i2] - x[i2] ** 2)
        dyc_dx[i2] = 2.0 * m / ((1.0 - p) ** 2) * (p - x[i2])

    # Optional gentle reflex near trailing edge.
    reflex_shape = (x**2) * (x - 1.0)
    yc += reflex * reflex_shape
    dyc_dx += reflex * (3.0 * x**2 - 2.0 * x)

    theta = np.arctan(dyc_dx)
    xu = x - yt * np.sin(theta)
    yu = yc + yt * np.cos(theta)
    xl = x + yt * np.sin(theta)
    yl = yc - yt * np.cos(theta)

    upper = np.column_stack([xu, yu]).tolist()
    lower = np.column_stack([xl, yl]).tolist()
    camber = np.column_stack([x, yc]).tolist()
    coords = upper[::-1] + lower[1:]

    max_camber = float(np.max(yc))
    max_camber_idx = int(np.argmax(yc))

    return {
        'coords': _round2(coords),
        'upper': _round2(upper),
        'lower': _round2(lower),
        'camber': _round2(camber),
        'summary': {
            'code': '커스텀 에어포일',
            'thickness_percent': round(t * 100.0, 3),
            'max_camber_percent': round(max_camber * 100.0, 3),
            'max_camber_x_percent': round(float(x[max_camber_idx]) * 100.0, 3),
        },
    }


def _round2(points: Iterable[Iterable[float]]) -> list[list[float]]:
    return [[round(float(p[0]), 6), round(float(p[1]), 6)] for p in points]


