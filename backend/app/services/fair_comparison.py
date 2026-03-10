from __future__ import annotations

import math
from typing import Any

from app.models.state import AnalysisResult, AppState, SolverId


_MIN_COMPARISON_POINTS = 3
_AOA_KEY_DIGITS = 6
_REYNOLDS_REL_TOL = 0.05
_REYNOLDS_ABS_TOL = 500.0
_REFERENCE_REL_TOL = 0.01


def enrich_state_with_fair_comparison(state: AppState) -> AppState:
    enriched = state.model_copy(deep=True)
    requested_conditions = _normalize_requested_conditions(enriched.analysis.conditions.model_dump())

    contexts: dict[SolverId, dict[str, Any] | None] = {
        "openvsp": _build_result_context(
            result=enriched.analysis.results.openvsp,
            solver_id="openvsp",
            requested_conditions=requested_conditions,
            state=enriched,
        ),
        "neuralfoil": _build_result_context(
            result=enriched.analysis.results.neuralfoil,
            solver_id="neuralfoil",
            requested_conditions=requested_conditions,
            state=enriched,
        ),
    }

    comparison = _build_pair_comparison(
        requested_conditions=requested_conditions,
        openvsp_context=contexts["openvsp"],
        neuralfoil_context=contexts["neuralfoil"],
    )

    for solver_id, result in (
        ("openvsp", enriched.analysis.results.openvsp),
        ("neuralfoil", enriched.analysis.results.neuralfoil),
    ):
        if result is None:
            continue

        context = contexts[solver_id]
        if context is None:
            continue

        extra = dict(result.extra_data or {})
        extra["requested_conditions"] = dict(context["requested_conditions"])
        extra["solver_effective_conditions"] = dict(context["solver_effective_conditions"])
        extra["valid_aoa_range"] = _copy_range(context["valid_aoa_range"])
        extra["reference_values_used"] = dict(context["reference_values_used"])
        extra["geometry_snapshot"] = dict(context["geometry_snapshot"])
        extra["comparison_ready"] = comparison["comparison_ready"]
        extra["comparison_blockers"] = list(comparison["comparison_blockers"])
        extra["comparison_aoa_window"] = _copy_window(comparison["comparison_aoa_window"])
        extra["comparison_summary"] = comparison["comparison_summary"]
        extra["comparison_counterpart_solver"] = (
            "neuralfoil"
            if solver_id == "openvsp" and contexts["neuralfoil"] is not None
            else "openvsp"
            if solver_id == "neuralfoil" and contexts["openvsp"] is not None
            else None
        )
        if comparison["comparison_metrics"] is not None:
            extra["comparison_metrics"] = dict(comparison["comparison_metrics"])
        else:
            extra.pop("comparison_metrics", None)
        result.extra_data = extra

    return enriched


def _build_result_context(
    *,
    result: AnalysisResult | None,
    solver_id: SolverId,
    requested_conditions: dict[str, Any],
    state: AppState,
) -> dict[str, Any] | None:
    if result is None:
        return None

    extra = dict(result.extra_data or {})
    return {
        "solver_id": solver_id,
        "result": result,
        "analysis_mode": result.analysis_mode,
        "requested_conditions": _normalize_requested_conditions(
            extra.get("requested_conditions") or extra.get("analysis_conditions") or requested_conditions
        ),
        "solver_effective_conditions": _normalize_solver_effective_conditions(
            result=result,
            solver_id=solver_id,
            requested_conditions=requested_conditions,
        ),
        "valid_aoa_range": _valid_aoa_range(result),
        "reference_values_used": _reference_values_used(result),
        "geometry_snapshot": _geometry_snapshot(state=state, result=result),
    }


def _build_pair_comparison(
    *,
    requested_conditions: dict[str, Any],
    openvsp_context: dict[str, Any] | None,
    neuralfoil_context: dict[str, Any] | None,
) -> dict[str, Any]:
    blockers: list[str] = []

    if openvsp_context is None or neuralfoil_context is None:
        blockers.append("missing_counterpart_result")
    else:
        if openvsp_context["analysis_mode"] == "fallback" or neuralfoil_context["analysis_mode"] == "fallback":
            blockers.append("fallback_result_present")

        if not _requested_conditions_match(
            openvsp_context["requested_conditions"],
            neuralfoil_context["requested_conditions"],
        ):
            blockers.append("analysis_condition_mismatch")

        if not _geometry_snapshot_match(
            openvsp_context["geometry_snapshot"],
            neuralfoil_context["geometry_snapshot"],
        ):
            blockers.append("geometry_mismatch")

        if _unsupported_airfoil_parity(
            openvsp_context["geometry_snapshot"],
            neuralfoil_context["geometry_snapshot"],
        ):
            blockers.append("unsupported_airfoil_parity")

        if not _reference_values_match(
            openvsp_context["reference_values_used"],
            neuralfoil_context["reference_values_used"],
        ):
            blockers.append("reference_value_mismatch")

        if _coefficient_family_unstable(openvsp_context):
            blockers.append("coefficient_family_unstable")

        blockers.extend(_reynolds_blockers(openvsp_context, neuralfoil_context))

    comparison_window = (
        _compute_comparison_window(
            requested_conditions=requested_conditions,
            openvsp_context=openvsp_context,
            neuralfoil_context=neuralfoil_context,
        )
        if openvsp_context is not None and neuralfoil_context is not None
        else None
    )

    if comparison_window is None:
        blockers.append("no_valid_aoa_overlap")

    blockers = _dedupe(blockers)
    comparison_ready = len(blockers) == 0
    comparison_metrics = (
        _comparison_metrics(
            openvsp_context["result"],
            neuralfoil_context["result"],
            comparison_window,
        )
        if comparison_ready and openvsp_context is not None and neuralfoil_context is not None and comparison_window is not None
        else None
    )

    if comparison_ready and comparison_window is not None:
        comparison_summary = (
            f"Fair comparison is ready over {comparison_window['start']:.1f}° to "
            f"{comparison_window['end']:.1f}° ({comparison_window['point_count']} shared points)."
        )
    else:
        comparison_summary = (
            "Fair comparison is blocked: " + ", ".join(blockers)
            if blockers
            else "Fair comparison is unavailable."
        )

    return {
        "comparison_ready": comparison_ready,
        "comparison_blockers": blockers,
        "comparison_aoa_window": comparison_window,
        "comparison_metrics": comparison_metrics,
        "comparison_summary": comparison_summary,
    }


def _normalize_requested_conditions(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        value = {}
    return {
        "aoa_start": _to_float(value.get("aoa_start")),
        "aoa_end": _to_float(value.get("aoa_end")),
        "aoa_step": _to_float(value.get("aoa_step")),
        "mach": _to_float(value.get("mach")),
        "reynolds": _to_float(value.get("reynolds"), positive_only=True),
    }


def _normalize_solver_effective_conditions(
    *,
    result: AnalysisResult,
    solver_id: SolverId,
    requested_conditions: dict[str, Any],
) -> dict[str, Any]:
    extra = dict(result.extra_data or {})
    base = dict(extra.get("solver_effective_conditions") or {})
    requested_reynolds = _to_float(requested_conditions.get("reynolds"), positive_only=True)

    effective_reynolds = _to_float(base.get("effective_reynolds"), positive_only=True)
    if effective_reynolds is None:
        effective_reynolds = _to_float(base.get("re_cref"), positive_only=True)
    if effective_reynolds is None:
        effective_reynolds = _to_float(extra.get("used_reynolds"), positive_only=True)
    if effective_reynolds is None and result.metrics:
        effective_reynolds = _to_float(result.metrics.reynolds, positive_only=True)

    mach = _to_float(base.get("mach"))
    if mach is None:
        mach = _to_float(extra.get("used_mach"))
    if mach is None:
        mach = _to_float(requested_conditions.get("mach"))

    reynolds_applied = base.get("reynolds_applied")
    if not isinstance(reynolds_applied, bool):
        reynolds_applied = (
            effective_reynolds is not None and requested_reynolds is not None and _within_tolerance(
                effective_reynolds,
                requested_reynolds,
                rel_tol=_REYNOLDS_REL_TOL,
                abs_tol=_REYNOLDS_ABS_TOL,
            )
        )

    reynolds_note = base.get("reynolds_note")
    if not isinstance(reynolds_note, str) or not reynolds_note.strip():
        if solver_id == "neuralfoil":
            if requested_reynolds is not None and effective_reynolds is not None and reynolds_applied:
                reynolds_note = f"Requested Reynolds {requested_reynolds:,.0f} was applied to NeuralFoil."
            elif requested_reynolds is not None and effective_reynolds is not None:
                reynolds_note = (
                    f"Requested Reynolds {requested_reynolds:,.0f} differs from NeuralFoil effective Reynolds "
                    f"{effective_reynolds:,.0f}."
                )
            elif effective_reynolds is not None:
                reynolds_note = f"NeuralFoil used effective Reynolds {effective_reynolds:,.0f}."
            else:
                reynolds_note = "NeuralFoil effective Reynolds could not be confirmed."
        else:
            if requested_reynolds is not None and effective_reynolds is None:
                reynolds_note = "Requested Reynolds was not confirmed in VSPAERO effective inputs."
            elif requested_reynolds is not None and effective_reynolds is not None and not reynolds_applied:
                reynolds_note = (
                    f"Requested Reynolds {requested_reynolds:,.0f} differs from VSPAERO effective ReCref "
                    f"{effective_reynolds:,.0f}."
                )
            elif effective_reynolds is not None:
                reynolds_note = f"VSPAERO effective ReCref is {effective_reynolds:,.0f}."
            else:
                reynolds_note = "VSPAERO effective Reynolds could not be confirmed."

    normalized = dict(base)
    normalized["requested_reynolds"] = requested_reynolds
    normalized["effective_reynolds"] = effective_reynolds
    if solver_id == "openvsp":
        normalized["re_cref"] = effective_reynolds
    normalized["reynolds_applied"] = bool(reynolds_applied)
    normalized["reynolds_note"] = str(reynolds_note)
    normalized["mach"] = mach
    normalized["aoa_range"] = _normalize_range(base.get("aoa_range")) or _valid_aoa_range(result)
    return normalized


def _reference_values_used(result: AnalysisResult) -> dict[str, Any]:
    extra = dict(result.extra_data or {})
    precision = extra.get("precision_data")
    precision_dict = dict(precision) if isinstance(precision, dict) else {}

    sref = _to_float(precision_dict.get("sref"))
    bref = _to_float(precision_dict.get("bref"))
    cref = _to_float(precision_dict.get("cref"))
    source = "precision_data"

    if sref is None:
        sref = _to_float(extra.get("Sref"))
        source = "solver_extra"
    if bref is None:
        bref = _to_float(extra.get("Bref"))
        source = "solver_extra"
    if cref is None:
        cref = _to_float(extra.get("Cref"))
        source = "solver_extra"

    return {
        "sref": sref,
        "bref": bref,
        "cref": cref,
        "source": source,
    }


def _geometry_snapshot(*, state: AppState, result: AnalysisResult) -> dict[str, Any]:
    extra = dict(result.extra_data or {})
    solver_airfoil = dict(extra.get("solver_airfoil") or {})
    solver_wingtip = dict(extra.get("solver_wingtip") or {})
    params = state.wing.params

    return {
        "airfoil_requested_label": str(solver_airfoil.get("requested_label") or state.airfoil.summary.code or "").strip(),
        "airfoil_geometry_kind": str(solver_airfoil.get("geometry_kind") or "").strip(),
        "airfoil_degraded": bool(str(solver_airfoil.get("degraded_note") or "").strip()),
        "span_m": float(params.span_m),
        "aspect_ratio": float(params.aspect_ratio),
        "taper_ratio": float(params.taper_ratio),
        "sweep_deg": float(params.sweep_deg),
        "dihedral_deg": float(params.dihedral_deg),
        "twist_deg": float(params.twist_deg),
        "wingtip_style": str(params.wingtip_style),
        "solver_wingtip_style": str(
            solver_wingtip.get("solver_style") or solver_wingtip.get("requested_style") or params.wingtip_style
        ).strip(),
        "wingtip_degraded": bool(str(solver_wingtip.get("degraded_note") or "").strip()),
    }


def _requested_conditions_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        _within_tolerance(
            _to_float(left.get(key)),
            _to_float(right.get(key)),
            rel_tol=1e-6,
            abs_tol=1e-6,
        )
        for key in ("aoa_start", "aoa_end", "aoa_step", "mach")
    )


def _geometry_snapshot_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        _within_tolerance(
            _to_float(left.get(key)),
            _to_float(right.get(key)),
            rel_tol=1e-6,
            abs_tol=1e-6,
        )
        for key in ("span_m", "aspect_ratio", "taper_ratio", "sweep_deg", "dihedral_deg", "twist_deg")
    ) and str(left.get("wingtip_style") or "") == str(right.get("wingtip_style") or "")


def _unsupported_airfoil_parity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if bool(left.get("airfoil_degraded")) or bool(right.get("airfoil_degraded")):
        return True
    if bool(left.get("wingtip_degraded")) or bool(right.get("wingtip_degraded")):
        return True
    return str(left.get("airfoil_requested_label") or "") != str(right.get("airfoil_requested_label") or "")


def _reference_values_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for key in ("sref", "bref", "cref"):
        if not _within_tolerance(
            _to_float(left.get(key)),
            _to_float(right.get(key)),
            rel_tol=_REFERENCE_REL_TOL,
            abs_tol=1e-6,
        ):
            return False
    return True


def _coefficient_family_unstable(openvsp_context: dict[str, Any]) -> bool:
    result = openvsp_context["result"]
    extra = dict(result.extra_data or {})
    selected = extra.get("selected_coefficient_family")
    candidates = dict(extra.get("coefficient_family_candidates") or {})
    candidate = dict(candidates.get(selected) or {})
    raw_row_count = _to_float(candidate.get("raw_row_count"))
    valid_row_count = _to_float(candidate.get("valid_row_count"))
    if raw_row_count is None or valid_row_count is None or raw_row_count <= 0:
        return False
    return (valid_row_count / raw_row_count) < 0.6


def _reynolds_blockers(openvsp_context: dict[str, Any], neuralfoil_context: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    openvsp_effective = dict(openvsp_context["solver_effective_conditions"])
    neuralfoil_effective = dict(neuralfoil_context["solver_effective_conditions"])

    requested_reynolds = _to_float(openvsp_effective.get("requested_reynolds"), positive_only=True)
    openvsp_re = _to_float(openvsp_effective.get("effective_reynolds"), positive_only=True)
    neuralfoil_re = _to_float(neuralfoil_effective.get("effective_reynolds"), positive_only=True)

    if requested_reynolds is not None and openvsp_re is None:
        blockers.append("no_effective_reynolds_in_vspaero")
    elif requested_reynolds is not None and not bool(openvsp_effective.get("reynolds_applied")):
        blockers.append("reynolds_mismatch")

    if openvsp_re is not None and neuralfoil_re is not None and not _within_tolerance(
        openvsp_re,
        neuralfoil_re,
        rel_tol=_REYNOLDS_REL_TOL,
        abs_tol=_REYNOLDS_ABS_TOL,
    ):
        blockers.append("reynolds_mismatch")

    return blockers


def _compute_comparison_window(
    *,
    requested_conditions: dict[str, Any],
    openvsp_context: dict[str, Any],
    neuralfoil_context: dict[str, Any],
) -> dict[str, Any] | None:
    requested_range = _range_from_conditions(requested_conditions)
    openvsp_range = _normalize_range(openvsp_context["valid_aoa_range"])
    neuralfoil_range = _normalize_range(neuralfoil_context["valid_aoa_range"])
    if requested_range is None or openvsp_range is None or neuralfoil_range is None:
        return None

    start = max(requested_range["start"], openvsp_range["start"], neuralfoil_range["start"])
    end = min(requested_range["end"], openvsp_range["end"], neuralfoil_range["end"])
    if end < start:
        return None

    openvsp_points = set(_curve_point_map(openvsp_context["result"]).keys())
    neuralfoil_points = set(_curve_point_map(neuralfoil_context["result"]).keys())
    common_points = sorted(
        aoa for aoa in openvsp_points.intersection(neuralfoil_points) if start <= aoa <= end
    )
    if len(common_points) < _MIN_COMPARISON_POINTS:
        return None

    return {
        "start": float(common_points[0]),
        "end": float(common_points[-1]),
        "point_count": len(common_points),
    }


def _comparison_metrics(
    openvsp_result: AnalysisResult,
    neuralfoil_result: AnalysisResult,
    comparison_window: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if comparison_window is None:
        return None

    openvsp_map = _curve_point_map(openvsp_result)
    neuralfoil_map = _curve_point_map(neuralfoil_result)
    common_aoa = sorted(
        aoa
        for aoa in openvsp_map.keys() & neuralfoil_map.keys()
        if comparison_window["start"] <= aoa <= comparison_window["end"]
    )
    if len(common_aoa) < _MIN_COMPARISON_POINTS:
        return None

    openvsp_points = [openvsp_map[aoa] for aoa in common_aoa]
    neuralfoil_points = [neuralfoil_map[aoa] for aoa in common_aoa]

    cl_delta = [abs(open_point["cl"] - neural_point["cl"]) for open_point, neural_point in zip(openvsp_points, neuralfoil_points)]
    cd_delta = [abs(open_point["cd"] - neural_point["cd"]) for open_point, neural_point in zip(openvsp_points, neuralfoil_points)]
    ld_delta = [abs(open_point["ld"] - neural_point["ld"]) for open_point, neural_point in zip(openvsp_points, neuralfoil_points)]

    slope_window = [aoa for aoa in common_aoa if abs(aoa) <= 5.0]
    if len(slope_window) < 2:
        slope_window = common_aoa

    slope_openvsp = _fit_cl_alpha(slope_window, [openvsp_map[aoa]["cl"] for aoa in slope_window])
    slope_neuralfoil = _fit_cl_alpha(slope_window, [neuralfoil_map[aoa]["cl"] for aoa in slope_window])
    openvsp_peak_aoa, openvsp_peak_ld = _peak_ld(openvsp_map, common_aoa)
    neuralfoil_peak_aoa, neuralfoil_peak_ld = _peak_ld(neuralfoil_map, common_aoa)

    return {
        "point_count": len(common_aoa),
        "cl_mean_abs_delta": round(sum(cl_delta) / len(cl_delta), 6),
        "cd_mean_abs_delta": round(sum(cd_delta) / len(cd_delta), 6),
        "ld_mean_abs_delta": round(sum(ld_delta) / len(ld_delta), 6),
        "cl_alpha_openvsp": round(slope_openvsp, 6) if slope_openvsp is not None else None,
        "cl_alpha_neuralfoil": round(slope_neuralfoil, 6) if slope_neuralfoil is not None else None,
        "cl_alpha_delta": round(abs(slope_openvsp - slope_neuralfoil), 6)
        if slope_openvsp is not None and slope_neuralfoil is not None
        else None,
        "ld_max_openvsp": round(openvsp_peak_ld, 6),
        "ld_max_neuralfoil": round(neuralfoil_peak_ld, 6),
        "ld_max_delta": round(abs(openvsp_peak_ld - neuralfoil_peak_ld), 6),
        "ld_max_aoa_openvsp": round(openvsp_peak_aoa, 6),
        "ld_max_aoa_neuralfoil": round(neuralfoil_peak_aoa, 6),
    }


def _fit_cl_alpha(aoa_values: list[float], cl_values: list[float]) -> float | None:
    if len(aoa_values) < 2 or len(aoa_values) != len(cl_values):
        return None
    x = [math.radians(value) for value in aoa_values]
    x_mean = sum(x) / len(x)
    y_mean = sum(cl_values) / len(cl_values)
    denom = sum((value - x_mean) ** 2 for value in x)
    if denom <= 1e-12:
        return None
    numer = sum((x_value - x_mean) * (y_value - y_mean) for x_value, y_value in zip(x, cl_values))
    return numer / denom


def _peak_ld(point_map: dict[float, dict[str, float]], aoa_values: list[float]) -> tuple[float, float]:
    best_aoa = aoa_values[0]
    best_ld = point_map[best_aoa]["ld"]
    for aoa in aoa_values[1:]:
        ld = point_map[aoa]["ld"]
        if ld > best_ld:
            best_aoa = aoa
            best_ld = ld
    return best_aoa, best_ld


def _curve_point_map(result: AnalysisResult) -> dict[float, dict[str, float]]:
    point_map: dict[float, dict[str, float]] = {}
    for idx, aoa in enumerate(result.curve.aoa_deg):
        cl = float(result.curve.cl[idx]) if idx < len(result.curve.cl) else 0.0
        cd = float(result.curve.cd[idx]) if idx < len(result.curve.cd) else 0.0
        cm = float(result.curve.cm[idx]) if idx < len(result.curve.cm) else 0.0
        key = round(float(aoa), _AOA_KEY_DIGITS)
        point_map[key] = {
            "aoa": float(aoa),
            "cl": cl,
            "cd": cd,
            "cm": cm,
            "ld": (cl / cd) if abs(cd) > 1e-9 else 0.0,
        }
    return point_map


def _valid_aoa_range(result: AnalysisResult) -> dict[str, float] | None:
    extra = dict(result.extra_data or {})
    direct = _normalize_range(extra.get("valid_aoa_range"))
    if direct is not None:
        return direct

    filtering = dict(extra.get("curve_filtering") or {})
    filtered = _normalize_range(filtering.get("used_aoa_range"))
    if filtered is not None:
        return filtered

    if result.curve.aoa_deg:
        aoa = [float(value) for value in result.curve.aoa_deg]
        return {"start": float(min(aoa)), "end": float(max(aoa))}
    return None


def _range_from_conditions(conditions: dict[str, Any]) -> dict[str, float] | None:
    start = _to_float(conditions.get("aoa_start"))
    end = _to_float(conditions.get("aoa_end"))
    if start is None or end is None:
        return None
    return {"start": start, "end": end}


def _normalize_range(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    start = _to_float(value.get("start"))
    end = _to_float(value.get("end"))
    if start is None or end is None:
        return None
    return {"start": start, "end": end}


def _within_tolerance(
    left: float | None,
    right: float | None,
    *,
    rel_tol: float,
    abs_tol: float,
) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= max(abs_tol, max(abs(left), abs(right)) * rel_tol)


def _to_float(value: Any, *, positive_only: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        numeric = float(value)
        if positive_only and numeric <= 0:
            return None
        return numeric
    if isinstance(value, str) and value.strip():
        try:
            numeric = float(value)
        except ValueError:
            return None
        if not math.isfinite(numeric):
            return None
        if positive_only and numeric <= 0:
            return None
        return numeric
    return None


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _copy_range(value: dict[str, float] | None) -> dict[str, float] | None:
    return None if value is None else {"start": float(value["start"]), "end": float(value["end"])}


def _copy_window(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "start": float(value["start"]),
        "end": float(value["end"]),
        "point_count": int(value["point_count"]),
    }
