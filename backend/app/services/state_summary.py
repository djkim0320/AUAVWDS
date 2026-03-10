from __future__ import annotations

from typing import Any

from app.models.state import AnalysisResult, AppState, get_active_result


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
    "curve_source",
    "row_count",
    "row_count_raw",
    "requested_aoa_range",
    "valid_aoa_range",
    "fallback_reason",
    "reason",
)
_CLIENT_NESTED_DICT_KEYS = (
    "precision_data",
    "vspaero_all_data",
    "solver_scalar_data",
)
_CLIENT_ARTIFACT_KEYS = (
    "available_artifacts",
    "curve_filtering",
)


def serialize_client_state(state: AppState) -> dict[str, Any]:
    return {
        "airfoil": {
            "coords": [],
            "upper": [],
            "lower": [],
            "camber": [],
            "summary": state.airfoil.summary.model_dump(),
        },
        "wing": {
            "params": state.wing.params.model_dump(),
            "preview_mesh": _serialize_preview_mesh(state.wing.preview_mesh),
            "planform_2d": None,
        },
        "analysis": {
            "results": {
                "openvsp": _serialize_analysis_result(state.analysis.results.openvsp),
                "neuralfoil": _serialize_analysis_result(state.analysis.results.neuralfoil),
            },
            "active_solver": state.analysis.active_solver,
            "conditions": state.analysis.conditions.model_dump(),
        },
        "history": [],
    }


def build_llm_state_summary(state: AppState) -> dict[str, Any]:
    active_solver, active = get_active_result(state.analysis)
    curve_summary = _curve_summary(active)
    return {
        "airfoil": state.airfoil.summary.model_dump(),
        "wing": state.wing.params.model_dump(),
        "analysis_conditions": state.analysis.conditions.model_dump(),
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
        "active_curve": curve_summary["curve"],
        "active_curve_range": curve_summary["range"],
        "active_curve_samples": curve_summary["samples"],
        "precision_data": _copy_dict(active.extra_data.get("precision_data")) if active else None,
        "vspaero_all_data": _copy_dict(active.extra_data.get("vspaero_all_data")) if active else None,
        "vspaero_focus_data": _vspaero_focus_data(active.extra_data.get("vspaero_all_data")) if active else None,
    }


def _serialize_preview_mesh(mesh: Any) -> dict[str, Any] | None:
    if mesh is None:
        return None
    return {
        "vertices": mesh.vertices,
        "triangles": mesh.triangles,
        "pressure_overlay": [],
    }


def _serialize_analysis_result(result: AnalysisResult | None) -> dict[str, Any] | None:
    if result is None:
        return None

    return {
        "source_label": result.source_label,
        "curve": result.curve.model_dump(),
        "metrics": result.metrics.model_dump() if result.metrics else None,
        "analysis_mode": result.analysis_mode,
        "fallback_reason": result.fallback_reason,
        "extra_data": _serialize_client_extra_data(result.extra_data or {}),
        "notes": result.notes,
        "created_at": result.created_at,
    }


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
            for key in ("raw_row_count", "valid_row_count", "dropped_row_count", "dropped_aoa", "used_aoa_range", "requested_aoa_range")
            if key in filtering
        }

    out["can_export_vsp3"] = bool(isinstance(extra.get("vsp3_path"), str) and str(extra.get("vsp3_path")).strip())
    return out


def _curve_summary(result: AnalysisResult | None) -> dict[str, Any]:
    if result is None:
        return {"curve": None, "range": None, "samples": None}

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
        return {"curve": curve, "range": None, "samples": None}

    samples: dict[str, dict[str, float]] = {}
    for target_aoa in _CURVE_SAMPLE_AOA:
        idx = min(range(len(aoa)), key=lambda i: abs(aoa[i] - target_aoa))
        cd_i = cd[idx]
        ld_i = (cl[idx] / cd_i) if abs(cd_i) > 1e-9 else 0.0
        samples[f"{target_aoa:.0f}"] = {
            "aoa_deg": float(aoa[idx]),
            "cl": float(cl[idx]),
            "cd": float(cd[idx]),
            "cm": float(cm[idx]),
            "ld": float(ld_i),
        }

    return {
        "curve": curve,
        "range": {
            "aoa_min": float(min(aoa)),
            "aoa_max": float(max(aoa)),
            "point_count": len(aoa),
        },
        "samples": samples,
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
