from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

from app.analysis.naca import generate_custom_airfoil, generate_naca4
from app.analysis.neuralfoil_adapter import run_neuralfoil_analysis
from app.analysis.openvsp_adapter import run_precision_analysis
from app.geometry.wing_builder import build_wing_mesh
from app.models.state import (
    AnalysisConditions,
    AirfoilState,
    AppState,
    CommandEnvelope,
    Planform2D,
    WingMesh,
    WingParams,
    clear_solver_results,
    default_app_state,
    get_active_result,
    set_solver_result,
)
from app.services.command_specs import CUSTOM_AIRFOIL_KEYS, allowed_payload_keys, normalize_command_name


_TXT_AIRFOIL = "\uc5d0\uc5b4\ud3ec\uc77c"
_TXT_THICKNESS = "\ub450\uaed8"
_TXT_CAMBER = "\ucea0\ubc84"
_TXT_CAMBER_POS = "\ucea0\ubc84 \uc704\uce58"
_TXT_WING_SHAPE = "\ub0a0\uac1c \ud615\uc0c1"
_TXT_SPAN = "\uc2a4\ud32c"
_TXT_SWEEP = "\uc2a4\uc717"
_TXT_TAPER = "\ud14c\uc774\ud37c"
_TXT_DIHEDRAL = "\ub514\ud5e4\ub4dc\ub7f4"
_TXT_TWIST = "\ud2b8\uc704\uc2a4\ud2b8"
_TXT_WINGTIP = "\uc719\ud301"
_TXT_LATEST_SOURCE = "\ucd5c\uc2e0 \ud574\uc11d \ucd9c\ucc98"
_TXT_CORE_PERF = "\ud575\uc2ec \uc131\ub2a5"
_TXT_AOA = "\ubc1b\uc74c\uac01"
_TXT_DEG = "\ub3c4"
_TXT_AOA_SUMMARY = "\ubc1b\uc74c\uac01\ubcc4 \uc694\uc57d"
_TXT_STABILITY = "\uc548\uc815"
_TXT_NEUTRAL = "\uc911\ub9bd"
_TXT_UNSTABLE = "\ubd88\uc548\uc815"
_TXT_STABILITY_METRIC = "\uc548\uc815\uc131 \uc9c0\ud45c"
_TXT_ZERO_LIFT_AOA = "\uc601\uc591\ub825 \ubc1b\uc74c\uac01"
_TXT_ANALYSIS_AOA = "\ud574\uc11d \ubc1b\uc74c\uac01 \uc124\uc815"
_TXT_INTERVAL = "\uac04\uaca9"
_TXT_REYNOLDS = "\ud574\uc11d \ub808\uc774\ub180\uc988\uc218"
_TXT_VSPAERO_SUMMARY = "VSPAERO \uc694\uc57d"
_TXT_FALLBACK_REASON = "\uadfc\uc0ac \uc0ac\uc720"
_TXT_NO_ANALYSIS = (
    "\uc544\uc9c1 \uacf5\ub825 \ud574\uc11d \uacb0\uacfc\uac00 \uc5c6\uc2b5\ub2c8\ub2e4. "
    "\ucc44\ud305\uc5d0\uc11c \uc815\ubc00 \ud574\uc11d\uc744 \uc694\uccad\ud558\uba74 "
    "\ub370\uc774\ud130\ub97c \ubc14\ud0d5\uc73c\ub85c \uc124\uba85\ud574 \ub4dc\ub9b4 \uc218 \uc788\uc5b4\uc694."
)

_VSPAERO_LABELS = {
    "aoa_ld_max": "L/D \ucd5c\ub300 \uc9c0\uc810 \ubc1b\uc74c\uac01",
    "l_d_max": "\ucd5c\ub300 \uc591\ud56d\ube44(L/D)",
    "cltot_ld_max": "L/D \ucd5c\ub300 \uc9c0\uc810 \ucd1d \uc591\ub825\uacc4\uc218",
    "cltot_max": "\ucd1d \uc591\ub825\uacc4\uc218 \ucd5c\ub300\uac12",
    "cltot_min": "\ucd1d \uc591\ub825\uacc4\uc218 \ucd5c\uc18c\uac12",
    "cdtot_ld_max": "L/D \ucd5c\ub300 \uc9c0\uc810 \ucd1d \ud56d\ub825\uacc4\uc218",
    "cdtot_min": "\ucd1d \ud56d\ub825\uacc4\uc218 \ucd5c\uc18c\uac12",
    "cdtot_max": "\ucd1d \ud56d\ub825\uacc4\uc218 \ucd5c\ub300\uac12",
    "cmytot_ld_max": "L/D \ucd5c\ub300 \uc9c0\uc810 \ud53c\uce58 \ubaa8\uba58\ud2b8\uacc4\uc218",
    "cmytot_max": "\ud53c\uce58 \ubaa8\uba58\ud2b8\uacc4\uc218 \ucd5c\ub300\uac12",
    "cmytot_min": "\ud53c\uce58 \ubaa8\uba58\ud2b8\uacc4\uc218 \ucd5c\uc18c\uac12",
    "e_ld_max": "L/D \ucd5c\ub300 \uc9c0\uc810 \uc624\uc2a4\uc648\ub4dc \ud6a8\uc728",
}


def _wingtip_style_label(value: str) -> str:
    return "\uc870\uc784\ud615" if value == "pinched" else "\uc9c1\uc120\ud615"


class CommandEngine:
    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._mesh_cache: OrderedDict[str, tuple[dict[str, Any], dict[str, Any]]] = OrderedDict()

    @staticmethod
    def normalize_command_alias(command: CommandEnvelope) -> CommandEnvelope:
        normalized_type = normalize_command_name(command.type)
        if normalized_type == command.type:
            return command
        return CommandEnvelope(type=normalized_type, payload=dict(command.payload or {}))

    def execute(self, state: AppState, command: CommandEnvelope) -> tuple[AppState, str]:
        prepared = self.prepare_command(command)
        return self.execute_prepared(state, prepared)

    def execute_prepared(self, state: AppState, command: CommandEnvelope) -> tuple[AppState, str]:
        cmd_type = command.type
        payload = command.payload or {}

        if cmd_type == "Reset":
            return default_app_state(), "State reset complete."

        if cmd_type == "Undo":
            if not state.history:
                return state, "No history snapshot available for undo."
            prev = state.history.pop()
            restored = AppState.model_validate(prev)
            restored.history = state.history
            return restored, "Reverted to previous snapshot."

        if cmd_type == "Explain":
            return state, self._explain_state(state)

        if cmd_type == "SetAirfoil":
            history_snapshot = self._snapshot_without_history(state)
            before_airfoil = state.airfoil.model_dump()
            self._set_airfoil(state, payload)
            mutated = state.airfoil.model_dump() != before_airfoil
            if mutated:
                self._push_history(state, history_snapshot)
                self._invalidate_geometry_outputs(state)
                clear_solver_results(state.analysis)
            return state, "\uc5d0\uc5b4\ud3ec\uc77c\uc744 \uc5c5\ub370\uc774\ud2b8\ud588\uc2b5\ub2c8\ub2e4."

        if cmd_type == "SetWing":
            history_snapshot = self._snapshot_without_history(state)
            before_params = state.wing.params.model_dump()
            self._set_wing(state, payload)
            mutated = state.wing.params.model_dump() != before_params
            if mutated:
                self._push_history(state, history_snapshot)
                self._invalidate_geometry_outputs(state)
                clear_solver_results(state.analysis)
            return state, "\ub0a0\uac1c \ud615\uc0c1 \ud30c\ub77c\ubbf8\ud130\ub97c \uc5c5\ub370\uc774\ud2b8\ud588\uc2b5\ub2c8\ub2e4."

        if cmd_type == "BuildWingMesh":
            history_snapshot = self._snapshot_without_history(state)
            before_airfoil = state.airfoil.model_dump()
            before_mesh = state.wing.preview_mesh.model_dump() if state.wing.preview_mesh else None
            before_planform = state.wing.planform_2d.model_dump() if state.wing.planform_2d else None
            if not state.airfoil.upper:
                self._set_airfoil(state, {"code": "2412"})
            mesh, planform = self._get_or_build_mesh(state)
            state.wing.preview_mesh = mesh
            state.wing.planform_2d = planform
            mutated = (
                state.airfoil.model_dump() != before_airfoil
                or mesh.model_dump() != before_mesh
                or planform.model_dump() != before_planform
            )
            if mutated:
                self._push_history(state, history_snapshot)
            return state, "\ub0a0\uac1c 3D \uba54\uc2dc\ub97c \uc0dd\uc131\ud588\uc2b5\ub2c8\ub2e4."

        if cmd_type == "SetAnalysisConditions":
            history_snapshot = self._snapshot_without_history(state)
            before_conditions = state.analysis.conditions.model_dump()
            self._set_analysis_conditions(state, payload)
            mutated = state.analysis.conditions.model_dump() != before_conditions
            if mutated:
                self._push_history(state, history_snapshot)
                clear_solver_results(state.analysis)
            return state, "\ud574\uc11d \uc870\uac74\uc744 \uc5c5\ub370\uc774\ud2b8\ud588\uc2b5\ub2c8\ub2e4."

        if cmd_type == "SetActiveSolver":
            history_snapshot = self._snapshot_without_history(state)
            before_solver = state.analysis.active_solver
            self._set_active_solver(state, payload)
            if state.analysis.active_solver != before_solver:
                self._push_history(state, history_snapshot)
            return state, "\ud65c\uc131 solver\ub97c \ubcc0\uacbd\ud588\uc2b5\ub2c8\ub2e4."

        if cmd_type == "RunOpenVspAnalysis":
            history_snapshot = self._snapshot_without_history(state)
            if not state.airfoil.upper:
                self._set_airfoil(state, {"code": "2412"})
            result = run_precision_analysis(state, self.work_dir, payload)
            self._push_history(state, history_snapshot)
            set_solver_result(state.analysis, "openvsp", result)
            return state, "OpenVSP/VSPAERO \ud574\uc11d\uc744 \uc644\ub8cc\ud588\uc2b5\ub2c8\ub2e4."

        if cmd_type == "RunNeuralFoilAnalysis":
            history_snapshot = self._snapshot_without_history(state)
            if not state.airfoil.upper:
                self._set_airfoil(state, {"code": "2412"})
            result = run_neuralfoil_analysis(state, self.work_dir, payload)
            self._push_history(state, history_snapshot)
            set_solver_result(state.analysis, "neuralfoil", result)
            return state, "NeuralFoil \uae30\ubc18 \ub0a0\uac1c \ucd94\uc815 \ud574\uc11d\uc744 \uc644\ub8cc\ud588\uc2b5\ub2c8\ub2e4."

        raise ValueError(f"\uc9c0\uc6d0\ud558\uc9c0 \uc54a\ub294 \uba85\ub839 \ud0c0\uc785\uc785\ub2c8\ub2e4: {cmd_type}")

    def _set_airfoil(self, state: AppState, payload: dict[str, Any]) -> None:
        code = str(payload.get("code") or "").strip()
        custom = payload.get("custom") if isinstance(payload.get("custom"), dict) else None

        if custom:
            out = generate_custom_airfoil(
                max_camber_percent=float(custom.get("max_camber_percent", custom.get("camber", 2.0))),
                max_camber_x_percent=float(custom.get("max_camber_x_percent", custom.get("camber_pos", 40.0))),
                thickness_percent=float(custom.get("thickness_percent", custom.get("thickness", 12.0))),
                reflex_percent=float(custom.get("reflex_percent", 0.0)),
            )
        else:
            if not code:
                code = state.airfoil.summary.code or "2412"
            out = generate_naca4(code)

        state.airfoil = AirfoilState.model_validate(out)

    def _set_wing(self, state: AppState, payload: dict[str, Any]) -> None:
        params = state.wing.params.model_dump()
        for key in ("span_m", "aspect_ratio", "sweep_deg", "taper_ratio", "dihedral_deg", "twist_deg"):
            if key in payload and payload[key] is not None:
                params[key] = float(payload[key])

        if "wingtip_style" in payload and payload["wingtip_style"] is not None:
            wingtip_style = str(payload["wingtip_style"]).strip().lower()
            if wingtip_style not in ("straight", "pinched"):
                raise ValueError("wingtip_style\ub294 straight \ub610\ub294 pinched \uc911 \ud558\ub098\uc5ec\uc57c \ud569\ub2c8\ub2e4.")
            params["wingtip_style"] = wingtip_style

        params["span_m"] = max(0.15, min(20.0, params["span_m"]))
        params["aspect_ratio"] = max(2.0, min(30.0, params["aspect_ratio"]))
        params["sweep_deg"] = max(-35.0, min(45.0, params["sweep_deg"]))
        params["taper_ratio"] = max(0.1, min(1.2, params["taper_ratio"]))
        params["dihedral_deg"] = max(-10.0, min(20.0, params["dihedral_deg"]))
        params["twist_deg"] = max(-10.0, min(10.0, params["twist_deg"]))

        state.wing.params = WingParams.model_validate(params)

    def _set_analysis_conditions(self, state: AppState, payload: dict[str, Any]) -> None:
        current = state.analysis.conditions.model_dump()
        for key in ("aoa_start", "aoa_end", "aoa_step", "mach", "reynolds"):
            if key in payload:
                value = payload[key]
                current[key] = None if value in (None, "") and key == "reynolds" else float(value) if value is not None else None

        current["aoa_step"] = max(0.25, min(10.0, float(current["aoa_step"])))
        current["aoa_start"] = max(-30.0, min(30.0, float(current["aoa_start"])))
        current["aoa_end"] = max(-30.0, min(30.0, float(current["aoa_end"])))
        if current["aoa_end"] <= current["aoa_start"]:
            raise ValueError("AoA \uc885\ub8cc\uac12\uc740 \uc2dc\uc791\uac12\ubcf4\ub2e4 \ucee4\uc57c \ud569\ub2c8\ub2e4.")
        point_count = int(round((current["aoa_end"] - current["aoa_start"]) / current["aoa_step"])) + 1
        if point_count > 121:
            raise ValueError("AoA \uc0d8\ud50c \uc218\uac00 \ub108\ubb34 \ub9ce\uc2b5\ub2c8\ub2e4. \uc804\uccb4 \uc0ac\uc774\ub97c 121\uac1c \uc774\ud558\ub85c \uc904\uc5ec \uc8fc\uc138\uc694.")

        current["mach"] = max(0.01, min(0.6, float(current["mach"])))
        if current["reynolds"] is not None:
            current["reynolds"] = max(1_000.0, min(100_000_000.0, float(current["reynolds"])))

        state.analysis.conditions = AnalysisConditions.model_validate(current)

    def _set_active_solver(self, state: AppState, payload: dict[str, Any]) -> None:
        solver = str(payload.get("solver") or "").strip().lower()
        if solver not in ("openvsp", "neuralfoil"):
            raise ValueError("solver\ub294 openvsp \ub610\ub294 neuralfoil \uc911 \ud558\ub098\uc5ec\uc57c \ud569\ub2c8\ub2e4.")
        state.analysis.active_solver = solver

    @staticmethod
    def _snapshot_without_history(state: AppState) -> dict[str, Any]:
        return copy.deepcopy(state.model_dump(exclude={"history"}))

    @staticmethod
    def _push_history(state: AppState, snapshot: dict[str, Any]) -> None:
        state.history.append(snapshot)
        state.history = state.history[-30:]

    @staticmethod
    def _invalidate_geometry_outputs(state: AppState) -> None:
        state.wing.preview_mesh = None
        state.wing.planform_2d = None

    def _get_or_build_mesh(self, state: AppState) -> tuple[WingMesh, Planform2D]:
        cache_key = self._mesh_cache_key(state)
        cached = self._mesh_cache.get(cache_key)
        if cached is not None:
            self._mesh_cache.move_to_end(cache_key)
            mesh_payload, planform_payload = cached
            return WingMesh.model_validate(mesh_payload), Planform2D.model_validate(planform_payload)

        mesh, planform = build_wing_mesh(state.airfoil, state.wing.params)
        self._mesh_cache[cache_key] = (mesh.model_dump(), planform.model_dump())
        while len(self._mesh_cache) > 16:
            self._mesh_cache.popitem(last=False)
        return mesh, planform

    @staticmethod
    def _mesh_cache_key(state: AppState) -> str:
        payload = {
            "airfoil_summary": state.airfoil.summary.model_dump(),
            "airfoil_coords": state.airfoil.coords,
            "airfoil_upper": state.airfoil.upper,
            "airfoil_lower": state.airfoil.lower,
            "wing": state.wing.params.model_dump(),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _explain_state(self, state: AppState) -> str:
        airfoil = state.airfoil.summary
        wing = state.wing.params
        lines = [
            (
                f"{_TXT_AIRFOIL}: {airfoil.code or '-'} "
                f"({_TXT_THICKNESS} {airfoil.thickness_percent:.1f}%, {_TXT_CAMBER} {airfoil.max_camber_percent:.1f}%, "
                f"{_TXT_CAMBER_POS} {airfoil.max_camber_x_percent:.1f}%c)"
            ),
            (
                f"{_TXT_WING_SHAPE}: "
                f"{_TXT_SPAN} {wing.span_m:.2f}m, AR {wing.aspect_ratio:.1f}, {_TXT_SWEEP} {wing.sweep_deg:.1f}{_TXT_DEG}, "
                f"{_TXT_TAPER} {wing.taper_ratio:.2f}, {_TXT_DIHEDRAL} {wing.dihedral_deg:.1f}{_TXT_DEG}, "
                f"{_TXT_TWIST} {wing.twist_deg:.1f}{_TXT_DEG}, "
                f"{_TXT_WINGTIP} {_wingtip_style_label(str(wing.wingtip_style))}"
            ),
        ]

        active_solver, active = get_active_result(state.analysis)
        if active and active.metrics:
            metrics = active.metrics
            lines.append(f"{_TXT_LATEST_SOURCE}: {active.source_label}")
            if active_solver:
                lines.append(f"\ud65c\uc131 solver: {active_solver}")
            if active.fallback_reason:
                lines.append(f"{_TXT_FALLBACK_REASON}: {active.fallback_reason}")
            lines.append(
                f"{_TXT_CORE_PERF}: "
                f"\ucd5c\ub300 \uc591\ud56d\ube44(L/D) {metrics.ld_max:.2f} @ {_TXT_AOA} {metrics.ld_max_aoa:.1f}{_TXT_DEG}, "
                f"\ucd5c\ub300 \uc591\ub825\uacc4\uc218(CLmax) {metrics.cl_max:.3f} @ {metrics.cl_max_aoa:.1f}{_TXT_DEG}, "
                f"\ucd5c\uc18c \ud56d\ub825\uacc4\uc218(CDmin) {metrics.cd_min:.4f} @ {metrics.cd_min_aoa:.1f}{_TXT_DEG}"
            )

            curve = active.curve
            if curve.aoa_deg and curve.cl and curve.cd and curve.cm:
                def near_val(xs: list[float], ys: list[float], target: float) -> float:
                    idx = min(range(len(xs)), key=lambda i: abs(xs[i] - target))
                    return float(ys[idx])

                samples = [-10.0, -5.0, 0.0, 5.0, 10.0, 15.0, 20.0]
                sample_parts: list[str] = []
                for aoa in samples:
                    if aoa < min(curve.aoa_deg) or aoa > max(curve.aoa_deg):
                        continue
                    cl_v = near_val(curve.aoa_deg, curve.cl, aoa)
                    cd_v = near_val(curve.aoa_deg, curve.cd, aoa)
                    ld_v = (cl_v / cd_v) if abs(cd_v) > 1e-9 else 0.0
                    sample_parts.append(f"{aoa:.0f}{_TXT_DEG}: CL {cl_v:.3f}, CD {cd_v:.4f}, L/D {ld_v:.2f}")

                if sample_parts:
                    lines.append(f"{_TXT_AOA_SUMMARY}: " + " | ".join(sample_parts))

            stability = _TXT_STABILITY if metrics.cm_alpha < 0 else (_TXT_NEUTRAL if abs(metrics.cm_alpha) < 1e-6 else _TXT_UNSTABLE)
            lines.append(
                f"{_TXT_STABILITY_METRIC}: Cm_alpha {metrics.cm_alpha:.4f}/rad ({stability}), "
                f"{_TXT_ZERO_LIFT_AOA} {metrics.alpha_zero_lift:.2f}{_TXT_DEG}, CD0 {metrics.cd_zero:.4f}, Oswald e {metrics.oswald_e:.3f}"
            )

            extra = active.extra_data or {}
            precision_data = extra.get("precision_data")
            if isinstance(precision_data, dict):
                aoa_start = precision_data.get("aoa_start")
                aoa_end = precision_data.get("aoa_end")
                aoa_step = precision_data.get("aoa_step")
                if isinstance(aoa_start, (int, float)) and isinstance(aoa_end, (int, float)) and isinstance(aoa_step, (int, float)):
                    lines.append(
                        f"{_TXT_ANALYSIS_AOA}: {aoa_start:.1f}{_TXT_DEG} ~ {aoa_end:.1f}{_TXT_DEG}, "
                        f"{_TXT_INTERVAL} {aoa_step:.1f}{_TXT_DEG}"
                    )
                reynolds = precision_data.get("reynolds")
                if isinstance(reynolds, (int, float)) and reynolds > 0:
                    lines.append(f"{_TXT_REYNOLDS}: {float(reynolds):,.0f}")

            vspaero = extra.get("vspaero_all_data")
            if isinstance(vspaero, dict):
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
                vsp_parts: list[str] = []
                for key in ordered_keys:
                    value = vspaero.get(key)
                    if isinstance(value, (int, float)):
                        label = _VSPAERO_LABELS.get(key, key)
                        digits = 3 if abs(float(value)) >= 1 else 5
                        vsp_parts.append(f"{label} {float(value):.{digits}f}")
                if vsp_parts:
                    lines.append(f"{_TXT_VSPAERO_SUMMARY}: " + " | ".join(vsp_parts))
        else:
            lines.append(_TXT_NO_ANALYSIS)

        return "\n".join(lines)

    @staticmethod
    def command_from_tool(name: str, args: dict[str, Any] | None) -> CommandEnvelope:
        return CommandEngine.prepare_command(CommandEnvelope(type=name, payload=args or {}))

    @staticmethod
    def prepare_command(command: CommandEnvelope) -> CommandEnvelope:
        requested_type = command.type
        payload = command.payload or {}
        if not isinstance(payload, dict):
            raise ValueError("\uba85\ub839 payload\ub294 \uac1d\uccb4\uc5ec\uc57c \ud569\ub2c8\ub2e4.")

        allowed = allowed_payload_keys(requested_type)
        if allowed is None:
            raise ValueError(f"\uc9c0\uc6d0\ud558\uc9c0 \uc54a\ub294 \uba85\ub839 \ud0c0\uc785\uc785\ub2c8\ub2e4: {requested_type}")

        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"{requested_type}\uc5d0\uc11c \uc9c0\uc6d0\ud558\uc9c0 \uc54a\ub294 payload \ud0a4\uc785\ub2c8\ub2e4: {', '.join(unknown)}")

        clean_payload = dict(payload)
        normalized_type = normalize_command_name(requested_type)
        if normalized_type == "SetAirfoil" and "custom" in clean_payload:
            custom = clean_payload.get("custom")
            if not isinstance(custom, dict):
                raise ValueError("SetAirfoil.custom \uac12\uc740 \uac1d\uccb4\uc5ec\uc57c \ud569\ub2c8\ub2e4.")
            custom_unknown = sorted(set(custom) - CUSTOM_AIRFOIL_KEYS)
            if custom_unknown:
                raise ValueError(f"\uc9c0\uc6d0\ud558\uc9c0 \uc54a\ub294 \ucee4\uc2a4\ud140 \uc5d0\uc5b4\ud3ec\uc77c \ud0a4\uc785\ub2c8\ub2e4: {', '.join(custom_unknown)}")
            clean_payload["custom"] = dict(custom)

        return CommandEnvelope(type=normalized_type, payload=clean_payload)
