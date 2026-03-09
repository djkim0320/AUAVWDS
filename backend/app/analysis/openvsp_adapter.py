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

        case = _build_case_geometry(params.model_dump(), solver_airfoil, aoa_start, aoa_end, aoa_step, mach)
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
                },
            )

        parsed = _parse_vspaero_table(stdout)
        if not parsed["aoa"]:
            return _openvsp_fallback_result(
                inputs=inputs,
                params=params.model_dump(),
                summary=summary.model_dump(),
                run_dir=run_dir,
                reason="Solver는 실행되었지만 stdout에서 공력 테이블 행을 읽어오지 못했습니다.",
                conditions=conditions.model_dump(),
                solver_extra={
                    "stdout_tail": _tail(stdout),
                    "command": cmd,
                    "script_path": str(script_path),
                    "solver_airfoil": case["solver_airfoil"],
                },
            )

        raw_aoa = list(parsed["aoa"])
        parsed = _resample_curve_to_unit_aoa(parsed, aoa_start=aoa_start, aoa_end=aoa_end, aoa_step=aoa_step)

        curve = AeroCurve(
            aoa_deg=[round(x, 6) for x in parsed["aoa"]],
            cl=[round(x, 6) for x in parsed["cl"]],
            cd=[round(max(1e-6, x), 6) for x in parsed["cd"]],
            cm=[round(x, 6) for x in parsed["cm"]],
        )

        re_used = reynolds if (reynolds is not None and reynolds > 0) else _estimate_reynolds(case["cref"], mach)
        metrics = derive_metrics(curve, reynolds=re_used, oswald=_estimate_oswald(params.aspect_ratio, params.sweep_deg, params.taper_ratio))
        precision_data = _build_precision_data(
            curve, metrics, case["sref"], case["cref"], case["bref"], raw_aoa=raw_aoa
        )
        vspaero_all_data = _build_vspaero_all_data(run_dir / "auav_case.polar")
        vsp3 = run_dir / "auav_case.vsp3"

        extra_data: dict[str, Any] = {
            "solver_id": "openvsp",
            "solver_label": "OpenVSP/VSPAERO",
            "solver_mode": "openvsp-script",
            "solver_bin_dir": str(solver["bin_dir"]),
            "vsp_exe": str(solver["vsp_exe"]),
            "vspaero_exe": str(solver["vspaero_exe"]) if solver["vspaero_exe"] else None,
            "script_path": str(script_path),
            "stdout_log": str(run_dir / "solver_stdout.log"),
            "stderr_log": str(run_dir / "solver_stderr.log"),
            "run_dir": str(run_dir),
            "row_count": len(curve.aoa_deg),
            "row_count_raw": len(raw_aoa),
            "Sref": case["sref"],
            "Cref": case["cref"],
            "Bref": case["bref"],
            "result_level": "wing_solver",
            "analysis_conditions": conditions.model_dump(),
            "solver_airfoil": case["solver_airfoil"],
            "precision_data": precision_data,
            "vspaero_all_data": vspaero_all_data,
            "available_artifacts": [
                "run_precision.vspscript",
                "solver_stdout.log",
                "solver_stderr.log",
                "auav_case.polar" if vspaero_all_data else None,
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
            notes=_build_openvsp_notes(case["solver_airfoil"]),
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

    alpha_npts = max(2, int(round((aoa_end - aoa_start) / max(0.25, aoa_step))) + 1)
    airfoil_script = _build_airfoil_script(solver_airfoil)

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
        "solver_airfoil": solver_airfoil,
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


def _build_openvsp_notes(solver_airfoil: dict[str, Any]) -> str:
    requested = str(solver_airfoil.get("requested_label") or "선택한 에어포일")
    geometry_kind = str(solver_airfoil.get("geometry_kind") or "")
    if geometry_kind == "custom_file":
        return f"{requested}의 좌표 파일을 사용해 OpenVSP/VSPAERO 정밀 해석을 완료했습니다."
    degraded_note = solver_airfoil.get("degraded_note")
    if isinstance(degraded_note, str) and degraded_note.strip():
        return degraded_note
    return f"{requested} 형상을 사용해 OpenVSP/VSPAERO 정밀 해석을 완료했습니다."


def _parse_vspaero_table(stdout: str) -> dict[str, list[float]]:
    rows_by_aoa: dict[float, tuple[int, float, float, float]] = {}
    for raw in stdout.splitlines():
        m = _ROW_RE.match(raw)
        if not m:
            continue
        parts = m.groups()
        iter_i = int(parts[0])
        aoa = float(parts[2])
        cl = float(parts[6])
        cd = float(parts[9])
        # VSPAERO table columns:
        # ... E, CMxtot, CMytot, Cmztot, ...
        # For aerodynamic pitching moment we use the Y-axis coefficient (CMytot).
        cm = float(parts[13])
        prev = rows_by_aoa.get(aoa)
        if prev is None or iter_i >= prev[0]:
            rows_by_aoa[aoa] = (iter_i, cl, cd, cm)

    if not rows_by_aoa:
        return {"aoa": [], "cl": [], "cd": [], "cm": []}

    aoa_sorted = sorted(rows_by_aoa.keys())
    cl_vals = [rows_by_aoa[a][1] for a in aoa_sorted]
    cd_vals = [max(1e-6, rows_by_aoa[a][2]) for a in aoa_sorted]
    cm_vals = [rows_by_aoa[a][3] for a in aoa_sorted]

    # Normalize sign convention: target positive CL slope around alpha=0.
    if len(aoa_sorted) >= 3:
        idx = min(range(len(aoa_sorted)), key=lambda i: abs(aoa_sorted[i]))
        i0 = max(0, idx - 1)
        i1 = min(len(aoa_sorted) - 1, idx + 1)
        if i1 > i0:
            slope = (cl_vals[i1] - cl_vals[i0]) / max(1e-9, (aoa_sorted[i1] - aoa_sorted[i0]))
            if slope < 0:
                cl_vals = [-x for x in cl_vals]
                cm_vals = [-x for x in cm_vals]

    return {"aoa": aoa_sorted, "cl": cl_vals, "cd": cd_vals, "cm": cm_vals}


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


def _build_vspaero_all_data(polar_path: Path) -> dict[str, float]:
    parsed = _parse_polar_rows(polar_path)
    if not parsed:
        return {}

    headers, rows = parsed
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

