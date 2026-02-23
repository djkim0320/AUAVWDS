from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from app.models.state import AeroCurve, DerivedMetrics


@dataclass
class AeroInputs:
    aoa_start: float = -90.0
    aoa_end: float = 90.0
    aoa_step: float = 1.0
    span_m: float = 1.0
    aspect_ratio: float = 8.0
    sweep_deg: float = 0.0
    taper_ratio: float = 1.0
    dihedral_deg: float = 0.0
    twist_deg: float = 0.0
    thickness_percent: float = 12.0
    camber_percent: float = 2.0
    speed_mps: float = 18.0
    kinematic_viscosity_m2_s: float = 1.5e-5
    reynolds: float | None = None


def build_surrogate_curve(inputs: AeroInputs, precision_mode: bool = False) -> tuple[AeroCurve, DerivedMetrics]:
    aoa = np.arange(inputs.aoa_start, inputs.aoa_end + 0.0001, inputs.aoa_step, dtype=float)

    ar = max(1.2, inputs.aspect_ratio)
    sweep = math.radians(inputs.sweep_deg)
    taper = max(0.1, min(1.2, inputs.taper_ratio))

    # Finite-wing CL slope with sweep correction.
    a0 = 2.0 * math.pi
    a = (a0 * ar) / (a0 + math.sqrt(4.0 + ar**2))
    a *= max(0.65, math.cos(sweep) ** 1.1)
    cl_alpha = a / 57.295779513

    camber = inputs.camber_percent / 100.0
    t = inputs.thickness_percent / 100.0

    alpha0 = -2.0 - 35.0 * camber + 0.15 * inputs.twist_deg
    cl_linear = cl_alpha * (aoa - alpha0)

    stall_pos = 11.0 + 16.0 * t + 0.12 * (ar - 7.0) - 0.08 * abs(inputs.sweep_deg)
    stall_neg = -10.0 - 0.8 * stall_pos
    stall_pos = max(8.0, min(32.0, stall_pos))
    stall_neg = max(-30.0, min(-6.0, stall_neg))

    # Smooth saturation to avoid unrealistic linear growth.
    cl = cl_linear.copy()
    cl *= np.tanh((aoa - stall_neg) / max(0.7, abs(stall_pos - stall_neg) * 0.08))
    cl *= np.tanh((stall_pos - aoa) / max(0.7, abs(stall_pos - stall_neg) * 0.08))

    # Normalize using expected CLmax envelope.
    cl_max_target = 0.85 + 3.8 * t + 1.2 * camber + 0.04 * max(0.0, ar - 6.0)
    cl_min_target = -0.75 * cl_max_target
    cl = np.clip(cl, cl_min_target, cl_max_target)

    # Drag model.
    e = max(0.55, min(0.95, 0.86 - 0.08 * abs(taper - 0.55) - 0.0012 * abs(inputs.sweep_deg) + 0.004 * inputs.dihedral_deg))
    k = 1.0 / (math.pi * ar * e)
    cd0 = 0.005 + 0.0015 * (t / 0.12) + 0.0005 * (1.0 - taper)
    cd = cd0 + k * cl**2

    stall_penalty = np.maximum(0.0, np.abs(aoa) - max(10.0, 0.7 * stall_pos))
    cd += 0.0008 * stall_penalty**1.8

    # Pitching moment.
    cm0 = -0.01 - 0.09 * camber
    cm_alpha = -0.015 - 0.0015 * ar
    cm = cm0 + cm_alpha * aoa

    if precision_mode:
        # Precision branch slightly damped and smoother than quick estimation.
        cl *= 0.94
        cd *= 0.92
        cm *= 1.08

    curve = AeroCurve(
        aoa_deg=[float(round(x, 6)) for x in aoa],
        cl=[float(round(x, 6)) for x in cl],
        cd=[float(round(x, 6)) for x in cd],
        cm=[float(round(x, 6)) for x in cm],
    )

    metrics = derive_metrics(curve, reynolds=_estimate_reynolds(inputs), oswald=e)
    return curve, metrics


def _estimate_reynolds(inputs: AeroInputs) -> float:
    if inputs.reynolds is not None and float(inputs.reynolds) > 0:
        return float(inputs.reynolds)

    nu = max(1e-7, float(inputs.kinematic_viscosity_m2_s))
    ar = max(1.2, float(inputs.aspect_ratio))
    c_ref = max(0.02, float(inputs.span_m) / ar)
    speed = max(0.1, float(inputs.speed_mps))
    return speed * c_ref / nu


def derive_metrics(curve: AeroCurve, reynolds: float, oswald: float) -> DerivedMetrics:
    aoa = np.array(curve.aoa_deg, dtype=float)
    cl = np.array(curve.cl, dtype=float)
    cd = np.array(curve.cd, dtype=float)
    cm = np.array(curve.cm, dtype=float)

    cd_safe = np.where(np.abs(cd) < 1e-9, np.nan, cd)
    ld = cl / cd_safe

    finite = np.isfinite(ld)
    if not finite.any():
        ld_max = 0.0
        ld_idx = 0
    else:
        ld_idx = int(np.nanargmax(ld))
        ld_max = float(ld[ld_idx])

    cl_idx = int(np.argmax(cl))
    cd_idx = int(np.argmin(cd))

    mask = (aoa >= -2.0) & (aoa <= 2.0)
    if np.count_nonzero(mask) >= 2:
        slope, intercept = np.polyfit(np.deg2rad(aoa[mask]), cl[mask], 1)
        cl_alpha = float(slope)
        alpha_zero_lift = float(np.rad2deg(-intercept / slope)) if abs(slope) > 1e-12 else 0.0
        cm_slope, cm_intercept = np.polyfit(np.deg2rad(aoa[mask]), cm[mask], 1)
    else:
        cl_alpha = 0.0
        alpha_zero_lift = 0.0
        cm_slope = 0.0
        cm_intercept = float(cm[0] if len(cm) else 0.0)

    cdo = float(np.interp(0.0, aoa, cd)) if len(aoa) else 0.0

    return DerivedMetrics(
        ld_max=round(ld_max, 6),
        ld_max_aoa=round(float(aoa[ld_idx]) if len(aoa) else 0.0, 6),
        cl_max=round(float(cl[cl_idx]) if len(cl) else 0.0, 6),
        cl_max_aoa=round(float(aoa[cl_idx]) if len(aoa) else 0.0, 6),
        cd_min=round(float(cd[cd_idx]) if len(cd) else 0.0, 6),
        cd_min_aoa=round(float(aoa[cd_idx]) if len(aoa) else 0.0, 6),
        cl_alpha=round(cl_alpha, 6),
        alpha_zero_lift=round(alpha_zero_lift, 6),
        cm_zero_lift=round(cm_intercept, 6),
        cm_alpha=round(float(cm_slope), 6),
        cd_zero=round(cdo, 6),
        oswald_e=round(float(oswald), 6),
        endurance_param=round(float(ld_max / max(1e-9, cdo)), 6),
        range_param=round(float((ld_max**0.5) / max(1e-9, cdo)), 6),
        reynolds=round(float(reynolds), 2),
    )


