from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.analysis.naca import generate_custom_airfoil, generate_naca4
from app.analysis.openvsp_adapter import run_precision_analysis
from app.geometry.wing_builder import build_wing_mesh
from app.models.state import AirfoilState, AppState, CommandEnvelope, WingParams, default_app_state


_COMMAND_PAYLOAD_KEYS: dict[str, set[str]] = {
    'SetAirfoil': {'code', 'custom'},
    'SetWing': {'span_m', 'aspect_ratio', 'sweep_deg', 'taper_ratio', 'dihedral_deg', 'twist_deg'},
    'BuildWingMesh': set(),
    'RunPrecisionAnalysis': set(),
    'Explain': set(),
    'Undo': set(),
    'Reset': set(),
}

_CUSTOM_AIRFOIL_KEYS = {
    'max_camber_percent',
    'max_camber_x_percent',
    'thickness_percent',
    'reflex_percent',
    'camber',
    'camber_pos',
    'thickness',
}

_TXT_AIRFOIL = '\uc5d0\uc5b4\ud3ec\uc77c'
_TXT_THICKNESS = '\ub450\uaed8'
_TXT_CAMBER = '\ucea0\ubc84'
_TXT_CAMBER_POS = '\ucea0\ubc84 \uc704\uce58'
_TXT_WING_SHAPE = '\ub0a0\uac1c \ud615\uc0c1'
_TXT_SPAN = '\uc2a4\ud32c'
_TXT_SWEEP = '\uc2a4\uc717'
_TXT_TAPER = '\ud14c\uc774\ud37c'
_TXT_DIHEDRAL = '\ub514\ud5e4\ub4dc\ub7f4'
_TXT_TWIST = '\ud2b8\uc704\uc2a4\ud2b8'
_TXT_LATEST_SOURCE = '\ucd5c\uc2e0 \ud574\uc11d \ucd9c\ucc98'
_TXT_CORE_PERF = '\ud575\uc2ec \uc131\ub2a5'
_TXT_AOA = '\ubc1b\uc74c\uac01'
_TXT_DEG = '\ub3c4'
_TXT_AOA_SUMMARY = '\ubc1b\uc74c\uac01\ubcc4 \uc694\uc57d'
_TXT_STABILITY = '\uc548\uc815'
_TXT_NEUTRAL = '\uc911\ub9bd'
_TXT_UNSTABLE = '\ubd88\uc548\uc815'
_TXT_STABILITY_METRIC = '\uc548\uc815\uc131 \uc9c0\ud45c'
_TXT_ZERO_LIFT_AOA = '\uc601\uc591\ub825 \ubc1b\uc74c\uac01'
_TXT_ANALYSIS_AOA = '\ud574\uc11d \ubc1b\uc74c\uac01 \uc124\uc815'
_TXT_INTERVAL = '\uac04\uaca9'
_TXT_REYNOLDS = '\ud574\uc11d \ub808\uc774\ub180\uc988\uc218'
_TXT_VSPAERO_SUMMARY = 'VSPAERO \uc694\uc57d'
_TXT_FALLBACK_REASON = '\uadfc\uc0ac \uc0ac\uc720'
_TXT_NO_ANALYSIS = (
    '\uc544\uc9c1 \uacf5\ub825 \ud574\uc11d \uacb0\uacfc\uac00 \uc5c6\uc2b5\ub2c8\ub2e4. '
    '\ucc44\ud305\uc5d0\uc11c \uc815\ubc00 \ud574\uc11d\uc744 \uc694\uccad\ud558\uba74 '
    '\ub370\uc774\ud130\ub97c \ubc14\ud0d5\uc73c\ub85c \uc124\uba85\ud574 \ub4dc\ub9b4 \uc218 \uc788\uc5b4\uc694.'
)

_VSPAERO_LABELS = {
    'aoa_ld_max': 'L/D \ucd5c\ub300 \uc9c0\uc810 \ubc1b\uc74c\uac01',
    'l_d_max': '\ucd5c\ub300 \uc591\ud56d\ube44(L/D)',
    'cltot_ld_max': 'L/D \ucd5c\ub300 \uc9c0\uc810 \ucd1d \uc591\ub825\uacc4\uc218',
    'cltot_max': '\ucd1d \uc591\ub825\uacc4\uc218 \ucd5c\ub300\uac12',
    'cltot_min': '\ucd1d \uc591\ub825\uacc4\uc218 \ucd5c\uc18c\uac12',
    'cdtot_ld_max': 'L/D \ucd5c\ub300 \uc9c0\uc810 \ucd1d \ud56d\ub825\uacc4\uc218',
    'cdtot_min': '\ucd1d \ud56d\ub825\uacc4\uc218 \ucd5c\uc18c\uac12',
    'cdtot_max': '\ucd1d \ud56d\ub825\uacc4\uc218 \ucd5c\ub300\uac12',
    'cmytot_ld_max': 'L/D \ucd5c\ub300 \uc9c0\uc810 \ud53c\uce58 \ubaa8\uba58\ud2b8\uacc4\uc218',
    'cmytot_max': '\ud53c\uce58 \ubaa8\uba58\ud2b8\uacc4\uc218 \ucd5c\ub300\uac12',
    'cmytot_min': '\ud53c\uce58 \ubaa8\uba58\ud2b8\uacc4\uc218 \ucd5c\uc18c\uac12',
    'e_ld_max': 'L/D \ucd5c\ub300 \uc9c0\uc810 \uc624\uc2a4\uc648\ub4dc \ud6a8\uc728',
}


class CommandEngine:
    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def execute(self, state: AppState, command: CommandEnvelope) -> tuple[AppState, str]:
        command = self.validate_command(command)
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
            if not state.airfoil.upper:
                self._set_airfoil(state, {'code': '2412'})
            result = run_precision_analysis(state, self.work_dir, payload)
            state.analysis.precision_result = result
            state.analysis.mode = 'precision'
            return state, 'Precision aerodynamic analysis completed.'

        raise ValueError(f'Unsupported command type: {cmd_type}')

    def _set_airfoil(self, state: AppState, payload: dict[str, Any]) -> None:
        code = str(payload.get('code') or '').strip()
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
            (
                f"{_TXT_AIRFOIL}: {af.code or '-'} "
                f"({_TXT_THICKNESS} {af.thickness_percent:.1f}%, {_TXT_CAMBER} {af.max_camber_percent:.1f}%, "
                f"{_TXT_CAMBER_POS} {af.max_camber_x_percent:.1f}%c)"
            ),
            (
                f"{_TXT_WING_SHAPE}: "
                f"{_TXT_SPAN} {wp.span_m:.2f}m, AR {wp.aspect_ratio:.1f}, {_TXT_SWEEP} {wp.sweep_deg:.1f}{_TXT_DEG}, "
                f"{_TXT_TAPER} {wp.taper_ratio:.2f}, {_TXT_DIHEDRAL} {wp.dihedral_deg:.1f}{_TXT_DEG}, "
                f"{_TXT_TWIST} {wp.twist_deg:.1f}{_TXT_DEG}"
            ),
        ]

        active = state.analysis.precision_result
        if active and active.metrics:
            m = active.metrics
            lines.append(f"{_TXT_LATEST_SOURCE}: {active.source_label}")
            if active.fallback_reason:
                lines.append(f"{_TXT_FALLBACK_REASON}: {active.fallback_reason}")
            lines.append(
                f"{_TXT_CORE_PERF}: "
                f"\ucd5c\ub300 \uc591\ud56d\ube44(L/D) {m.ld_max:.2f} @ {_TXT_AOA} {m.ld_max_aoa:.1f}{_TXT_DEG}, "
                f"\ucd5c\ub300 \uc591\ub825\uacc4\uc218(CLmax) {m.cl_max:.3f} @ {m.cl_max_aoa:.1f}{_TXT_DEG}, "
                f"\ucd5c\uc18c \ud56d\ub825\uacc4\uc218(CDmin) {m.cd_min:.4f} @ {m.cd_min_aoa:.1f}{_TXT_DEG}"
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
                    sample_parts.append(f"{a:.0f}{_TXT_DEG}: CL {cl_v:.3f}, CD {cd_v:.4f}, L/D {ld_v:.2f}")

                if sample_parts:
                    lines.append(f"{_TXT_AOA_SUMMARY}: " + ' | '.join(sample_parts))

            stability = _TXT_STABILITY if m.cm_alpha < 0 else (_TXT_NEUTRAL if abs(m.cm_alpha) < 1e-6 else _TXT_UNSTABLE)
            lines.append(
                f"{_TXT_STABILITY_METRIC}: Cm_alpha {m.cm_alpha:.4f}/rad ({stability}), "
                f"{_TXT_ZERO_LIFT_AOA} {m.alpha_zero_lift:.2f}{_TXT_DEG}, CD0 {m.cd_zero:.4f}, Oswald e {m.oswald_e:.3f}"
            )

            extra = active.extra_data or {}
            pd = extra.get('precision_data')
            if isinstance(pd, dict):
                a0 = pd.get('aoa_start')
                a1 = pd.get('aoa_end')
                st = pd.get('aoa_step')
                if isinstance(a0, (int, float)) and isinstance(a1, (int, float)) and isinstance(st, (int, float)):
                    lines.append(
                        f"{_TXT_ANALYSIS_AOA}: {a0:.1f}{_TXT_DEG} ~ {a1:.1f}{_TXT_DEG}, "
                        f"{_TXT_INTERVAL} {st:.1f}{_TXT_DEG}"
                    )
                re_v = pd.get('reynolds')
                if isinstance(re_v, (int, float)) and re_v > 0:
                    lines.append(f"{_TXT_REYNOLDS}: {float(re_v):,.0f}")

            va = extra.get('vspaero_all_data')
            if isinstance(va, dict):
                ordered_keys = [
                    'aoa_ld_max',
                    'l_d_max',
                    'cltot_ld_max',
                    'cltot_max',
                    'cltot_min',
                    'cdtot_ld_max',
                    'cdtot_min',
                    'cdtot_max',
                    'cmytot_ld_max',
                    'cmytot_max',
                    'cmytot_min',
                    'e_ld_max',
                ]
                vsp_parts = []
                for key in ordered_keys:
                    val = va.get(key)
                    if isinstance(val, (int, float)):
                        label = _VSPAERO_LABELS.get(key, key)
                        digits = 3 if abs(float(val)) >= 1 else 5
                        vsp_parts.append(f"{label} {float(val):.{digits}f}")
                if vsp_parts:
                    lines.append(f"{_TXT_VSPAERO_SUMMARY}: " + ' | '.join(vsp_parts))
        else:
            lines.append(_TXT_NO_ANALYSIS)

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
        return CommandEngine.validate_command(CommandEnvelope(type=ctype, payload=args))

    @staticmethod
    def validate_command(command: CommandEnvelope) -> CommandEnvelope:
        payload = command.payload or {}
        if not isinstance(payload, dict):
            raise ValueError('Command payload must be an object.')

        allowed = _COMMAND_PAYLOAD_KEYS.get(command.type)
        if allowed is None:
            raise ValueError(f'Unsupported command type: {command.type}')

        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f'Unsupported payload keys for {command.type}: {", ".join(unknown)}')

        clean_payload = dict(payload)
        if command.type == 'SetAirfoil' and 'custom' in clean_payload:
            custom = clean_payload.get('custom')
            if not isinstance(custom, dict):
                raise ValueError('SetAirfoil.custom must be an object.')
            custom_unknown = sorted(set(custom) - _CUSTOM_AIRFOIL_KEYS)
            if custom_unknown:
                raise ValueError(f'Unsupported custom airfoil keys: {", ".join(custom_unknown)}')
            clean_payload['custom'] = dict(custom)

        return CommandEnvelope(type=command.type, payload=clean_payload)
