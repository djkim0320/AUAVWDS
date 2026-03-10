from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Literal

from pydantic import BaseModel, Field


SolverId = Literal['openvsp', 'neuralfoil']
AnalysisMode = Literal['openvsp', 'neuralfoil', 'fallback']
WingtipStyle = Literal['straight', 'pinched']


class AirfoilSummary(BaseModel):
    code: str = ''
    thickness_percent: float = 0.0
    max_camber_percent: float = 0.0
    max_camber_x_percent: float = 0.0


class AirfoilState(BaseModel):
    coords: list[list[float]] = Field(default_factory=list)
    upper: list[list[float]] = Field(default_factory=list)
    lower: list[list[float]] = Field(default_factory=list)
    camber: list[list[float]] = Field(default_factory=list)
    summary: AirfoilSummary = Field(default_factory=AirfoilSummary)


class WingParams(BaseModel):
    span_m: float = 1.0
    aspect_ratio: float = 8.0
    sweep_deg: float = 0.0
    taper_ratio: float = 1.0
    dihedral_deg: float = 5.0
    twist_deg: float = 0.0
    wingtip_style: WingtipStyle = 'straight'


class WingMesh(BaseModel):
    vertices: list[list[float]] = Field(default_factory=list)
    triangles: list[list[int]] = Field(default_factory=list)
    pressure_overlay: list[float] = Field(default_factory=list)


class Planform2D(BaseModel):
    polygon: list[list[float]] = Field(default_factory=list)
    quarter_chord: list[list[float]] = Field(default_factory=list)


class WingState(BaseModel):
    params: WingParams = Field(default_factory=WingParams)
    preview_mesh: WingMesh | None = None
    planform_2d: Planform2D | None = None


class AeroCurve(BaseModel):
    aoa_deg: list[float] = Field(default_factory=list)
    cl: list[float] = Field(default_factory=list)
    cd: list[float] = Field(default_factory=list)
    cm: list[float] = Field(default_factory=list)


class DerivedMetrics(BaseModel):
    ld_max: float = 0.0
    ld_max_aoa: float = 0.0
    cl_max: float = 0.0
    cl_max_aoa: float = 0.0
    cd_min: float = 0.0
    cd_min_aoa: float = 0.0
    cl_alpha: float = 0.0
    alpha_zero_lift: float = 0.0
    cm_zero_lift: float = 0.0
    cm_alpha: float = 0.0
    cd_zero: float = 0.0
    oswald_e: float = 0.0
    endurance_param: float = 0.0
    range_param: float = 0.0
    reynolds: float = 0.0


class AnalysisConditions(BaseModel):
    aoa_start: float = -10.0
    aoa_end: float = 20.0
    aoa_step: float = 1.0
    mach: float = 0.08
    reynolds: float | None = None


class AnalysisResult(BaseModel):
    source_label: str
    curve: AeroCurve
    metrics: DerivedMetrics | None = None
    analysis_mode: AnalysisMode = 'fallback'
    fallback_reason: str | None = None
    extra_data: dict[str, Any] = Field(default_factory=dict)
    notes: str = ''
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SolverResults(BaseModel):
    openvsp: AnalysisResult | None = None
    neuralfoil: AnalysisResult | None = None


class AnalysisState(BaseModel):
    results: SolverResults = Field(default_factory=SolverResults)
    active_solver: SolverId = 'openvsp'
    conditions: AnalysisConditions = Field(default_factory=AnalysisConditions)


class AppState(BaseModel):
    airfoil: AirfoilState = Field(default_factory=AirfoilState)
    wing: WingState = Field(default_factory=WingState)
    analysis: AnalysisState = Field(default_factory=AnalysisState)
    history: list[dict[str, Any]] = Field(default_factory=list)


class CommandEnvelope(BaseModel):
    type: Literal[
        'SetAirfoil',
        'SetWing',
        'BuildWingMesh',
        'SetAnalysisConditions',
        'SetActiveSolver',
        'RunOpenVspAnalysis',
        'RunNeuralFoilAnalysis',
        'RunPrecisionAnalysis',
        'Explain',
        'Undo',
        'Reset',
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


def default_app_state() -> AppState:
    return AppState()


def get_solver_result(analysis: AnalysisState, solver_id: SolverId) -> AnalysisResult | None:
    return getattr(analysis.results, solver_id)


def get_active_result(analysis: AnalysisState) -> tuple[SolverId | None, AnalysisResult | None]:
    active = get_solver_result(analysis, analysis.active_solver)
    if active is not None:
        return analysis.active_solver, active

    if analysis.results.openvsp is not None:
        return 'openvsp', analysis.results.openvsp
    if analysis.results.neuralfoil is not None:
        return 'neuralfoil', analysis.results.neuralfoil
    return None, None


def set_solver_result(analysis: AnalysisState, solver_id: SolverId, result: AnalysisResult) -> None:
    setattr(analysis.results, solver_id, result)
    analysis.active_solver = solver_id


def clear_solver_results(analysis: AnalysisState) -> None:
    analysis.results = SolverResults()


def source_label_for(solver_id: SolverId, analysis_mode: AnalysisMode) -> str:
    if solver_id == 'openvsp':
        if analysis_mode == 'fallback':
            return '\uc815\ubc00 \ud574\uc11d(OpenVSP/VSPAERO, \ub300\uccb4 \uacbd\ub85c)'
        return '\uc815\ubc00 \ud574\uc11d(OpenVSP/VSPAERO)'

    if analysis_mode == 'fallback':
        return '\ube60\ub978 \ud574\uc11d(NeuralFoil \uae30\ubc18 \ub0a0\uac1c \ucd94\uc815, \ub300\uccb4 \uacbd\ub85c)'
    return '\ube60\ub978 \ud574\uc11d(NeuralFoil \uae30\ubc18 \ub0a0\uac1c \ucd94\uc815)'


def migrate_legacy_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    analysis_raw = out.get('analysis')
    analysis = dict(analysis_raw) if isinstance(analysis_raw, dict) else {}

    precision_raw = analysis.get('precision_result')
    if precision_raw is None and analysis.get('quick_result') is not None:
        precision_raw = analysis.get('quick_result')

    results_raw = analysis.get('results')
    results = dict(results_raw) if isinstance(results_raw, dict) else {}

    if isinstance(precision_raw, dict) and 'openvsp' not in results:
        results['openvsp'] = _normalize_result_record(precision_raw, preferred_solver='openvsp')

    neuralfoil_raw = results.get('neuralfoil')
    if isinstance(neuralfoil_raw, dict):
        results['neuralfoil'] = _normalize_result_record(neuralfoil_raw, preferred_solver='neuralfoil')

    openvsp_raw = results.get('openvsp')
    if isinstance(openvsp_raw, dict):
        results['openvsp'] = _normalize_result_record(openvsp_raw, preferred_solver='openvsp')

    analysis['results'] = results
    conditions_raw = analysis.get('conditions')
    analysis['conditions'] = (
        dict(conditions_raw) if isinstance(conditions_raw, dict) else AnalysisConditions().model_dump()
    )

    active_solver = analysis.get('active_solver')
    if active_solver not in ('openvsp', 'neuralfoil'):
        if isinstance(results.get('openvsp'), dict):
            active_solver = 'openvsp'
        elif isinstance(results.get('neuralfoil'), dict):
            active_solver = 'neuralfoil'
        else:
            active_solver = 'openvsp'
    analysis['active_solver'] = active_solver

    analysis.pop('precision_result', None)
    analysis.pop('quick_result', None)
    analysis.pop('mode', None)
    out['analysis'] = analysis
    return out


def _normalize_result_record(record: dict[str, Any], preferred_solver: SolverId) -> dict[str, Any]:
    precision = dict(record)
    extra_raw = precision.get('extra_data')
    extra = dict(extra_raw) if isinstance(extra_raw, dict) else {}

    solver_id = str(extra.get('solver_id') or preferred_solver).strip().lower()
    if solver_id not in ('openvsp', 'neuralfoil'):
        solver_id = preferred_solver
    extra['solver_id'] = solver_id

    mode = precision.get('analysis_mode')
    if mode not in ('openvsp', 'neuralfoil', 'fallback'):
        mode = _infer_analysis_mode(extra, precision, solver_id)
    precision['analysis_mode'] = mode

    fallback_reason = precision.get('fallback_reason')
    if not isinstance(fallback_reason, str) or not fallback_reason.strip():
        fallback_reason = _infer_fallback_reason(extra, precision, mode)
    precision['fallback_reason'] = fallback_reason

    source_label = str(precision.get('source_label') or '').strip()
    if not source_label or source_label in ('?類???곴퐤(OpenVSP+VSPAERO)', '?뺣? ?댁꽍(OpenVSP/VSPAERO)', '洹쇱궗 ?댁꽍(Fallback)'):
        precision['source_label'] = source_label_for(solver_id, mode)

    precision['extra_data'] = extra
    return precision


def _infer_analysis_mode(
    extra: dict[str, Any],
    precision: dict[str, Any],
    solver_id: SolverId,
) -> AnalysisMode:
    solver_mode = str(extra.get('solver_mode') or '').strip().lower()
    notes = str(precision.get('notes') or '').lower()
    source_label = str(precision.get('source_label') or '').lower()

    if 'fallback' in solver_mode or 'fallback' in notes or 'fallback' in source_label:
        return 'fallback'
    if isinstance(extra.get('reason'), str) and extra.get('reason', '').strip():
        return 'fallback'
    return 'neuralfoil' if solver_id == 'neuralfoil' else 'openvsp'


def _infer_fallback_reason(
    extra: dict[str, Any],
    precision: dict[str, Any],
    mode: AnalysisMode,
) -> str | None:
    if mode != 'fallback':
        return None

    reason = extra.get('reason')
    if isinstance(reason, str) and reason.strip():
        return reason.strip()

    notes = str(precision.get('notes') or '').strip()
    match = re.search(r'fallback used:\s*(.+)$', notes, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return notes or 'Solver fallback was used.'
