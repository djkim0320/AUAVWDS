from __future__ import annotations

from datetime import datetime, timezone
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

    analysis['mode'] = 'precision'
    analysis.pop('quick_result', None)
    out['analysis'] = analysis
    return out


