from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.analysis.common import AeroInputs, build_surrogate_curve, derive_metrics
from app.models.state import AeroCurve, AnalysisResult, AppState, source_label_for
from app.runtime.native import prepare_native_runtime_dirs


def run_neuralfoil_analysis(state: AppState, work_dir: str | Path, payload: dict[str, Any] | None = None) -> AnalysisResult:
    _ = payload or {}

    conditions = state.analysis.conditions
    aoa_start = float(conditions.aoa_start)
    aoa_end = float(conditions.aoa_end)
    aoa_step = max(0.25, float(conditions.aoa_step))
    mach = max(0.01, float(conditions.mach))

    params = state.wing.params
    summary = state.airfoil.summary
    coords = _solver_airfoil_coords(state)
    base_work = Path(work_dir).resolve()
    base_work.mkdir(parents=True, exist_ok=True)
    run_dir = base_work / "neuralfoil_runs" / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)

    reynolds = _resolve_reynolds(state)
    inputs = AeroInputs(
        aoa_start=aoa_start,
        aoa_end=aoa_end,
        aoa_step=aoa_step,
        span_m=params.span_m,
        aspect_ratio=params.aspect_ratio,
        sweep_deg=params.sweep_deg,
        taper_ratio=params.taper_ratio,
        dihedral_deg=params.dihedral_deg,
        twist_deg=params.twist_deg,
        thickness_percent=summary.thickness_percent or 12.0,
        camber_percent=summary.max_camber_percent or 2.0,
        speed_mps=max(0.1, mach * 340.3),
        reynolds=reynolds,
    )

    if len(coords) < 6:
        return _neuralfoil_fallback_result(
            inputs=inputs,
            params=params.model_dump(),
            summary=summary.model_dump(),
            conditions=conditions.model_dump(),
            run_dir=run_dir,
            reason="선택한 에어포일에 NeuralFoil 해석에 필요한 좌표가 충분하지 않습니다.",
        )

    input_payload = {
        "airfoil_summary": summary.model_dump(),
        "wing_params": params.model_dump(),
        "analysis_conditions": conditions.model_dump(),
        "coordinate_count": len(coords),
        "coordinates": coords,
    }
    (run_dir / "inputs.json").write_text(json.dumps(input_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    aoa = np.arange(aoa_start, aoa_end + 1e-9, aoa_step, dtype=float)
    section_alpha = aoa + float(params.twist_deg) * 0.35
    mac = _mean_aerodynamic_chord(params.span_m, params.aspect_ratio, params.taper_ratio)

    try:
        prepare_native_runtime_dirs()
        import neuralfoil  # type: ignore

        raw = neuralfoil.get_aero_from_coordinates(
            np.array(coords, dtype=float),
            alpha=section_alpha,
            Re=reynolds,
            n_crit=9.0,
            xtr_upper=1.0,
            xtr_lower=1.0,
            model_size="large",
        )
    except Exception as exc:
        return _neuralfoil_fallback_result(
            inputs=inputs,
            params=params.model_dump(),
            summary=summary.model_dump(),
            conditions=conditions.model_dump(),
            run_dir=run_dir,
            reason=f"NeuralFoil 실행에 실패했습니다: {exc}",
        )

    raw_payload = _jsonify(raw)
    (run_dir / "outputs.json").write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        cl_2d = np.asarray(raw.get("CL"), dtype=float)
        cd_2d = np.asarray(raw.get("CD"), dtype=float)
        cm_2d = np.asarray(raw.get("CM"), dtype=float)
    except Exception as exc:
        return _neuralfoil_fallback_result(
            inputs=inputs,
            params=params.model_dump(),
            summary=summary.model_dump(),
            conditions=conditions.model_dump(),
            run_dir=run_dir,
            reason=f"NeuralFoil이 유효하지 않은 결과를 반환했습니다: {exc}",
        )

    if cl_2d.size != aoa.size or cd_2d.size != aoa.size or cm_2d.size != aoa.size:
        return _neuralfoil_fallback_result(
            inputs=inputs,
            params=params.model_dump(),
            summary=summary.model_dump(),
            conditions=conditions.model_dump(),
            run_dir=run_dir,
            reason="NeuralFoil 결과 곡선 길이가 요청한 AoA 범위와 일치하지 않습니다.",
        )

    corrected = _apply_wing_correction(
        aoa=aoa,
        cl_2d=cl_2d,
        cd_2d=cd_2d,
        cm_2d=cm_2d,
        params=params.model_dump(),
    )
    curve = AeroCurve(
        aoa_deg=[float(round(v, 6)) for v in aoa.tolist()],
        cl=[float(round(v, 6)) for v in corrected["cl"]],
        cd=[float(round(max(1e-6, v), 6)) for v in corrected["cd"]],
        cm=[float(round(v, 6)) for v in corrected["cm"]],
    )
    metrics = derive_metrics(curve, reynolds=float(reynolds), oswald=corrected["oswald_e"])

    processed = {
        "curve": curve.model_dump(),
        "metrics": metrics.model_dump(),
        "correction_meta": {
            "oswald_e": corrected["oswald_e"],
            "lift_factor": corrected["lift_factor"],
            "profile_drag_factor": corrected["profile_drag_factor"],
            "representative_twist_shift_deg": corrected["twist_shift_deg"],
        },
    }
    (run_dir / "processed_result.json").write_text(json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8")

    precision_data = {
        "aoa_start": float(aoa_start),
        "aoa_end": float(aoa_end),
        "aoa_step": float(aoa_step),
        "aoa_count": float(len(curve.aoa_deg)),
        "cl_min": float(min(curve.cl) if curve.cl else 0.0),
        "cl_max": float(max(curve.cl) if curve.cl else 0.0),
        "cd_min": float(min(curve.cd) if curve.cd else 0.0),
        "cd_max": float(max(curve.cd) if curve.cd else 0.0),
        "cm_min": float(min(curve.cm) if curve.cm else 0.0),
        "cm_max": float(max(curve.cm) if curve.cm else 0.0),
        "ld_max": float(metrics.ld_max if metrics else 0.0),
        "sref": float(max(1e-6, params.span_m * params.span_m / max(1.0, params.aspect_ratio))),
        "cref": float(mac),
        "bref": float(params.span_m),
        "reynolds": float(reynolds),
    }

    extra_data: dict[str, Any] = {
        "solver_id": "neuralfoil",
        "solver_label": "NeuralFoil",
        "solver_mode": "neuralfoil-wing-estimate",
        "run_dir": str(run_dir),
        "inputs_path": str(run_dir / "inputs.json"),
        "outputs_path": str(run_dir / "outputs.json"),
        "processed_result_path": str(run_dir / "processed_result.json"),
        "analysis_conditions": conditions.model_dump(),
        "solver_airfoil": {
            "requested_label": str(summary.code or "").strip() or "커스텀 에어포일",
            "coordinate_count": len(coords),
            "representation_label": "에어포일 좌표",
            "geometry_kind": "coordinates",
        },
        "result_level": "wing_estimate_from_2d_solver",
        "correction_model": "finite-wing-coupled-from-neuralfoil",
        "wing_correction_model": "finite-wing-coupled-from-neuralfoil",
        "limitation_note": "VLM/패널 solver가 아니며, 2D 에어포일 해석 결과에 명시적 날개 보정을 적용한 추정 결과입니다.",
        "airfoil_representation": "coordinates",
        "raw_neuralfoil_output": raw_payload,
        "analysis_confidence": raw_payload.get("analysis_confidence"),
        "used_reynolds": float(reynolds),
        "used_n_crit": 9.0,
        "used_model_size": "large",
        "used_mach": float(mach),
        "precision_data": precision_data,
        "available_artifacts": ["inputs.json", "outputs.json", "processed_result.json"],
    }

    return AnalysisResult(
        source_label=source_label_for("neuralfoil", "neuralfoil"),
        curve=curve,
        metrics=metrics,
        analysis_mode="neuralfoil",
        fallback_reason=None,
        extra_data=extra_data,
        notes="NeuralFoil 2D polar를 기반으로 유한 날개 보정을 적용한 추정 결과입니다. 더 높은 충실도의 날개 해석이 필요하면 OpenVSP/VSPAERO 결과를 확인해 주세요.",
    )


def _neuralfoil_fallback_result(
    *,
    inputs: AeroInputs,
    params: dict[str, Any],
    summary: dict[str, Any],
    conditions: dict[str, Any],
    run_dir: Path,
    reason: str,
) -> AnalysisResult:
    curve, metrics = build_surrogate_curve(inputs, precision_mode=False)
    processed = {
        "curve": curve.model_dump(),
        "metrics": metrics.model_dump(),
        "reason": reason,
    }
    (run_dir / "processed_result.json").write_text(json.dumps(processed, ensure_ascii=False, indent=2), encoding="utf-8")

    extra_data: dict[str, Any] = {
        "solver_id": "neuralfoil",
        "solver_label": "NeuralFoil",
        "solver_mode": "neuralfoil-fallback",
        "reason": reason,
        "fallback_reason": reason,
        "params": params,
        "airfoil_summary": summary,
        "analysis_conditions": conditions,
        "run_dir": str(run_dir),
        "result_level": "wing_estimate_fallback",
        "correction_model": "finite-wing-coupled-from-neuralfoil",
        "wing_correction_model": "finite-wing-coupled-from-neuralfoil",
        "limitation_note": "VLM/패널 solver가 아니며, 2D 에어포일 해석 결과에 명시적 날개 보정을 적용한 추정 결과입니다.",
        "precision_data": {
            "aoa_start": float(inputs.aoa_start),
            "aoa_end": float(inputs.aoa_end),
            "aoa_step": float(inputs.aoa_step),
            "aoa_count": float(len(curve.aoa_deg)),
            "reynolds": float(metrics.reynolds if metrics else 0.0),
        },
    }
    return AnalysisResult(
        source_label=source_label_for("neuralfoil", "fallback"),
        curve=curve,
        metrics=metrics,
        analysis_mode="fallback",
        fallback_reason=reason,
        extra_data=extra_data,
        notes=f"NeuralFoil 경로가 대체 해석으로 전환되었습니다: {reason}",
    )


def _apply_wing_correction(
    *,
    aoa: np.ndarray,
    cl_2d: np.ndarray,
    cd_2d: np.ndarray,
    cm_2d: np.ndarray,
    params: dict[str, Any],
) -> dict[str, Any]:
    ar = max(1.2, float(params["aspect_ratio"]))
    taper = max(0.1, min(1.2, float(params["taper_ratio"])))
    sweep_deg = float(params["sweep_deg"])
    dihedral_deg = float(params["dihedral_deg"])
    twist_deg = float(params["twist_deg"])

    sweep_rad = math.radians(sweep_deg)
    lift_factor = (ar / (ar + 2.0)) * max(0.55, math.cos(sweep_rad) ** 0.9) * max(0.88, 1.0 - 0.14 * abs(taper - 0.45))
    oswald_e = max(
        0.55,
        min(
            0.95,
            0.84 - 0.002 * abs(sweep_deg) - 0.06 * abs(taper - 0.45) + 0.003 * dihedral_deg,
        ),
    )
    profile_drag_factor = max(1.0, 1.0 + 0.06 * (1.0 - math.cos(sweep_rad)) + 0.015 * abs(twist_deg))

    cl_3d = cl_2d * lift_factor
    cdi = (cl_3d ** 2) / max(1e-6, math.pi * ar * oswald_e)
    cd_3d = np.maximum(1e-6, cd_2d * profile_drag_factor + cdi)
    cm_3d = cm_2d * max(0.8, math.cos(sweep_rad) ** 0.65) * max(0.92, 1.0 - 0.003 * abs(dihedral_deg))

    return {
        "cl": [float(v) for v in cl_3d.tolist()],
        "cd": [float(v) for v in cd_3d.tolist()],
        "cm": [float(v) for v in cm_3d.tolist()],
        "oswald_e": float(oswald_e),
        "lift_factor": float(lift_factor),
        "profile_drag_factor": float(profile_drag_factor),
        "twist_shift_deg": float(twist_deg * 0.35),
    }


def _resolve_reynolds(state: AppState) -> float:
    cond = state.analysis.conditions
    if cond.reynolds is not None and float(cond.reynolds) > 0:
        return float(cond.reynolds)

    span = max(0.15, float(state.wing.params.span_m))
    ar = max(1.2, float(state.wing.params.aspect_ratio))
    mach = max(0.01, float(cond.mach))
    speed = mach * 340.3
    chord = max(0.02, span / ar)
    return speed * chord / 1.5e-5


def _mean_aerodynamic_chord(span: float, aspect_ratio: float, taper_ratio: float) -> float:
    span = max(0.15, float(span))
    ar = max(1.2, float(aspect_ratio))
    taper = max(0.1, min(1.2, float(taper_ratio)))
    area = span * span / ar
    c_root = max(1e-4, (2.0 * area) / (span * (1.0 + taper)))
    return (2.0 / 3.0) * c_root * ((1.0 + taper + taper * taper) / (1.0 + taper))


def _solver_airfoil_coords(state: AppState) -> list[list[float]]:
    airfoil = state.airfoil
    if airfoil.coords:
        return [[float(p[0]), float(p[1])] for p in airfoil.coords]
    if airfoil.upper and airfoil.lower:
        return [[float(p[0]), float(p[1])] for p in (airfoil.upper[::-1] + airfoil.lower[1:])]
    return []


def _jsonify(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    return value
