from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Literal

from pydantic import BaseModel, Field


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


class AnalysisResult(BaseModel):
    source_label: str
    curve: AeroCurve
    metrics: DerivedMetrics | None = None
    analysis_mode: Literal['openvsp', 'fallback'] = 'fallback'
    fallback_reason: str | None = None
    extra_data: dict[str, Any] = Field(default_factory=dict)
    notes: str = ''
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AnalysisState(BaseModel):
    precision_result: AnalysisResult | None = None
    mode: Literal['precision'] = 'precision'


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
        'RunPrecisionAnalysis',
        'Explain',
        'Undo',
        'Reset',
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


def default_app_state() -> AppState:
    return AppState()


def migrate_legacy_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    analysis_raw = out.get('analysis')
    analysis = dict(analysis_raw) if isinstance(analysis_raw, dict) else {}

    if analysis.get('precision_result') is None and analysis.get('quick_result') is not None:
        analysis['precision_result'] = analysis.get('quick_result')

    precision_raw = analysis.get('precision_result')
    if isinstance(precision_raw, dict):
        precision = dict(precision_raw)
        extra_raw = precision.get('extra_data')
        extra = dict(extra_raw) if isinstance(extra_raw, dict) else {}

        mode = precision.get('analysis_mode')
        if mode not in ('openvsp', 'fallback'):
            mode = _infer_analysis_mode(extra, precision)
        precision['analysis_mode'] = mode

        fallback_reason = precision.get('fallback_reason')
        if not isinstance(fallback_reason, str) or not fallback_reason.strip():
            fallback_reason = _infer_fallback_reason(extra, precision, mode)
        precision['fallback_reason'] = fallback_reason

        source_label = str(precision.get('source_label') or '').strip()
        if not source_label or source_label == '?뺣??댁꽍(OpenVSP+VSPAERO)':
            precision['source_label'] = _default_source_label(mode)

        analysis['precision_result'] = precision

    analysis['mode'] = 'precision'
    analysis.pop('quick_result', None)
    out['analysis'] = analysis
    return out


def _infer_analysis_mode(extra: dict[str, Any], precision: dict[str, Any]) -> Literal['openvsp', 'fallback']:
    solver_mode = str(extra.get('solver_mode') or '').strip().lower()
    notes = str(precision.get('notes') or '').lower()
    source_label = str(precision.get('source_label') or '').lower()

    if solver_mode.startswith('openvsp') or extra.get('vsp3_path'):
        return 'openvsp'
    if 'fallback' in solver_mode or 'fallback' in notes or 'fallback' in source_label:
        return 'fallback'
    if isinstance(extra.get('reason'), str) and extra.get('reason', '').strip():
        return 'fallback'
    return 'openvsp'


def _infer_fallback_reason(
    extra: dict[str, Any],
    precision: dict[str, Any],
    mode: Literal['openvsp', 'fallback'],
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
    return notes or 'OpenVSP solver fallback was used.'


def _default_source_label(mode: Literal['openvsp', 'fallback']) -> str:
    if mode == 'openvsp':
        return '\uc815\ubc00 \ud574\uc11d(OpenVSP/VSPAERO)'
    return '\uadfc\uc0ac \ud574\uc11d(Fallback)'
