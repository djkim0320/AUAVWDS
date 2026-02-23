from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.analysis.naca import generate_custom_airfoil, generate_naca4
from app.analysis.openvsp_adapter import run_precision_analysis
from app.geometry.wing_builder import build_wing_mesh
from app.models.state import AirfoilState, AppState, CommandEnvelope, WingParams, default_app_state


class CommandEngine:
    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def execute(self, state: AppState, command: CommandEnvelope) -> tuple[AppState, str]:
        cmd_type = command.type
        payload = command.payload or {}

        if cmd_type == 'Reset':
            return default_app_state(), 'State reset complete.'

        if cmd_type == 'Undo':
            if not state.history:
                return state, 'No history snapshot available for undo.'
            prev = state.history.pop()
            restored = AppState.model_validate(prev)
            restored.history = state.history
            return restored, 'Reverted to previous snapshot.'

        if cmd_type == 'Explain':
            return state, self._explain_state(state)

        state.history.append(copy.deepcopy(state.model_dump(exclude={'history'})))
        state.history = state.history[-30:]

        if cmd_type == 'SetAirfoil':
            self._set_airfoil(state, payload)
            return state, 'Airfoil updated.'

        if cmd_type == 'SetWing':
            self._set_wing(state, payload)
            return state, 'Wing parameters updated.'

        if cmd_type == 'BuildWingMesh':
            if not state.airfoil.upper:
                self._set_airfoil(state, {'code': '2412'})
            mesh, planform = build_wing_mesh(state.airfoil, state.wing.params)
            state.wing.preview_mesh = mesh
            state.wing.planform_2d = planform
            return state, '3D wing mesh generated.'

        if cmd_type == 'RunPrecisionAnalysis':
            result = run_precision_analysis(state, self.work_dir, payload)
            state.analysis.precision_result = result
            state.analysis.mode = 'precision'
            return state, 'Precision aerodynamic analysis completed.'

        raise ValueError(f'Unsupported command type: {cmd_type}')

    def _set_airfoil(self, state: AppState, payload: dict[str, Any]) -> None:
        code = str(payload.get('code') or payload.get('name') or '').strip()
        custom = payload.get('custom') if isinstance(payload.get('custom'), dict) else None

        if custom:
            out = generate_custom_airfoil(
                max_camber_percent=float(custom.get('max_camber_percent', custom.get('camber', 2.0))),
                max_camber_x_percent=float(custom.get('max_camber_x_percent', custom.get('camber_pos', 40.0))),
                thickness_percent=float(custom.get('thickness_percent', custom.get('thickness', 12.0))),
                reflex_percent=float(custom.get('reflex_percent', 0.0)),
            )
        else:
            if not code:
                code = state.airfoil.summary.code or '2412'
            out = generate_naca4(code)

        state.airfoil = AirfoilState.model_validate(out)

    def _set_wing(self, state: AppState, payload: dict[str, Any]) -> None:
        p = state.wing.params.model_dump()
        for key in ('span_m', 'aspect_ratio', 'sweep_deg', 'taper_ratio', 'dihedral_deg', 'twist_deg'):
            if key in payload and payload[key] is not None:
                p[key] = float(payload[key])

        p['span_m'] = max(0.15, min(20.0, p['span_m']))
        p['aspect_ratio'] = max(2.0, min(30.0, p['aspect_ratio']))
        p['sweep_deg'] = max(-35.0, min(45.0, p['sweep_deg']))
        p['taper_ratio'] = max(0.1, min(1.2, p['taper_ratio']))
        p['dihedral_deg'] = max(-10.0, min(20.0, p['dihedral_deg']))
        p['twist_deg'] = max(-10.0, min(10.0, p['twist_deg']))

        state.wing.params = WingParams.model_validate(p)

    def _explain_state(self, state: AppState) -> str:
        af = state.airfoil.summary
        wp = state.wing.params
        lines = [
            f"에어포일: {af.code or '-'} (두께 {af.thickness_percent:.1f}%, 캠버 {af.max_camber_percent:.1f}%, 캠버 위치 {af.max_camber_x_percent:.1f}%c)",
            (
                "날개 형상: "
                f"스팬 {wp.span_m:.2f}m, AR {wp.aspect_ratio:.1f}, 스윕 {wp.sweep_deg:.1f}도, "
                f"테이퍼 {wp.taper_ratio:.2f}, 상반각 {wp.dihedral_deg:.1f}도, 트위스트 {wp.twist_deg:.1f}도"
            ),
        ]

        active = state.analysis.precision_result
        if active and active.metrics:
            m = active.metrics
            lines.append(f"최근 해석 출처: {active.source_label}")
            lines.append(
                f"핵심 성능: 최대 양항비(L/D) {m.ld_max:.2f} @ 받음각 {m.ld_max_aoa:.1f}도, "
                f"최대 양력계수(CLmax) {m.cl_max:.3f} @ {m.cl_max_aoa:.1f}도, "
                f"최소 항력계수(CDmin) {m.cd_min:.4f} @ {m.cd_min_aoa:.1f}도"
            )

            curve = active.curve
            if curve.aoa_deg and curve.cl and curve.cd and curve.cm:
                def near_val(xs: list[float], ys: list[float], target: float) -> float:
                    idx = min(range(len(xs)), key=lambda i: abs(xs[i] - target))
                    return float(ys[idx])

                samples = [-10.0, -5.0, 0.0, 5.0, 10.0, 15.0, 20.0]
                sample_parts = []
                for a in samples:
                    if a < min(curve.aoa_deg) or a > max(curve.aoa_deg):
                        continue
                    cl_v = near_val(curve.aoa_deg, curve.cl, a)
                    cd_v = near_val(curve.aoa_deg, curve.cd, a)
                    ld_v = (cl_v / cd_v) if abs(cd_v) > 1e-9 else 0.0
                    sample_parts.append(f"{a:.0f}도: CL {cl_v:.3f}, CD {cd_v:.4f}, L/D {ld_v:.2f}")

                if sample_parts:
                    lines.append("받음각별 요약: " + " | ".join(sample_parts))

            stability = "안정" if m.cm_alpha < 0 else ("중립" if abs(m.cm_alpha) < 1e-6 else "불안정")
            lines.append(
                f"안정성/효율: Cm_alpha {m.cm_alpha:.4f}/rad ({stability}), "
                f"영양력 받음각 {m.alpha_zero_lift:.2f}도, CD0 {m.cd_zero:.4f}, Oswald e {m.oswald_e:.3f}"
            )

            extra = active.extra_data or {}
            pd = extra.get("precision_data")
            if isinstance(pd, dict):
                a0 = pd.get("aoa_start")
                a1 = pd.get("aoa_end")
                st = pd.get("aoa_step")
                if isinstance(a0, (int, float)) and isinstance(a1, (int, float)) and isinstance(st, (int, float)):
                    lines.append(f"해석 스윕 설정: 받음각 {a0:.1f}도 ~ {a1:.1f}도, 간격 {st:.1f}도")
                re_v = pd.get("reynolds")
                if isinstance(re_v, (int, float)) and re_v > 0:
                    lines.append(f"해석 레이놀즈수: {float(re_v):,.0f}")

            va = extra.get("vspaero_all_data")
            if isinstance(va, dict):
                label_map = {
                    "aoa_ld_max": "L/D 최대 지점 받음각",
                    "l_d_max": "최대 양항비(L/D)",
                    "cltot_ld_max": "L/D 최대 지점 총 양력계수",
                    "cltot_max": "총 양력계수 최대값",
                    "cltot_min": "총 양력계수 최소값",
                    "cdtot_ld_max": "L/D 최대 지점 총 항력계수",
                    "cdtot_min": "총 항력계수 최소값",
                    "cdtot_max": "총 항력계수 최대값",
                    "cmytot_ld_max": "L/D 최대 지점 피치 모멘트계수",
                    "cmytot_max": "피치 모멘트계수 최대값",
                    "cmytot_min": "피치 모멘트계수 최소값",
                    "e_ld_max": "L/D 최대 지점 오스왈드 효율",
                }
                ordered_keys = [
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
                ]
                vsp_parts = []
                for key in ordered_keys:
                    val = va.get(key)
                    if isinstance(val, (int, float)):
                        label = label_map.get(key, key)
                        digits = 3 if abs(float(val)) >= 1 else 5
                        vsp_parts.append(f"{label} {float(val):.{digits}f}")
                if vsp_parts:
                    lines.append("VSPAERO 요약: " + " | ".join(vsp_parts))
        else:
            lines.append("아직 공력 해석 결과가 없습니다. 채팅에서 정밀 해석을 요청하면 데이터 기반 해설이 가능해요.")

        return '\n'.join(lines)

    @staticmethod
    def command_from_tool(name: str, args: dict[str, Any] | None) -> CommandEnvelope:
        args = args or {}
        alias = {
            'SetAirfoil': 'SetAirfoil',
            'SetWing': 'SetWing',
            'BuildWingMesh': 'BuildWingMesh',
            'RunPrecisionAnalysis': 'RunPrecisionAnalysis',
            'Explain': 'Explain',
            'Undo': 'Undo',
            'Reset': 'Reset',
        }
        ctype = alias.get(name)
        if not ctype:
            raise ValueError(f'Unknown tool/command: {name}')
        return CommandEnvelope(type=ctype, payload=args)
