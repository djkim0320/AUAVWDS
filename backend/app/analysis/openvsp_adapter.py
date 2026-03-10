from __future__ import annotations

import math
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.analysis.common import AeroInputs, build_surrogate_curve, derive_metrics
from app.models.state import AeroCurve, AirfoilState, AnalysisResult, AppState, source_label_for


_NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
_ROW_RE = re.compile(
    rf"^\s*(\d+)\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+"
    rf"({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+"
    rf"({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})"
)
_NACA4_RE = re.compile(r"(\d{4})")

# VSPAERO .polar files expose both surface-integration and wake/far-field
# coefficient families. We score both families independently and keep the
# more physically consistent one as the primary wing-level result.
_POLAR_FAMILY_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "surface_integration",
        "label": "surface integration",
        "selection_columns": {
            "cl": "cltot",
            "cd": "cdtot",
            "cm": "cmytot",
            "cdo": "cdo",
            "cdi": "cdi",
            "ld": "l_d",
            "e": "e",
        },
    },
    {
        "id": "wake_far_field",
        "label": "wake/far-field",
        "selection_columns": {
            "cl": "clwtot",
            "cd": "cdwtot",
            # VSPAERO exports pitch moment once, so wake-family force
            # coefficients still share the surface moment column.
            "cm": "cmytot",
            "ld": "lodw",
            "e": "ew",
        },
    },
)


def run_precision_analysis(state: AppState, work_dir: str | Path, payload: dict[str, Any] | None = None) -> AnalysisResult:
    _ = payload or {}

    conditions = state.analysis.conditions
    aoa_start = float(conditions.aoa_start)
    aoa_end = float(conditions.aoa_end)
    aoa_step = max(0.25, float(conditions.aoa_step))
    mach = max(0.01, float(conditions.mach))
    speed_mps = max(0.1, mach * 340.3)
    reynolds = float(conditions.reynolds) if conditions.reynolds and float(conditions.reynolds) > 0 else None

    summary = state.airfoil.summary
    params = state.wing.params

    inputs = AeroInputs(
        aoa_start=aoa_start,
        aoa_end=aoa_end,
        aoa_step=max(0.25, aoa_step),
        span_m=params.span_m,
        aspect_ratio=params.aspect_ratio,
        sweep_deg=params.sweep_deg,
        taper_ratio=params.taper_ratio,
        dihedral_deg=params.dihedral_deg,
        twist_deg=params.twist_deg,
        thickness_percent=summary.thickness_percent or 12.0,
        camber_percent=summary.max_camber_percent or 2.0,
        speed_mps=speed_mps,
        reynolds=reynolds,
    )

    base_work = Path(work_dir).resolve()
    base_work.mkdir(parents=True, exist_ok=True)
    run_dir = base_work / "precision_runs" / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    run_dir.mkdir(parents=True, exist_ok=True)

    try:
        solver_airfoil, airfoil_error = _prepare_solver_airfoil(state.airfoil, run_dir)
        if airfoil_error:
            return _openvsp_fallback_result(
                inputs=inputs,
                params=params.model_dump(),
                summary=summary.model_dump(),
                run_dir=run_dir,
                reason=airfoil_error,
                conditions=conditions.model_dump(),
                solver_extra={"solver_airfoil": solver_airfoil},
            )

        case = _build_case_geometry(
            params.model_dump(),
            solver_airfoil,
            aoa_start,
            aoa_end,
            aoa_step,
            mach,
            reynolds,
        )
        script_path = run_dir / "run_precision.vspscript"
        script_path.write_text(case["script"], encoding="utf-8")

        solver = _resolve_solver_paths()
        if solver["vsp_exe"] is None:
            return _openvsp_fallback_result(
                inputs=inputs,
                params=params.model_dump(),
                summary=summary.model_dump(),
                run_dir=run_dir,
                reason="OpenVSP solver 실행 파일을 찾을 수 없습니다. third_party/openvsp/win64 또는 AUAV_SOLVER_BIN_DIR에 vsp.exe가 필요합니다.",
                conditions=conditions.model_dump(),
                solver_extra={
                    "script_path": str(script_path),
                    "solver_airfoil": case["solver_airfoil"],
                    "solver_wingtip": case["solver_wingtip"],
                },
            )

        cmd = [str(solver["vsp_exe"]), "-script", str(script_path)]
        env = os.environ.copy()
        env["PATH"] = f"{solver['bin_dir']};{env.get('PATH', '')}"

        proc = subprocess.run(
            cmd,
            cwd=str(run_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        (run_dir / "solver_stdout.log").write_text(stdout, encoding="utf-8")
        (run_dir / "solver_stderr.log").write_text(stderr, encoding="utf-8")

        if proc.returncode != 0:
            return _openvsp_fallback_result(
                inputs=inputs,
                params=params.model_dump(),
                summary=summary.model_dump(),
                run_dir=run_dir,
                reason=f"OpenVSP solver가 비정상 종료되었습니다. 종료 코드: {proc.returncode}",
                conditions=conditions.model_dump(),
                solver_extra={
                    "stdout_tail": _tail(stdout),
                    "stderr_tail": _tail(stderr),
                    "command": cmd,
                    "script_path": str(script_path),
                    "solver_airfoil": case["solver_airfoil"],
                    "solver_wingtip": case["solver_wingtip"],
                },
            )

        polar_path = run_dir / "auav_case.polar"
        curve_payload = _load_openvsp_curve(
            stdout,
            polar_path=polar_path,
            aoa_start=aoa_start,
            aoa_end=aoa_end,
            aoa_step=aoa_step,
        )
        if curve_payload is None:
            return _openvsp_fallback_result(
                inputs=inputs,
                params=params.model_dump(),
                summary=summary.model_dump(),
                run_dir=run_dir,
                reason="Solver는 실행되었지만 신뢰할 수 있는 공력 곡선을 읽어오지 못했습니다.",
                conditions=conditions.model_dump(),
                solver_extra={
                    "stdout_tail": _tail(stdout),
                    "command": cmd,
                    "script_path": str(script_path),
                    "solver_airfoil": case["solver_airfoil"],
                    "solver_wingtip": case["solver_wingtip"],
                },
            )

        curve = AeroCurve(
            aoa_deg=[round(x, 6) for x in curve_payload["curve"]["aoa"]],
            cl=[round(x, 6) for x in curve_payload["curve"]["cl"]],
            cd=[round(x, 6) for x in curve_payload["curve"]["cd"]],
            cm=[round(x, 6) for x in curve_payload["curve"]["cm"]],
        )
        raw_aoa_all = list(curve_payload["raw_aoa_all"])

        vspaero_path = run_dir / "auav_case.vspaero"
        solver_effective_conditions = _extract_solver_effective_conditions(
            requested_conditions=conditions.model_dump(),
            vspaero_case_path=vspaero_path,
            scripted_re_cref=case.get("scripted_re_cref"),
            fallback_mach=mach,
        )
        effective_reynolds = solver_effective_conditions.get("re_cref")
        re_used = (
            float(effective_reynolds)
            if isinstance(effective_reynolds, (int, float)) and float(effective_reynolds) > 0
            else _estimate_reynolds(case["cref"], mach)
        )
        metrics = derive_metrics(curve, reynolds=re_used, oswald=_estimate_oswald(params.aspect_ratio, params.sweep_deg, params.taper_ratio))
        precision_data = _build_precision_data(
            curve, metrics, case["sref"], case["cref"], case["bref"], raw_aoa=raw_aoa_all
        )
        vspaero_all_data = curve_payload.get("vspaero_all_data") or _build_vspaero_all_data_from_rows(curve_payload["summary_rows"])
        vspaero_all_data_raw = curve_payload.get("vspaero_all_data_raw") or {}
        vsp3 = run_dir / "auav_case.vsp3"

        extra_data: dict[str, Any] = {
            "solver_id": "openvsp",
            "solver_label": "OpenVSP/VSPAERO",
            "solver_mode": "openvsp-script",
            "limitation_note": (
                "Potential-flow / thin-surface wing solver output. "
                "Use only overlapping attached-flow ranges for direct comparison with NeuralFoil."
            ),
            "solver_bin_dir": str(solver["bin_dir"]),
            "vsp_exe": str(solver["vsp_exe"]),
            "vspaero_exe": str(solver["vspaero_exe"]) if solver["vspaero_exe"] else None,
            "script_path": str(script_path),
            "stdout_log": str(run_dir / "solver_stdout.log"),
            "stderr_log": str(run_dir / "solver_stderr.log"),
            "run_dir": str(run_dir),
            "row_count": len(curve.aoa_deg),
            "row_count_raw": len(raw_aoa_all),
            "curve_source": curve_payload["source"],
            "curve_filtering": curve_payload["filtering"],
            "requested_aoa_range": curve_payload["filtering"].get("requested_aoa_range"),
            "valid_aoa_range": curve_payload["filtering"].get("used_aoa_range"),
            "selected_coefficient_family": curve_payload.get("selected_coefficient_family"),
            "selected_coefficient_family_label": curve_payload.get("selected_coefficient_family_label"),
            "coefficient_family_selection": curve_payload.get("coefficient_family_selection"),
            "selected_coefficient_columns": curve_payload.get("selected_coefficient_columns"),
            "coefficient_family_candidates": curve_payload.get("coefficient_family_candidates"),
            "Sref": case["sref"],
            "Cref": case["cref"],
            "Bref": case["bref"],
            "result_level": "wing_solver",
            "analysis_conditions": conditions.model_dump(),
            "solver_effective_conditions": solver_effective_conditions,
            "solver_airfoil": case["solver_airfoil"],
            "solver_wingtip": case["solver_wingtip"],
            "used_reynolds": solver_effective_conditions.get("re_cref"),
            "used_mach": solver_effective_conditions.get("mach"),
            "precision_data": precision_data,
            "vspaero_all_data": vspaero_all_data,
            "vspaero_all_data_raw": vspaero_all_data_raw,
            "available_artifacts": [
                "run_precision.vspscript",
                "solver_stdout.log",
                "solver_stderr.log",
                "auav_case.polar" if polar_path.exists() else None,
                "auav_case.vspaero" if vspaero_path.exists() else None,
                "auav_case.vsp3" if vsp3.exists() else None,
            ],
        }

        if vsp3.exists():
            extra_data["vsp3_path"] = str(vsp3)
        extra_data["available_artifacts"] = [item for item in extra_data["available_artifacts"] if item]

        return AnalysisResult(
            source_label=source_label_for("openvsp", "openvsp"),
            curve=curve,
            metrics=metrics,
            analysis_mode="openvsp",
            fallback_reason=None,
            extra_data=extra_data,
            notes=_build_openvsp_notes(
                case["solver_airfoil"],
                curve_filtering=curve_payload["filtering"],
                solver_wingtip=case["solver_wingtip"],
                coefficient_family_label=curve_payload.get("selected_coefficient_family_label"),
                solver_effective_conditions=solver_effective_conditions,
            ),
        )
    except subprocess.TimeoutExpired:
        return _openvsp_fallback_result(
            inputs=inputs,
            params=params.model_dump(),
            summary=summary.model_dump(),
            run_dir=run_dir,
            reason="OpenVSP solver 실행 시간이 초과되었습니다.",
            conditions=conditions.model_dump(),
            solver_extra={"solver_airfoil": _requested_airfoil_meta(state.airfoil)},
        )
    except Exception as exc:
        return _openvsp_fallback_result(
            inputs=inputs,
            params=params.model_dump(),
            summary=summary.model_dump(),
            run_dir=run_dir,
            reason=f"OpenVSP solver 실행에 실패했습니다: {exc}",
            conditions=conditions.model_dump(),
            solver_extra={"solver_airfoil": _requested_airfoil_meta(state.airfoil)},
        )


def _openvsp_fallback_result(
    *,
    inputs: AeroInputs,
    params: dict[str, Any],
    summary: dict[str, Any],
    run_dir: Path,
    reason: str,
    conditions: dict[str, Any],
    solver_extra: dict[str, Any] | None = None,
) -> AnalysisResult:
    curve, metrics = build_surrogate_curve(inputs, precision_mode=True)
    sref = max(1e-5, inputs.span_m * inputs.span_m / max(1.0, inputs.aspect_ratio))
    cref = max(0.02, inputs.span_m / max(1.2, inputs.aspect_ratio))
    bref = max(0.02, inputs.span_m)
    precision_data = _build_precision_data(curve, metrics, sref, cref, bref)

    extra_data: dict[str, Any] = {
        "solver_id": "openvsp",
        "solver_label": "OpenVSP/VSPAERO",
        "solver_mode": "surrogate-fallback",
        "limitation_note": (
            "Potential-flow / thin-surface wing solver output. "
            "Fallback results must not be treated as solver-to-solver fair comparisons."
        ),
        "reason": reason,
        "fallback_reason": reason,
        "params": params,
        "airfoil_summary": summary,
        "run_dir": str(run_dir),
        "analysis_conditions": conditions,
        "result_level": "wing_solver_fallback",
        "precision_data": precision_data,
        "available_artifacts": [],
    }
    if solver_extra:
        extra_data.update(solver_extra)

    return AnalysisResult(
        source_label=source_label_for("openvsp", "fallback"),
        curve=curve,
        metrics=metrics,
        analysis_mode="fallback",
        fallback_reason=reason,
        extra_data=extra_data,
        notes=f"OpenVSP/VSPAERO 경로가 대체 해석으로 전환되었습니다: {reason}",
    )


def _resolve_solver_paths() -> dict[str, Path | None]:
    candidates: list[Path] = []

    env_dir = os.getenv("AUAV_SOLVER_BIN_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    resources_path = os.getenv("AUAV_RESOURCES_PATH")
    if resources_path:
        candidates.append(Path(resources_path) / "bin" / "win64")

    repo_default = Path(__file__).resolve().parents[3] / "third_party" / "openvsp" / "win64"
    candidates.append(repo_default)

    for d in candidates:
        vsp = d / "vsp.exe"
        vspaero = d / "vspaero.exe"
        if vsp.exists():
            return {"bin_dir": d, "vsp_exe": vsp, "vspaero_exe": vspaero if vspaero.exists() else None}
    return {"bin_dir": None, "vsp_exe": None, "vspaero_exe": None}


def _build_case_geometry(
    params: dict[str, Any],
    solver_airfoil: dict[str, Any],
    aoa_start: float,
    aoa_end: float,
    aoa_step: float,
    mach: float,
    reynolds: float | None,
) -> dict[str, Any]:
    span = float(params["span_m"])
    ar = float(params["aspect_ratio"])
    taper = float(params["taper_ratio"])
    sweep = float(params["sweep_deg"])
    dihedral = float(params["dihedral_deg"])
    twist = float(params["twist_deg"])

    area = max(1e-5, span * span / max(1.0, ar))
    c_root = max(1e-4, (2.0 * area) / (span * (1.0 + taper)))
    c_tip = c_root * taper
    semi = span * 0.5
    mac = (2.0 / 3.0) * c_root * ((1.0 + taper + taper * taper) / (1.0 + taper))
    requested_wingtip_style = str(params.get("wingtip_style") or "straight").strip().lower()
    if requested_wingtip_style not in ("straight", "pinched"):
        requested_wingtip_style = "straight"

    alpha_npts = max(2, int(round((aoa_end - aoa_start) / max(0.25, aoa_step))) + 1)
    airfoil_script = _build_airfoil_script(solver_airfoil)
    reynolds_block = ""
    if reynolds is not None and reynolds > 0:
        reynolds_block = f"""
    array< double > reCref;
    reCref.push_back( {reynolds:.6f} );
    SetDoubleAnalysisInput( analysis_name, "ReCref", reCref, 0 );

    array< double > reCrefEnd;
    reCrefEnd.push_back( {reynolds:.6f} );
    SetDoubleAnalysisInput( analysis_name, "ReCrefEnd", reCrefEnd, 0 );

    array< int > reCrefNpts;
    reCrefNpts.push_back( 1 );
    SetIntAnalysisInput( analysis_name, "ReCrefNpts", reCrefNpts, 0 );
"""

    script = f"""void main()
{{
    string wid = AddGeom( "WING", "" );
    SetParmVal( wid, "Sym_Planar_Flag", "Sym", SYM_XZ );
    SetParmVal( wid, "RotateAirfoilMatchDideralFlag", "WingGeom", 1.0 );
    SetParmVal( wid, "Span", "XSec_1", {semi:.6f} );
    SetParmVal( wid, "Root_Chord", "XSec_1", {c_root:.6f} );
    SetParmVal( wid, "Tip_Chord", "XSec_1", {c_tip:.6f} );
    SetParmVal( wid, "Sweep", "XSec_1", {sweep:.6f} );
    SetParmVal( wid, "Dihedral", "XSec_1", {dihedral:.6f} );
    SetParmVal( wid, "Twist", "XSec_1", {twist:.6f} );
    SetParmVal( wid, "Tess_W", "Shape", 45 );
    SetParmVal( wid, "SectTess_U", "XSec_1", 20 );
    Update();
{airfoil_script}

    WriteVSPFile( "auav_case.vsp3", SET_ALL );

    string compgeom_name = "VSPAEROComputeGeometry";
    SetAnalysisInputDefaults( compgeom_name );
    array< int > thick_set = GetIntAnalysisInput( compgeom_name, "GeomSet" );
    array< int > thin_set = GetIntAnalysisInput( compgeom_name, "ThinGeomSet" );
    thick_set[0] = ( SET_TYPE::SET_NONE );
    thin_set[0] = ( SET_TYPE::SET_ALL );
    SetIntAnalysisInput( compgeom_name, "GeomSet", thick_set );
    SetIntAnalysisInput( compgeom_name, "ThinGeomSet", thin_set );
    ExecAnalysis( compgeom_name );

    string analysis_name = "VSPAEROSweep";
    SetAnalysisInputDefaults( analysis_name );

    array< int > geom_set;
    geom_set.push_back( 0 );
    SetIntAnalysisInput( analysis_name, "GeomSet", geom_set, 0 );

    array< int > ref_flag;
    ref_flag.push_back( 0 );
    SetIntAnalysisInput( analysis_name, "RefFlag", ref_flag, 0 );

    array< double > sref;
    sref.push_back( {area:.6f} );
    SetDoubleAnalysisInput( analysis_name, "Sref", sref, 0 );

    array< double > cref;
    cref.push_back( {mac:.6f} );
    SetDoubleAnalysisInput( analysis_name, "cref", cref, 0 );

    array< double > bref;
    bref.push_back( {span:.6f} );
    SetDoubleAnalysisInput( analysis_name, "bref", bref, 0 );

    array< double > alphaStart;
    alphaStart.push_back( {aoa_start:.6f} );
    SetDoubleAnalysisInput( analysis_name, "AlphaStart", alphaStart, 0 );

    array< double > alphaEnd;
    alphaEnd.push_back( {aoa_end:.6f} );
    SetDoubleAnalysisInput( analysis_name, "AlphaEnd", alphaEnd, 0 );

    array< int > alphaNpts;
    alphaNpts.push_back( {alpha_npts} );
    SetIntAnalysisInput( analysis_name, "AlphaNpts", alphaNpts, 0 );

    array< double > machStart;
    machStart.push_back( {mach:.6f} );
    SetDoubleAnalysisInput( analysis_name, "MachStart", machStart, 0 );

    array< int > machNpts;
    machNpts.push_back( 1 );
    SetIntAnalysisInput( analysis_name, "MachNpts", machNpts, 0 );
{reynolds_block}

    array< int > wakeIter;
    wakeIter.push_back( 3 );
    SetIntAnalysisInput( analysis_name, "WakeNumIter", wakeIter, 0 );

    ExecAnalysis( analysis_name );

    while ( GetNumTotalErrors() > 0 )
    {{
        ErrorObj err = PopLastError();
        Print( "AUAV_ERR=" + err.GetErrorString() );
    }}
}}
"""

    return {
        "script": script,
        "sref": area,
        "cref": mac,
        "bref": span,
        "scripted_re_cref": float(reynolds) if reynolds is not None and reynolds > 0 else None,
        "solver_airfoil": solver_airfoil,
        "solver_wingtip": {
            "requested_style": requested_wingtip_style,
            "used_style": "straight",
            "geometry_kind": "single_taper_section",
            "degraded_note": (
                "조임형 윙팁 프리뷰는 현재 OpenVSP 해석에서 등가 직선 테이퍼 팁으로 근사합니다."
                if requested_wingtip_style == "pinched"
                else None
            ),
        },
    }


def _prepare_solver_airfoil(airfoil: AirfoilState, run_dir: Path) -> tuple[dict[str, Any], str | None]:
    requested = _requested_airfoil_meta(airfoil)
    naca_code = _extract_naca4_code(requested["requested_label"])

    if naca_code:
        camber, camber_loc, thickness = _naca4_parameters(naca_code)
        geometry_kind = "naca4"
        degraded_note = None
        if "approx" in requested["requested_label"].lower():
            geometry_kind = "naca4_approx"
            degraded_note = "UI와 동일한 근사 NACA 형상으로 OpenVSP 해석을 수행했습니다."
        requested.update(
            {
                "representation_label": f"NACA {naca_code}",
                "geometry_kind": geometry_kind,
                "camber": camber,
                "camber_loc": camber_loc,
                "thickness": thickness,
                "degraded_note": degraded_note,
            }
        )
        return requested, None

    coords = _solver_airfoil_coords(airfoil)
    if len(coords) < 6:
        requested.update({"geometry_kind": "unsupported"})
        return requested, "선택한 에어포일을 OpenVSP 형상으로 표현할 수 없습니다. 사용 가능한 NACA 코드나 좌표 세트가 없습니다."

    airfoil_path = run_dir / "solver_airfoil.af"
    _write_airfoil_file(airfoil_path, requested["requested_label"], coords)
    requested.update(
        {
            "representation_label": airfoil_path.name,
            "geometry_kind": "custom_file",
            "file_name": airfoil_path.name,
            "file_path": str(airfoil_path),
        }
    )
    return requested, None


def _requested_airfoil_meta(airfoil: AirfoilState) -> dict[str, Any]:
    coords = _solver_airfoil_coords(airfoil)
    return {
        "requested_label": str(airfoil.summary.code or "").strip() or "이름 없는 에어포일",
        "coordinate_count": len(coords),
    }


def _solver_airfoil_coords(airfoil: AirfoilState) -> list[list[float]]:
    if airfoil.coords:
        return [[float(p[0]), float(p[1])] for p in airfoil.coords]
    if airfoil.upper and airfoil.lower:
        return [[float(p[0]), float(p[1])] for p in (airfoil.upper[::-1] + airfoil.lower[1:])]
    return []


def _extract_naca4_code(label: str) -> str | None:
    match = _NACA4_RE.search(str(label or ""))
    if not match:
        return None
    return match.group(1)


def _naca4_parameters(code: str) -> tuple[float, float, float]:
    camber = int(code[0]) / 100.0
    camber_loc = int(code[1]) / 10.0
    thickness = int(code[2:]) / 100.0
    if camber <= 0.0:
        camber_loc = 0.4
    return camber, camber_loc, thickness


def _build_airfoil_script(solver_airfoil: dict[str, Any]) -> str:
    geometry_kind = str(solver_airfoil.get("geometry_kind") or "")
    if geometry_kind in {"naca4", "naca4_approx"}:
        camber = float(solver_airfoil["camber"])
        camber_loc = float(solver_airfoil["camber_loc"])
        thickness = float(solver_airfoil["thickness"])
        return f"""    string xsec_surf = GetXSecSurf( wid, 0 );
    ChangeXSecShape( xsec_surf, 0, XS_FOUR_SERIES );
    ChangeXSecShape( xsec_surf, 1, XS_FOUR_SERIES );
    Update();
    string xsec0 = GetXSec( xsec_surf, 0 );
    string xsec1 = GetXSec( xsec_surf, 1 );
    SetParmVal( GetXSecParm( xsec0, "Camber" ), {camber:.6f} );
    SetParmVal( GetXSecParm( xsec0, "CamberLoc" ), {camber_loc:.6f} );
    SetParmVal( GetXSecParm( xsec0, "ThickChord" ), {thickness:.6f} );
    SetParmVal( GetXSecParm( xsec1, "Camber" ), {camber:.6f} );
    SetParmVal( GetXSecParm( xsec1, "CamberLoc" ), {camber_loc:.6f} );
    SetParmVal( GetXSecParm( xsec1, "ThickChord" ), {thickness:.6f} );
    Update();"""

    file_name = _vsp_string(str(solver_airfoil.get("file_name") or "solver_airfoil.af"))
    return f"""    string xsec_surf = GetXSecSurf( wid, 0 );
    ChangeXSecShape( xsec_surf, 0, XS_FILE_AIRFOIL );
    string xsec0 = GetXSec( xsec_surf, 0 );
    ReadFileAirfoil( xsec0, "{file_name}" );
    ChangeXSecShape( xsec_surf, 1, XS_FILE_AIRFOIL );
    string xsec1 = GetXSec( xsec_surf, 1 );
    ReadFileAirfoil( xsec1, "{file_name}" );
    Update();"""


def _write_airfoil_file(path: Path, label: str, coords: list[list[float]]) -> None:
    lines = [label or "AUAVWDS Airfoil"]
    lines.extend(f"{float(x):.6f} {float(z):.6f}" for x, z in coords)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _vsp_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_openvsp_notes(
    solver_airfoil: dict[str, Any],
    curve_filtering: dict[str, Any] | None = None,
    solver_wingtip: dict[str, Any] | None = None,
    coefficient_family_label: str | None = None,
    solver_effective_conditions: dict[str, Any] | None = None,
) -> str:
    requested = str(solver_airfoil.get("requested_label") or "선택한 에어포일")
    geometry_kind = str(solver_airfoil.get("geometry_kind") or "")
    filtering = curve_filtering or {}
    wingtip = solver_wingtip or {}
    dropped = int(filtering.get("dropped_row_count") or 0)
    aoa_range = filtering.get("used_aoa_range")
    requested_range = filtering.get("requested_aoa_range")

    suffix = ""
    if isinstance(requested_range, dict) and isinstance(aoa_range, dict):
        requested_start = requested_range.get("start")
        requested_end = requested_range.get("end")
        start = aoa_range.get("start")
        end = aoa_range.get("end")
        if all(isinstance(v, (int, float)) for v in (requested_start, requested_end, start, end)):
            suffix = (
                f" 요청한 해석 범위는 {float(requested_start):.1f}°~{float(requested_end):.1f}°입니다."
            )
            if dropped > 0:
                suffix += (
                    f" 수렴이 불안정하거나 물리적으로 부적절한 받음각 {dropped}개 행은 제외하고 "
                    f"{float(start):.1f}°~{float(end):.1f}° 유효 구간만 결과에 반영했습니다."
                )
            else:
                suffix += f" 전체 요청 구간을 그대로 결과에 반영했습니다."

    if isinstance(coefficient_family_label, str) and coefficient_family_label.strip():
        suffix += f" 주 계수 계열은 {coefficient_family_label.strip()}입니다."

    effective = solver_effective_conditions or {}
    reynolds_note = effective.get("reynolds_note")
    if isinstance(reynolds_note, str) and reynolds_note.strip():
        suffix += f" {reynolds_note.strip()}"

    if geometry_kind == "custom_file":
        base = f"{requested}의 좌표 파일을 사용해 OpenVSP/VSPAERO 정밀 해석을 완료했습니다."
    else:
        degraded_note = solver_airfoil.get("degraded_note")
        if isinstance(degraded_note, str) and degraded_note.strip():
            base = degraded_note
        else:
            base = f"{requested} 형상을 사용해 OpenVSP/VSPAERO 정밀 해석을 완료했습니다."

    tip_note = wingtip.get("degraded_note")
    if isinstance(tip_note, str) and tip_note.strip():
        base = f"{base} {tip_note}"

    return f"{base}{suffix}"


def _load_openvsp_curve(
    stdout: str,
    *,
    polar_path: Path,
    aoa_start: float,
    aoa_end: float,
    aoa_step: float,
) -> dict[str, Any] | None:
    polar_parsed = _parse_polar_rows(polar_path)
    if polar_parsed is not None:
        headers, rows = polar_parsed
        payload = _finalize_polar_curve_payload(
            headers=headers,
            rows=rows,
            aoa_start=aoa_start,
            aoa_end=aoa_end,
            aoa_step=aoa_step,
        )
        if payload is not None:
            return payload

    stdout_rows = _extract_curve_rows_from_stdout(stdout)
    selection = _select_stable_curve_rows(stdout_rows, aoa_step=aoa_step)
    if len(selection["rows"]) < 3:
        return None

    filter_meta = dict(selection["filtering"])
    filter_meta["requested_aoa_range"] = {"start": float(aoa_start), "end": float(aoa_end)}
    return {
        "source": "stdout_filtered" if filter_meta.get("dropped_row_count", 0) else "stdout",
        "curve": _curve_rows_to_curve_payload(
            selection["rows"],
            aoa_start=aoa_start,
            aoa_end=aoa_end,
            aoa_step=aoa_step,
        ),
        "raw_aoa": [float(row["aoa"]) for row in selection["rows"]],
        "raw_aoa_all": [float(row["aoa"]) for row in stdout_rows],
        "summary_rows": selection["rows"],
        "vspaero_all_data": _build_vspaero_all_data_from_rows(selection["rows"]),
        "vspaero_all_data_raw": {},
        "filtering": filter_meta,
        "selected_coefficient_family": "stdout_solver_table",
        "selected_coefficient_family_label": "solver stdout table",
        "coefficient_family_selection": "stdout_single_family",
        "selected_coefficient_columns": {
            "cl": "CL",
            "cd": "CD",
            "cm": "CM",
            "cdo": "CDo",
            "cdi": "CDi",
            "ld": "L/D",
            "e": "E",
        },
        "coefficient_family_candidates": {},
    }


def _finalize_polar_curve_payload(
    *,
    headers: list[str],
    rows: list[dict[str, float]],
    aoa_start: float,
    aoa_end: float,
    aoa_step: float,
) -> dict[str, Any] | None:
    families = _extract_curve_families_from_polar(headers, rows)
    evaluations = [
        _evaluate_curve_family(family, aoa_start=aoa_start, aoa_end=aoa_end, aoa_step=aoa_step)
        for family in families
    ]
    usable = [item for item in evaluations if len(item["selected_rows"]) >= 3]
    if not usable:
        return None

    usable.sort(
        key=lambda item: (
            -float(item["score"]),
            -int(item["filtering"].get("valid_row_count") or 0),
            -float(item["selected_rows"][-1]["aoa"] - item["selected_rows"][0]["aoa"]),
        )
    )
    chosen = usable[0]
    filter_meta = dict(chosen["filtering"])
    filter_meta["requested_aoa_range"] = {"start": float(aoa_start), "end": float(aoa_end)}
    chosen_summary_rows = [chosen["summary_rows"][idx] for idx in chosen["selected_indices"]]

    return {
        "source": "polar_filtered" if filter_meta.get("dropped_row_count", 0) else "polar",
        "curve": _curve_rows_to_curve_payload(
            chosen["selected_rows"],
            aoa_start=aoa_start,
            aoa_end=aoa_end,
            aoa_step=aoa_step,
        ),
        "raw_aoa": [float(row["aoa"]) for row in chosen["selected_rows"]],
        "raw_aoa_all": [float(row["aoa"]) for row in chosen["curve_rows"]],
        "summary_rows": chosen["selected_rows"],
        "vspaero_all_data": _build_vspaero_all_data_from_headers_and_rows(chosen["summary_headers"], chosen_summary_rows),
        "vspaero_all_data_raw": _build_vspaero_all_data_from_headers_and_rows(headers, rows),
        "filtering": filter_meta,
        "selected_coefficient_family": chosen["id"],
        "selected_coefficient_family_label": chosen["label"],
        "coefficient_family_selection": "dynamic_family_selection",
        "selected_coefficient_columns": chosen["selected_columns"],
        "coefficient_family_candidates": {
            item["id"]: _build_curve_family_candidate_summary(item, selected=item["id"] == chosen["id"])
            for item in evaluations
        },
    }


def _extract_curve_rows_from_stdout(stdout: str) -> list[dict[str, float]]:
    rows_by_aoa: dict[float, dict[str, float]] = {}
    for raw in stdout.splitlines():
        m = _ROW_RE.match(raw)
        if not m:
            continue

        parts = m.groups()
        row = {
            "iter": float(int(parts[0])),
            "mach": float(parts[1]),
            "aoa": float(parts[2]),
            "beta": float(parts[3]),
            "cl": float(parts[6]),
            "cdo": float(parts[7]),
            "cdi": float(parts[8]),
            "cd": float(parts[9]),
            "ld": float(parts[10]),
            "e": float(parts[11]),
            "cm": float(parts[13]),
        }

        aoa = row["aoa"]
        prev = rows_by_aoa.get(aoa)
        if prev is None or row["iter"] >= prev["iter"]:
            rows_by_aoa[aoa] = row

    rows = [rows_by_aoa[aoa] for aoa in sorted(rows_by_aoa.keys())]
    return _normalize_curve_row_sign(rows)


def _extract_curve_families_from_polar(headers: list[str], rows: list[dict[str, float]]) -> list[dict[str, Any]]:
    header_map = {_norm_header_key(h): h for h in headers}
    aoa_key = header_map.get("aoa")
    if not aoa_key:
        return []

    families: list[dict[str, Any]] = []
    for spec in _POLAR_FAMILY_SPECS:
        resolved_columns: dict[str, str] = {}
        missing_required = False
        for field, normalized_key in spec["selection_columns"].items():
            raw_key = header_map.get(normalized_key)
            if raw_key is None and field in {"cl", "cd", "cm"}:
                missing_required = True
                break
            if raw_key is not None:
                resolved_columns[field] = raw_key
        if missing_required:
            continue

        curve_rows: list[dict[str, float]] = []
        for source_row in rows:
            curve_rows.append(
                {
                    "aoa": float(source_row.get(aoa_key, float("nan"))),
                    "cl": float(source_row.get(resolved_columns["cl"], float("nan"))),
                    "cd": float(source_row.get(resolved_columns["cd"], float("nan"))),
                    "cm": float(source_row.get(resolved_columns["cm"], float("nan"))),
                    "cdo": float(source_row.get(resolved_columns.get("cdo", ""), float("nan"))) if resolved_columns.get("cdo") else float("nan"),
                    "cdi": float(source_row.get(resolved_columns.get("cdi", ""), float("nan"))) if resolved_columns.get("cdi") else float("nan"),
                    "ld": float(source_row.get(resolved_columns.get("ld", ""), float("nan"))) if resolved_columns.get("ld") else float("nan"),
                    "e": float(source_row.get(resolved_columns.get("e", ""), float("nan"))) if resolved_columns.get("e") else float("nan"),
                }
            )

        normalized_rows = _normalize_curve_row_sign(curve_rows)
        summary_headers = [aoa_key]
        for field in ("cl", "cd", "cm", "cdo", "cdi", "ld", "e"):
            raw_key = resolved_columns.get(field)
            if raw_key and raw_key not in summary_headers:
                summary_headers.append(raw_key)

        summary_rows: list[dict[str, float]] = []
        for row in normalized_rows:
            summary_row: dict[str, float] = {aoa_key: float(row["aoa"])}
            if resolved_columns.get("cl"):
                summary_row[resolved_columns["cl"]] = float(row["cl"])
            if resolved_columns.get("cd"):
                summary_row[resolved_columns["cd"]] = float(row["cd"])
            if resolved_columns.get("cm"):
                summary_row[resolved_columns["cm"]] = float(row["cm"])
            if resolved_columns.get("cdo") and math.isfinite(float(row.get("cdo", float("nan")))):
                summary_row[resolved_columns["cdo"]] = float(row["cdo"])
            if resolved_columns.get("cdi") and math.isfinite(float(row.get("cdi", float("nan")))):
                summary_row[resolved_columns["cdi"]] = float(row["cdi"])
            if resolved_columns.get("ld") and math.isfinite(float(row.get("ld", float("nan")))):
                summary_row[resolved_columns["ld"]] = float(row["ld"])
            if resolved_columns.get("e") and math.isfinite(float(row.get("e", float("nan")))):
                summary_row[resolved_columns["e"]] = float(row["e"])
            summary_rows.append(summary_row)

        families.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "curve_rows": normalized_rows,
                "summary_headers": summary_headers,
                "summary_rows": summary_rows,
                "selected_columns": {field: raw_key for field, raw_key in resolved_columns.items()},
            }
        )

    return families


def _normalize_curve_row_sign(rows: list[dict[str, float]]) -> list[dict[str, float]]:
    if len(rows) < 3:
        return rows

    near_zero = [row for row in rows if abs(float(row["aoa"])) <= 2.5]
    if len(near_zero) >= 2:
        sample = near_zero
    else:
        idx = min(range(len(rows)), key=lambda i: abs(float(rows[i]["aoa"])))
        sample = rows[max(0, idx - 1): min(len(rows), idx + 2)]

    if len(sample) < 2:
        return rows

    first = sample[0]
    last = sample[-1]
    delta_aoa = float(last["aoa"]) - float(first["aoa"])
    if abs(delta_aoa) < 1e-9:
        return rows

    slope = (float(last["cl"]) - float(first["cl"])) / delta_aoa
    if slope >= 0:
        return rows

    for row in rows:
        row["cl"] = -float(row["cl"])
        row["cm"] = -float(row["cm"])
        if math.isfinite(float(row.get("ld", float("nan")))):
            row["ld"] = -float(row["ld"])
    return rows


def _select_stable_curve_rows(
    rows: list[dict[str, float]],
    *,
    aoa_step: float,
) -> dict[str, Any]:
    filtering: dict[str, Any] = {
        "raw_row_count": len(rows),
        "plausible_row_count": 0,
        "valid_row_count": 0,
        "dropped_row_count": len(rows),
        "dropped_aoa": [float(row["aoa"]) for row in rows if math.isfinite(float(row.get("aoa", float("nan"))))],
        "used_aoa_range": None,
        "exclusion_reason_summary": {},
    }
    if not rows:
        return {"rows": [], "indices": [], "filtering": filtering}

    valid_entries: list[tuple[int, dict[str, float]]] = []
    reason_counts: dict[str, int] = {}
    for idx, row in enumerate(rows):
        reason = _curve_row_rejection_reason(row)
        if reason is None:
            valid_entries.append((idx, row))
            continue
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    filtering["plausible_row_count"] = len(valid_entries)
    if len(valid_entries) < 3:
        filtering["exclusion_reason_summary"] = reason_counts
        return {"rows": [], "indices": [], "filtering": filtering}

    segments: list[list[tuple[int, dict[str, float]]]] = []
    current: list[tuple[int, dict[str, float]]] = []
    max_gap = max(0.26, float(aoa_step) * 2.25)

    for entry in valid_entries:
        if not current:
            current = [entry]
            continue

        _, row = entry
        _, prev = current[-1]
        if abs(float(row["aoa"]) - float(prev["aoa"])) <= max_gap:
            current.append(entry)
        else:
            segments.append(current)
            current = [entry]

    if current:
        segments.append(current)

    if not segments:
        filtering["exclusion_reason_summary"] = reason_counts
        return {"rows": [], "indices": [], "filtering": filtering}

    segments.sort(
        key=lambda segment: (
            not any(abs(float(row["aoa"])) <= max(0.51, float(aoa_step)) for _, row in segment),
            -len(segment),
            abs(min(float(row["aoa"]) for _, row in segment)),
        )
    )
    chosen = segments[0]
    chosen_indices = [idx for idx, _ in chosen]
    chosen_rows = [rows[idx] for idx in chosen_indices]
    chosen_index_set = set(chosen_indices)
    outside_segment = len(valid_entries) - len(chosen)
    if outside_segment > 0:
        reason_counts["outside_stable_segment"] = reason_counts.get("outside_stable_segment", 0) + outside_segment

    filtering.update(
        {
            "valid_row_count": len(chosen_rows),
            "dropped_row_count": len(rows) - len(chosen_rows),
            "dropped_aoa": [
                float(row["aoa"])
                for idx, row in enumerate(rows)
                if idx not in chosen_index_set and math.isfinite(float(row.get("aoa", float("nan"))))
            ],
            "used_aoa_range": {
                "start": float(chosen_rows[0]["aoa"]),
                "end": float(chosen_rows[-1]["aoa"]),
            },
            "exclusion_reason_summary": reason_counts,
        }
    )
    return {"rows": chosen_rows, "indices": chosen_indices, "filtering": filtering}


def _curve_row_rejection_reason(row: dict[str, float]) -> str | None:
    aoa = float(row.get("aoa", float("nan")))
    cl = float(row.get("cl", float("nan")))
    cd = float(row.get("cd", float("nan")))
    cm = float(row.get("cm", float("nan")))
    cdo = float(row.get("cdo", float("nan")))
    cdi = float(row.get("cdi", float("nan")))
    ld = float(row.get("ld", float("nan")))
    e = float(row.get("e", float("nan")))

    if not all(math.isfinite(v) for v in (aoa, cl, cd, cm)):
        return "nonfinite"
    if abs(cl) > 3.5:
        return "cl_out_of_range"
    if abs(cm) > 2.5:
        return "cm_out_of_range"
    if cd <= 1e-4:
        return "nonpositive_cd"
    if cd > 1.5:
        return "excessive_cd"
    if math.isfinite(cdo) and cdo > 0.5:
        return "excessive_profile_drag"
    if math.isfinite(cdo) and cdo < -1e-4:
        return "negative_profile_drag"
    if math.isfinite(cdi) and cdi > 1.5:
        return "excessive_induced_drag"
    if math.isfinite(cdi) and cdi < -5e-4:
        return "negative_induced_drag"
    if math.isfinite(e) and e < -1e-6:
        return "oswald_out_of_range"

    ld_mag = abs(cl / cd) if not math.isfinite(ld) and cd > 0 else abs(ld)
    if math.isfinite(ld_mag) and ld_mag > 120.0:
        return "ld_out_of_range"

    return None


def _evaluate_curve_family(
    family: dict[str, Any],
    *,
    aoa_start: float,
    aoa_end: float,
    aoa_step: float,
) -> dict[str, Any]:
    selection = _select_stable_curve_rows(family["curve_rows"], aoa_step=aoa_step)
    score = _score_curve_family(
        family["curve_rows"],
        selection["rows"],
        filtering=selection["filtering"],
        aoa_start=aoa_start,
        aoa_end=aoa_end,
        aoa_step=aoa_step,
    )
    return {
        "id": family["id"],
        "label": family["label"],
        "curve_rows": family["curve_rows"],
        "summary_rows": family["summary_rows"],
        "summary_headers": family["summary_headers"],
        "selected_rows": selection["rows"],
        "selected_indices": selection["indices"],
        "selected_columns": family["selected_columns"],
        "filtering": selection["filtering"],
        "score": score,
    }


def _score_curve_family(
    all_rows: list[dict[str, float]],
    selected_rows: list[dict[str, float]],
    *,
    filtering: dict[str, Any],
    aoa_start: float,
    aoa_end: float,
    aoa_step: float,
) -> float:
    if len(selected_rows) < 3:
        return float("-inf")

    used_range = filtering.get("used_aoa_range") or {}
    span = float(used_range.get("end", selected_rows[-1]["aoa"])) - float(used_range.get("start", selected_rows[0]["aoa"]))
    requested_mid = (float(aoa_start) + float(aoa_end)) * 0.5
    used_mid = (float(selected_rows[0]["aoa"]) + float(selected_rows[-1]["aoa"])) * 0.5
    midpoint_penalty = abs(used_mid - requested_mid)
    includes_zero = any(abs(float(row["aoa"])) <= max(0.51, float(aoa_step)) for row in selected_rows)
    reason_counts = filtering.get("exclusion_reason_summary") or {}
    reversals = _count_cl_slope_reversals(selected_rows)

    score = 0.0
    score += float(len(selected_rows)) * 12.0
    score += float(filtering.get("plausible_row_count") or 0) * 1.5
    score += span * 1.75
    if includes_zero:
        score += 8.0
    score -= midpoint_penalty * 1.25
    score -= float(reason_counts.get("nonpositive_cd", 0)) * 20.0
    score -= float(reason_counts.get("negative_profile_drag", 0)) * 8.0
    score -= float(reason_counts.get("negative_induced_drag", 0)) * 8.0
    score -= float(reason_counts.get("outside_stable_segment", 0)) * 1.5
    score -= float(reason_counts.get("excessive_cd", 0)) * 2.5
    score -= float(reversals) * 4.0
    score -= max(0.0, float(len(all_rows) - len(selected_rows))) * 0.25
    return score


def _count_cl_slope_reversals(rows: list[dict[str, float]]) -> int:
    reversals = 0
    prev_sign = 0
    for idx in range(1, len(rows)):
        delta_aoa = float(rows[idx]["aoa"]) - float(rows[idx - 1]["aoa"])
        if abs(delta_aoa) < 1e-9:
            continue
        slope = (float(rows[idx]["cl"]) - float(rows[idx - 1]["cl"])) / delta_aoa
        if abs(slope) < 1e-9:
            continue
        sign = 1 if slope > 0 else -1
        if prev_sign and sign != prev_sign:
            reversals += 1
        prev_sign = sign
    return reversals


def _build_curve_family_candidate_summary(candidate: dict[str, Any], *, selected: bool) -> dict[str, Any]:
    filtering = candidate["filtering"]
    summary: dict[str, Any] = {
        "label": candidate["label"],
        "available": bool(candidate["curve_rows"]),
        "selected": selected,
        "raw_row_count": filtering.get("raw_row_count", 0),
        "plausible_row_count": filtering.get("plausible_row_count", 0),
        "valid_row_count": filtering.get("valid_row_count", 0),
        "dropped_row_count": filtering.get("dropped_row_count", 0),
        "used_aoa_range": filtering.get("used_aoa_range"),
        "columns": candidate["selected_columns"],
    }
    if candidate["score"] != float("-inf"):
        summary["score"] = round(float(candidate["score"]), 3)
    exclusion_summary = filtering.get("exclusion_reason_summary")
    if isinstance(exclusion_summary, dict) and exclusion_summary:
        summary["exclusion_reason_summary"] = exclusion_summary
    return summary


def _curve_rows_to_curve_payload(
    rows: list[dict[str, float]],
    *,
    aoa_start: float,
    aoa_end: float,
    aoa_step: float,
) -> dict[str, list[float]]:
    parsed = {
        "aoa": [float(row["aoa"]) for row in rows],
        "cl": [float(row["cl"]) for row in rows],
        "cd": [float(row["cd"]) for row in rows],
        "cm": [float(row["cm"]) for row in rows],
    }

    if len(rows) < 2:
        return parsed

    start = max(float(aoa_start), float(rows[0]["aoa"]))
    end = min(float(aoa_end), float(rows[-1]["aoa"]))
    if end - start < max(0.25, float(aoa_step)) * 0.5:
        return parsed

    return _resample_curve_to_unit_aoa(parsed, aoa_start=start, aoa_end=end, aoa_step=aoa_step)


def _resample_curve_to_unit_aoa(
    parsed: dict[str, list[float]],
    *,
    aoa_start: float,
    aoa_end: float,
    aoa_step: float = 1.0,
) -> dict[str, list[float]]:
    aoa = parsed.get("aoa", [])
    cl = parsed.get("cl", [])
    cd = parsed.get("cd", [])
    cm = parsed.get("cm", [])

    if len(aoa) < 2:
        return parsed

    # Ensure strictly sorted interpolation inputs.
    order = np.argsort(np.array(aoa, dtype=float))
    x = np.array([aoa[i] for i in order], dtype=float)
    y_cl = np.array([cl[i] for i in order], dtype=float)
    y_cd = np.array([cd[i] for i in order], dtype=float)
    y_cm = np.array([cm[i] for i in order], dtype=float)

    # Remove duplicate x values (keep first occurrence after sorting).
    x_unique, unique_idx = np.unique(x, return_index=True)
    y_cl = y_cl[unique_idx]
    y_cd = y_cd[unique_idx]
    y_cm = y_cm[unique_idx]

    if len(x_unique) < 2:
        return parsed

    target = np.arange(float(aoa_start), float(aoa_end) + 1e-9, float(aoa_step), dtype=float)
    cl_i = np.interp(target, x_unique, y_cl)
    cd_i = np.interp(target, x_unique, y_cd)
    cm_i = np.interp(target, x_unique, y_cm)

    return {
        "aoa": [float(v) for v in target.tolist()],
        "cl": [float(v) for v in cl_i.tolist()],
        "cd": [float(max(1e-6, v)) for v in cd_i.tolist()],
        "cm": [float(v) for v in cm_i.tolist()],
    }


def _estimate_oswald(ar: float, sweep_deg: float, taper: float) -> float:
    e = 0.84 - 0.002 * abs(sweep_deg) - 0.06 * abs(taper - 0.45)
    if ar > 14:
        e -= 0.03
    return max(0.55, min(0.95, e))


def _estimate_reynolds(cref: float, mach: float, nu: float = 1.5e-5) -> float:
    speed = max(0.1, float(mach) * 340.3)
    return speed * max(0.02, float(cref)) / max(1e-7, float(nu))


def _tail(text: str, lines: int = 40) -> str:
    split = text.splitlines()
    return "\n".join(split[-lines:])


def _build_precision_data(
    curve: AeroCurve,
    metrics: Any,
    sref: float,
    cref: float,
    bref: float,
    raw_aoa: list[float] | None = None,
) -> dict[str, float]:
    aoa = curve.aoa_deg or []
    cl = curve.cl or []
    cd = curve.cd or []
    cm = curve.cm or []
    raw = raw_aoa or []

    ld: list[float] = []
    for i, cl_i in enumerate(cl):
        cd_i = cd[i] if i < len(cd) else 0.0
        ld.append(float(cl_i / cd_i) if abs(cd_i) > 1e-9 else 0.0)

    aoa_step = 0.0
    if len(aoa) >= 2:
        aoa_step = float((aoa[-1] - aoa[0]) / max(1, len(aoa) - 1))

    raw_step = 0.0
    if len(raw) >= 2:
        raw_step = float((max(raw) - min(raw)) / max(1, len(raw) - 1))

    return {
        "aoa_start": float(min(aoa) if aoa else 0.0),
        "aoa_end": float(max(aoa) if aoa else 0.0),
        "aoa_step": aoa_step,
        "aoa_count": float(len(aoa)),
        "aoa_step_raw": raw_step,
        "aoa_count_raw": float(len(raw)),
        "cl_min": float(min(cl) if cl else 0.0),
        "cl_max": float(max(cl) if cl else 0.0),
        "cd_min": float(min(cd) if cd else 0.0),
        "cd_max": float(max(cd) if cd else 0.0),
        "cm_min": float(min(cm) if cm else 0.0),
        "cm_max": float(max(cm) if cm else 0.0),
        "ld_min": float(min(ld) if ld else 0.0),
        "ld_max": float(max(ld) if ld else 0.0),
        "sref": float(sref),
        "cref": float(cref),
        "bref": float(bref),
        "reynolds": float(getattr(metrics, "reynolds", 0.0) if metrics else 0.0),
    }


def _extract_solver_effective_conditions(
    *,
    requested_conditions: dict[str, Any],
    vspaero_case_path: Path,
    scripted_re_cref: float | None,
    fallback_mach: float,
) -> dict[str, Any]:
    raw_inputs = _parse_vspaero_case_file(vspaero_case_path)
    requested_re = requested_conditions.get("reynolds")
    requested_re_value = float(requested_re) if isinstance(requested_re, (int, float)) and float(requested_re) > 0 else None

    raw_re_cref = raw_inputs.get("ReCref")
    re_cref = float(raw_re_cref) if isinstance(raw_re_cref, (int, float)) and float(raw_re_cref) > 0 else None
    if re_cref is None and scripted_re_cref is not None and scripted_re_cref > 0:
        re_cref = float(scripted_re_cref)

    raw_mach = raw_inputs.get("Mach")
    mach = float(raw_mach) if isinstance(raw_mach, (int, float)) and math.isfinite(float(raw_mach)) else float(fallback_mach)

    wake_iters_raw = raw_inputs.get("WakeIters")
    wake_iters = int(round(float(wake_iters_raw))) if isinstance(wake_iters_raw, (int, float)) and math.isfinite(float(wake_iters_raw)) else None

    aoa_values = raw_inputs.get("AoA")
    aoa_range = None
    aoa_count = None
    if isinstance(aoa_values, list):
        finite_aoa = [float(value) for value in aoa_values if isinstance(value, (int, float)) and math.isfinite(float(value))]
        if finite_aoa:
            aoa_range = {"start": float(min(finite_aoa)), "end": float(max(finite_aoa))}
            aoa_count = len(finite_aoa)

    tolerance = max(5.0, abs(float(requested_re_value or 0.0)) * 0.01)
    reynolds_applied = bool(
        requested_re_value is not None
        and re_cref is not None
        and abs(float(re_cref) - float(requested_re_value)) <= tolerance
    )

    if requested_re_value is not None:
        if reynolds_applied:
            reynolds_note = f"요청한 Reynolds {requested_re_value:,.0f}를 VSPAERO ReCref에 적용했습니다."
        elif re_cref is not None:
            reynolds_note = (
                f"UI Reynolds {requested_re_value:,.0f}와 달리 VSPAERO가 실제로 사용한 ReCref는 {re_cref:,.0f}입니다."
            )
        else:
            reynolds_note = "UI Reynolds 값은 요청되었지만 VSPAERO 실제 입력 파일에서 ReCref를 확인하지 못했습니다."
    elif re_cref is not None:
        reynolds_note = f"UI Reynolds를 지정하지 않아 VSPAERO 입력의 ReCref {re_cref:,.0f}를 사용했습니다."
    else:
        reynolds_note = "VSPAERO 실제 Reynolds 입력(ReCref)을 확인하지 못했습니다."

    return {
        "source": "vspaero_case_file" if raw_inputs else "script_configuration",
        "requested_reynolds": requested_re_value,
        "re_cref": re_cref,
        "reynolds_applied": reynolds_applied,
        "reynolds_note": reynolds_note,
        "mach": mach,
        "wake_iterations": wake_iters,
        "aoa_range": aoa_range,
        "aoa_count": aoa_count,
    }


def _parse_vspaero_case_file(vspaero_case_path: Path) -> dict[str, Any]:
    if not vspaero_case_path.exists():
        return {}

    try:
        lines = vspaero_case_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}

    parsed: dict[str, Any] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, raw_value = [part.strip() for part in line.split("=", 1)]
        if not key:
            continue

        if "," in raw_value:
            values: list[Any] = []
            for item in raw_value.split(","):
                item = item.strip()
                if not item:
                    continue
                values.append(_coerce_vspaero_value(item))
            parsed[key] = values
        else:
            parsed[key] = _coerce_vspaero_value(raw_value)

    return parsed


def _coerce_vspaero_value(raw: str) -> Any:
    try:
        return float(raw)
    except Exception:
        return raw


def _build_vspaero_all_data_from_rows(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {}

    key_aliases = {
        "AoA": "aoa",
        "CLtot": "cltot",
        "CDtot": "cdtot",
        "CMytot": "cmytot",
        "CDo": "cdo",
        "CDi": "cdi",
        "L/D": "l_d",
        "E": "e",
    }
    normalized_rows: list[dict[str, float]] = []
    for row in rows:
        normalized_rows.append(
            {
                raw_key: float(row[src_key])
                for raw_key, src_key in key_aliases.items()
                if src_key in row and math.isfinite(float(row[src_key]))
            }
        )

    headers = list(key_aliases.keys())
    return _build_vspaero_all_data_from_headers_and_rows(headers, normalized_rows)


def _build_vspaero_all_data_from_headers_and_rows(headers: list[str], rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {}

    ld_key = _pick_ld_key(headers)
    ld_vals = [rows[i].get(ld_key, float("nan")) for i in range(len(rows))]
    finite_idx = [i for i, v in enumerate(ld_vals) if math.isfinite(v)]
    ld_max_idx = finite_idx[0] if finite_idx else 0
    if finite_idx:
        ld_max_idx = max(finite_idx, key=lambda i: ld_vals[i])

    out: dict[str, float] = {}
    for h in headers:
        values = [row.get(h, float("nan")) for row in rows]
        finite = [v for v in values if math.isfinite(v)]
        if not finite:
            continue
        key = _norm_header_key(h)
        out[f"{key}_ld_max"] = float(rows[ld_max_idx].get(h, float("nan")))
        out[f"{key}_max"] = float(max(finite))
        out[f"{key}_min"] = float(min(finite))

    return out


def _parse_polar_rows(polar_path: Path) -> tuple[list[str], list[dict[str, float]]] | None:
    if not polar_path.exists():
        return None

    try:
        lines = polar_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    header_idx = -1
    headers: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if "Beta" in s and "Mach" in s and "AoA" in s:
            cand = re.split(r"\s+", s)
            if len(cand) >= 8:
                headers = cand
                header_idx = i
                break

    if header_idx < 0 or not headers:
        return None

    rows: list[dict[str, float]] = []
    n = len(headers)
    for line in lines[header_idx + 1 :]:
        s = line.strip()
        if not s:
            continue
        parts = re.split(r"\s+", s)
        if len(parts) < n:
            continue
        vals: list[float] = []
        ok = True
        for tok in parts[:n]:
            try:
                vals.append(float(tok))
            except Exception:
                ok = False
                break
        if not ok:
            continue
        row = {headers[j]: vals[j] for j in range(n)}
        rows.append(row)

    return headers, rows


def _pick_ld_key(headers: list[str]) -> str:
    for c in ("L/D", "LoD", "L_D", "LoDw"):
        if c in headers:
            return c
    return headers[0] if headers else "AoA"


def _norm_header_key(name: str) -> str:
    k = name.strip().lower()
    k = k.replace("/", "_")
    k = k.replace("-", "_")
    k = k.replace("(", "")
    k = k.replace(")", "")
    k = k.replace(".", "_")
    k = re.sub(r"[^a-z0-9_]+", "_", k)
    k = re.sub(r"_+", "_", k).strip("_")
    return k or "value"

