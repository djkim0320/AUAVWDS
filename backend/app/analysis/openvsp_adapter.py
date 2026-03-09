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
from app.models.state import AeroCurve, AnalysisResult, AppState


_NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
_ROW_RE = re.compile(
    rf"^\s*(\d+)\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+"
    rf"({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+"
    rf"({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})"
)


def run_precision_analysis(state: AppState, work_dir: str | Path, payload: dict[str, Any] | None = None) -> AnalysisResult:
    _ = payload or {}

    # Keep solver sweep aligned with frontend aerodynamic chart range.
    aoa_start = -10.0
    aoa_end = 20.0
    aoa_step = 1.0
    mach = 0.08
    speed_mps = max(0.1, mach * 340.3)
    reynolds: float | None = None

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
    run_dir = base_work / "precision_runs" / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    solver = _resolve_solver_paths()
    if solver["vsp_exe"] is None:
        return _surrogate_precision_result(
            inputs=inputs,
            params=params.model_dump(),
            summary=summary.model_dump(),
            run_dir=run_dir,
            reason="OpenVSP solver binary not found. Expected vsp.exe in third_party/openvsp/win64 or AUAV_SOLVER_BIN_DIR.",
        )

    try:
        case = _build_case_geometry(params.model_dump(), aoa_start, aoa_end, aoa_step, mach)
        script_path = run_dir / "run_precision.vspscript"
        script_path.write_text(case["script"], encoding="utf-8")

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
            return _surrogate_precision_result(
                inputs=inputs,
                params=params.model_dump(),
                summary=summary.model_dump(),
                run_dir=run_dir,
                reason=f"OpenVSP solver returned non-zero exit code: {proc.returncode}",
                solver_extra={"stdout_tail": _tail(stdout), "stderr_tail": _tail(stderr), "command": cmd},
            )

        parsed = _parse_vspaero_table(stdout)
        if not parsed["aoa"]:
            return _surrogate_precision_result(
                inputs=inputs,
                params=params.model_dump(),
                summary=summary.model_dump(),
                run_dir=run_dir,
                reason="Solver ran but no aerodynamic table rows were parsed from stdout.",
                solver_extra={"stdout_tail": _tail(stdout), "command": cmd},
            )

        raw_aoa = list(parsed["aoa"])
        parsed = _resample_curve_to_unit_aoa(parsed, aoa_start=aoa_start, aoa_end=aoa_end, aoa_step=1.0)

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

        extra_data: dict[str, Any] = {
            "solver_mode": "openvsp-script",
            "solver_bin_dir": str(solver["bin_dir"]),
            "vsp_exe": str(solver["vsp_exe"]),
            "vspaero_exe": str(solver["vspaero_exe"]) if solver["vspaero_exe"] else None,
            "script_path": str(script_path),
            "stdout_log": str(run_dir / "solver_stdout.log"),
            "stderr_log": str(run_dir / "solver_stderr.log"),
            "row_count": len(curve.aoa_deg),
            "row_count_raw": len(raw_aoa),
            "Sref": case["sref"],
            "Cref": case["cref"],
            "Bref": case["bref"],
            "precision_data": precision_data,
            "vspaero_all_data": vspaero_all_data,
        }

        vsp3 = run_dir / "auav_case.vsp3"
        if vsp3.exists():
            extra_data["vsp3_path"] = str(vsp3)

        return AnalysisResult(
            source_label="정밀해석(OpenVSP+VSPAERO)",
            curve=curve,
            metrics=metrics,
            extra_data=extra_data,
            notes="OpenVSP script-based precision analysis completed.",
        )
    except subprocess.TimeoutExpired:
        return _surrogate_precision_result(
            inputs=inputs,
            params=params.model_dump(),
            summary=summary.model_dump(),
            run_dir=run_dir,
            reason="OpenVSP solver timed out.",
        )
    except Exception as exc:
        return _surrogate_precision_result(
            inputs=inputs,
            params=params.model_dump(),
            summary=summary.model_dump(),
            run_dir=run_dir,
            reason=f"OpenVSP solver execution failed: {exc}",
        )


def _surrogate_precision_result(
    *,
    inputs: AeroInputs,
    params: dict[str, Any],
    summary: dict[str, Any],
    run_dir: Path,
    reason: str,
    solver_extra: dict[str, Any] | None = None,
) -> AnalysisResult:
    curve, metrics = build_surrogate_curve(inputs, precision_mode=True)
    sref = max(1e-5, inputs.span_m * inputs.span_m / max(1.0, inputs.aspect_ratio))
    cref = max(0.02, inputs.span_m / max(1.2, inputs.aspect_ratio))
    bref = max(0.02, inputs.span_m)
    precision_data = _build_precision_data(curve, metrics, sref, cref, bref)

    extra_data: dict[str, Any] = {
        "solver_mode": "surrogate-fallback",
        "reason": reason,
        "params": params,
        "airfoil_summary": summary,
        "run_dir": str(run_dir),
        "precision_data": precision_data,
    }
    if solver_extra:
        extra_data.update(solver_extra)

    return AnalysisResult(
        source_label="정밀해석(OpenVSP+VSPAERO)",
        curve=curve,
        metrics=metrics,
        extra_data=extra_data,
        notes=f"Precision solver fallback used: {reason}",
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


def _build_case_geometry(params: dict[str, Any], aoa_start: float, aoa_end: float, aoa_step: float, mach: float) -> dict[str, Any]:
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

    script = f"""void main()
{{
    string wid = AddGeom( "WING", "" );
    SetParmVal( wid, "Span", "XSec_1", {semi:.6f} );
    SetParmVal( wid, "Root_Chord", "XSec_1", {c_root:.6f} );
    SetParmVal( wid, "Tip_Chord", "XSec_1", {c_tip:.6f} );
    SetParmVal( wid, "Sweep", "XSec_1", {sweep:.6f} );
    SetParmVal( wid, "Dihedral", "XSec_1", {dihedral:.6f} );
    SetParmVal( wid, "Twist", "XSec_1", {twist:.6f} );
    SetParmVal( wid, "Tess_W", "Shape", 45 );
    SetParmVal( wid, "SectTess_U", "XSec_1", 20 );
    Update();

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

    return {"script": script, "sref": area, "cref": mac, "bref": span}


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

