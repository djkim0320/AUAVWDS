from __future__ import annotations

from bisect import bisect_left
from typing import Any

from pydantic import BaseModel, Field

from app.models.state import (
    AnalysisConditions,
    AnalysisMode,
    AnalysisResult,
    AppState,
    AirfoilSummary,
    DerivedMetrics,
    SolverId,
    WingParams,
    get_active_result,
)


_CURVE_SAMPLE_AOA = (-10.0, -5.0, 0.0, 5.0, 10.0, 15.0, 20.0)
_VSPAERO_FOCUS_KEYS = (
    "aoa_ld_max",
    "l_d_max",
    "cltot_ld_max",
    "cltot_max",
    "cltot_min",
    "cdtot_ld_max",
    "cdtot_min",
    "cdtot_max",
    "cmytot_ld_max",
    "cmytot_max",
    "cmytot_min",
    "e_ld_max",
)
_CLIENT_EXTRA_KEYS = (
    "solver_id",
    "solver_label",
    "solver_mode",
    "result_level",
    "correction_model",
    "wing_correction_model",
    "limitation_note",
    "analysis_conditions",
    "solver_airfoil",
    "solver_wingtip",
    "airfoil_representation",
    "analysis_confidence",
    "used_reynolds",
    "used_n_crit",
    "used_model_size",
    "used_mach",
    "used_reference_source",
    "used_oswald",
    "used_oswald_source",
    "curve_source",
    "row_count",
    "row_count_raw",
    "requested_aoa_range",
    "valid_aoa_range",
    "selected_coefficient_family",
    "selected_coefficient_family_label",
    "coefficient_family_selection",
    "requested_conditions",
    "comparison_ready",
    "comparison_blockers",
    "comparison_summary",
    "comparison_counterpart_solver",
    "fallback_reason",
    "reason",
)
_CLIENT_NESTED_DICT_KEYS = (
    "precision_data",
    "vspaero_all_data",
    "solver_scalar_data",
    "solver_effective_conditions",
    "selected_coefficient_columns",
    "coefficient_family_candidates",
    "reference_values_used",
    "geometry_snapshot",
    "comparison_aoa_window",
    "comparison_metrics",
)


class ClientAirfoilState(BaseModel):
    coords: list[list[float]] = Field(default_factory=list)
    upper: list[list[float]] = Field(default_factory=list)
    lower: list[list[float]] = Field(default_factory=list)
    camber: list[list[float]] = Field(default_factory=list)
    summary: AirfoilSummary = Field(default_factory=AirfoilSummary)


class ClientWingState(BaseModel):
    params: WingParams = Field(default_factory=WingParams)
    preview_mesh: None = None
    planform_2d: None = None


class ClientAeroCurve(BaseModel):
    aoa_deg: list[float] = Field(default_factory=list)
    cl: list[float] = Field(default_factory=list)
    cd: list[float] = Field(default_factory=list)
    cm: list[float] = Field(default_factory=list)


class ClientAnalysisResult(BaseModel):
    source_label: str
    curve: ClientAeroCurve = Field(default_factory=ClientAeroCurve)
    metrics: DerivedMetrics | None = None
    analysis_mode: AnalysisMode = 'fallback'
    fallback_reason: str | None = None
    extra_data: dict[str, Any] = Field(default_factory=dict)
    notes: str = ''
    created_at: str


class ClientSolverResults(BaseModel):
    openvsp: ClientAnalysisResult | None = None
    neuralfoil: ClientAnalysisResult | None = None


class ClientAnalysisState(BaseModel):
    results: ClientSolverResults = Field(default_factory=ClientSolverResults)
    active_solver: SolverId = 'openvsp'
    conditions: AnalysisConditions = Field(default_factory=AnalysisConditions)


class ClientAppState(BaseModel):
    airfoil: ClientAirfoilState = Field(default_factory=ClientAirfoilState)
    wing: ClientWingState = Field(default_factory=ClientWingState)
    analysis: ClientAnalysisState = Field(default_factory=ClientAnalysisState)
    history: list[dict[str, Any]] = Field(default_factory=list)


def serialize_client_state(state: AppState) -> ClientAppState:
    return ClientAppState(
        airfoil=ClientAirfoilState(summary=state.airfoil.summary.model_copy(deep=True)),
        wing=ClientWingState(params=state.wing.params.model_copy(deep=True)),
        analysis=ClientAnalysisState(
            results=ClientSolverResults(
                openvsp=_serialize_analysis_result(state.analysis.results.openvsp),
                neuralfoil=_serialize_analysis_result(state.analysis.results.neuralfoil),
            ),
            active_solver=state.analysis.active_solver,
            conditions=state.analysis.conditions.model_copy(deep=True),
        ),
        history=[],
    )


def _recommended_reynolds_hint(state: AppState) -> dict[str, Any] | None:
    conditions = state.analysis.conditions
    if conditions.reynolds is not None:
        return None

    mach = float(conditions.mach or 0.0)
    span_m = float(state.wing.params.span_m or 0.0)
    aspect_ratio = float(state.wing.params.aspect_ratio or 0.0)
    if mach <= 0.0 or span_m <= 0.0 or aspect_ratio <= 0.0:
        return None

    representative_chord_m = span_m / aspect_ratio
    speed_mps = mach * 340.3
    if representative_chord_m <= 0.0 or speed_mps <= 0.0:
        return None

    estimated_reynolds = speed_mps * representative_chord_m / 1.5e-5
    if estimated_reynolds <= 0.0:
        return None

    recommended_reynolds = int(round(estimated_reynolds / 100.0) * 100)
    return {
        "reynolds": recommended_reynolds,
        "source": "mach_and_mean_chord_estimate",
        "speed_mps": round(speed_mps, 3),
        "representative_chord_m": round(representative_chord_m, 4),
        "mach": mach,
        "note": "Use this inferred Reynolds before solver analysis when the user did not specify one.",
    }


def build_llm_state_summary(state: AppState) -> dict[str, Any]:
    active_solver, active = get_active_result(state.analysis)
    curve_summary = _curve_summary(active)
    reynolds_hint = _recommended_reynolds_hint(state)
    return {
        "airfoil": state.airfoil.summary.model_dump(),
        "wing": state.wing.params.model_dump(),
        "analysis_conditions": state.analysis.conditions.model_dump(),
        "recommended_reynolds": reynolds_hint["reynolds"] if reynolds_hint else None,
        "recommended_reynolds_basis": reynolds_hint,
        "active_solver": active_solver,
        "analysis_available": bool(active),
        "available_results": {
            "openvsp": state.analysis.results.openvsp is not None,
            "neuralfoil": state.analysis.results.neuralfoil is not None,
        },
        "active_source_label": active.source_label if active else None,
        "active_result_mode": active.analysis_mode if active else None,
        "active_result_solver_id": active.extra_data.get("solver_id") if active else None,
        "active_fallback_reason": active.fallback_reason if active else None,
        "active_notes": active.notes if active else None,
        "active_solver_airfoil": active.extra_data.get("solver_airfoil") if active else None,
        "has_mesh": bool(state.wing.preview_mesh and state.wing.preview_mesh.vertices),
        "active_metrics": active.metrics.model_dump() if active and active.metrics else None,
        "active_curve_range": curve_summary["range"],
        "active_curve_samples": curve_summary["samples"],
        "active_coefficient_family": active.extra_data.get("selected_coefficient_family_label") if active else None,
        "active_solver_effective_conditions": _copy_dict(active.extra_data.get("solver_effective_conditions")) if active else None,
        "fair_comparison_ready": active.extra_data.get("comparison_ready") if active else None,
        "fair_comparison_blockers": list(active.extra_data.get("comparison_blockers") or []) if active else None,
        "fair_comparison_aoa_window": _copy_dict(active.extra_data.get("comparison_aoa_window")) if active else None,
        "fair_comparison_metrics": _copy_dict(active.extra_data.get("comparison_metrics")) if active else None,
        "precision_data": _copy_dict(active.extra_data.get("precision_data")) if active else None,
        "vspaero_focus_data": _vspaero_focus_data(active.extra_data.get("vspaero_all_data")) if active else None,
    }

def _serialize_analysis_result(result: AnalysisResult | None) -> ClientAnalysisResult | None:
    if result is None:
        return None

    return ClientAnalysisResult(
        source_label=result.source_label,
        curve=_empty_curve_payload(),
        metrics=result.metrics.model_copy(deep=True) if result.metrics else None,
        analysis_mode=result.analysis_mode,
        fallback_reason=result.fallback_reason,
        extra_data=_serialize_client_extra_data(result.extra_data or {}),
        notes=result.notes,
        created_at=result.created_at,
    )


def _serialize_client_extra_data(extra: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _CLIENT_EXTRA_KEYS:
        if key in extra:
            out[key] = extra[key]

    for key in _CLIENT_NESTED_DICT_KEYS:
        copied = _copy_dict(extra.get(key))
        if copied is not None:
            out[key] = copied

    artifacts = extra.get("available_artifacts")
    if isinstance(artifacts, list):
        out["available_artifacts"] = [str(item) for item in artifacts]

    filtering = extra.get("curve_filtering")
    if isinstance(filtering, dict):
        out["curve_filtering"] = {
            key: filtering[key]
            for key in (
                "raw_row_count",
                "plausible_row_count",
                "valid_row_count",
                "dropped_row_count",
                "dropped_aoa",
                "used_aoa_range",
                "requested_aoa_range",
                "exclusion_reason_summary",
            )
            if key in filtering
        }

    out["can_export_vsp3"] = bool(isinstance(extra.get("vsp3_path"), str) and str(extra.get("vsp3_path")).strip())
    return out


def _curve_summary(result: AnalysisResult | None) -> dict[str, Any]:
    if result is None:
        return {"range": None, "samples": None}

    curve = {
        "aoa_deg": [float(x) for x in (result.curve.aoa_deg or [])],
        "cl": [float(x) for x in (result.curve.cl or [])],
        "cd": [float(x) for x in (result.curve.cd or [])],
        "cm": [float(x) for x in (result.curve.cm or [])],
    }
    aoa = curve["aoa_deg"]
    cl = curve["cl"]
    cd = curve["cd"]
    cm = curve["cm"]
    if not (aoa and cl and cd and cm):
        return {"range": None, "samples": None}

    aoa_min = float(min(aoa))
    aoa_max = float(max(aoa))
    aoa_sorted = all(aoa[i] <= aoa[i + 1] for i in range(len(aoa) - 1))
    used_indices: set[int] = set()
    samples: list[dict[str, float | bool]] = []
    for target_aoa in _CURVE_SAMPLE_AOA:
        if target_aoa < aoa_min or target_aoa > aoa_max:
            continue

        if aoa_sorted:
            insert_at = bisect_left(aoa, target_aoa)
            candidate_indices = []
            if insert_at < len(aoa):
                candidate_indices.append(insert_at)
            if insert_at > 0:
                candidate_indices.append(insert_at - 1)
            idx = min(candidate_indices, key=lambda i: (abs(aoa[i] - target_aoa), i))
        else:
            idx = min(range(len(aoa)), key=lambda i: abs(aoa[i] - target_aoa))
        if idx in used_indices:
            continue
        used_indices.add(idx)

        sampled_aoa = float(aoa[idx])
        cd_i = cd[idx]
        ld_i = (cl[idx] / cd_i) if abs(cd_i) > 1e-9 else 0.0
        samples.append({
            "requested_aoa_deg": float(target_aoa),
            "sampled_aoa_deg": sampled_aoa,
            "exact_match": abs(sampled_aoa - float(target_aoa)) < 1e-9,
            "cl": float(cl[idx]),
            "cd": float(cd[idx]),
            "cm": float(cm[idx]),
            "ld": float(ld_i),
        })

    return {
        "range": {
            "aoa_min": aoa_min,
            "aoa_max": aoa_max,
            "point_count": len(aoa),
        },
        "samples": samples or None,
    }


def _vspaero_focus_data(payload: Any) -> dict[str, float] | None:
    if not isinstance(payload, dict):
        return None

    focus: dict[str, float] = {}
    for key in _VSPAERO_FOCUS_KEYS:
        value = payload.get(key)
        if isinstance(value, (int, float)):
            focus[key] = float(value)
    return focus or None


def _copy_dict(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _empty_curve_payload() -> ClientAeroCurve:
    return ClientAeroCurve()
