from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from app.models.state import AppState, default_app_state, get_active_result, migrate_legacy_state_payload

_T = TypeVar('_T')


class StateStore:
    def __init__(self, work_dir: Path):
        self._work_dir = work_dir
        self._lock = threading.RLock()
        self._state = default_app_state()
        self._work_dir.mkdir(parents=True, exist_ok=True)

    def get(self) -> AppState:
        with self._lock:
            return self._clone_state(self._state)

    def set(self, state: AppState) -> None:
        with self._lock:
            self._state = state

    def mutate(self, fn) -> AppState:
        with self._lock:
            state = AppState.model_validate(self._state.model_dump())
            next_state = fn(state)
            if not isinstance(next_state, AppState):
                raise TypeError('mutate callback must return AppState')
            self._state = next_state
            return self._clone_state(self._state)

    def transact(self, fn: Callable[[AppState], tuple[AppState, _T]]) -> tuple[AppState, _T]:
        with self._lock:
            working = self._clone_state(self._state)
            next_state, extra = fn(working)
            if not isinstance(next_state, AppState):
                raise TypeError('transaction callback must return (AppState, extra)')
            self._state = next_state
            return self._clone_state(self._state), extra

    def reset(self) -> AppState:
        with self._lock:
            self._state = default_app_state()
            return self._clone_state(self._state)

    @staticmethod
    def _clone_state(state: AppState) -> AppState:
        payload = migrate_legacy_state_payload(state.model_dump())
        return AppState.model_validate(payload)


class SaveManager:
    def __init__(self, work_dir: Path):
        self._save_dir = work_dir / 'saves'
        self._save_dir.mkdir(parents=True, exist_ok=True)
        self._save_id_re = re.compile(r'^[0-9a-f]{32}$')

    def list(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for p in self._save_dir.glob('*.json'):
            try:
                payload = json.loads(p.read_text(encoding='utf-8'))
                if not isinstance(payload, dict):
                    continue
                records.append(payload)
            except Exception:
                continue
        records.sort(key=self._sort_key, reverse=True)
        return records

    def save(self, state: AppState, name: str | None = None) -> dict[str, Any]:
        from datetime import datetime, timezone
        import uuid

        created_at = datetime.now(timezone.utc).isoformat()
        rec_id = uuid.uuid4().hex
        display = (name or '').strip() or f'Snapshot {created_at[:19]}'

        summary = self._build_summary(state)

        payload = {
            'id': rec_id,
            'name': display,
            'created_at': created_at,
            'summary': summary,
            'state': state.model_dump(),
        }
        (self._save_dir / f'{rec_id}.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        return {k: payload[k] for k in ('id', 'name', 'created_at', 'summary')}

    def load(self, save_id: str) -> AppState:
        payload = self._read_payload(save_id)
        state = payload.get('state')
        if not isinstance(state, dict):
            raise ValueError(f'Save is corrupted: {save_id}')
        migrated = migrate_legacy_state_payload(state if isinstance(state, dict) else {})
        return AppState.model_validate(migrated)

    def get_record(self, save_id: str) -> dict[str, Any]:
        payload = self._read_payload(save_id)
        return self._record_view(payload, save_id)

    def compare(self, left_id: str, right_id: str) -> dict[str, Any]:
        left_raw = self._read_payload(left_id)
        right_raw = self._read_payload(right_id)

        left_summary = self._normalized_summary(left_raw)
        right_summary = self._normalized_summary(right_raw)
        left_wing = self._summary_section(left_summary, 'wing')
        right_wing = self._summary_section(right_summary, 'wing')
        left_airfoil = self._summary_section(left_summary, 'airfoil')
        right_airfoil = self._summary_section(right_summary, 'airfoil')

        fields = [
            ('span_m', left_wing.get('span_m'), right_wing.get('span_m')),
            ('aspect_ratio', left_wing.get('aspect_ratio'), right_wing.get('aspect_ratio')),
            ('sweep_deg', left_wing.get('sweep_deg'), right_wing.get('sweep_deg')),
            ('taper_ratio', left_wing.get('taper_ratio'), right_wing.get('taper_ratio')),
            ('dihedral_deg', left_wing.get('dihedral_deg'), right_wing.get('dihedral_deg')),
            ('airfoil', left_airfoil.get('code'), right_airfoil.get('code')),
            (
                'airfoil_thickness_percent',
                left_airfoil.get('thickness_percent'),
                right_airfoil.get('thickness_percent'),
            ),
            (
                'airfoil_max_camber_percent',
                left_airfoil.get('max_camber_percent'),
                right_airfoil.get('max_camber_percent'),
            ),
            (
                'airfoil_max_camber_x_percent',
                left_airfoil.get('max_camber_x_percent'),
                right_airfoil.get('max_camber_x_percent'),
            ),
            ('active_solver', left_summary.get('active_solver'), right_summary.get('active_solver')),
            ('openvsp_status', left_summary.get('openvsp_status'), right_summary.get('openvsp_status')),
            ('neuralfoil_status', left_summary.get('neuralfoil_status'), right_summary.get('neuralfoil_status')),
            (
                'aoa_start',
                left_summary.get('conditions', {}).get('aoa_start'),
                right_summary.get('conditions', {}).get('aoa_start'),
            ),
            (
                'aoa_end',
                left_summary.get('conditions', {}).get('aoa_end'),
                right_summary.get('conditions', {}).get('aoa_end'),
            ),
            (
                'aoa_step',
                left_summary.get('conditions', {}).get('aoa_step'),
                right_summary.get('conditions', {}).get('aoa_step'),
            ),
            (
                'mach',
                left_summary.get('conditions', {}).get('mach'),
                right_summary.get('conditions', {}).get('mach'),
            ),
            (
                'reynolds',
                left_summary.get('conditions', {}).get('reynolds'),
                right_summary.get('conditions', {}).get('reynolds'),
            ),
        ]

        diffs: list[dict[str, Any]] = []
        for key, lval, rval in fields:
            delta = None
            if isinstance(lval, (int, float)) and isinstance(rval, (int, float)):
                delta = float(rval) - float(lval)
            diffs.append({'key': key, 'left': lval, 'right': rval, 'delta': delta})

        left_signature = left_airfoil.get('shape_signature')
        right_signature = right_airfoil.get('shape_signature')
        airfoil_scalars_changed = any(
            diff['key'].startswith('airfoil_') and diff['key'] != 'airfoil' and diff['left'] != diff['right']
            for diff in diffs
        )
        if left_signature != right_signature and not airfoil_scalars_changed:
            diffs.append(
                {
                    'key': 'airfoil_shape_signature',
                    'left': left_signature,
                    'right': right_signature,
                    'delta': None,
                }
            )

        return {
            'left': self._record_view(left_raw, left_id),
            'right': self._record_view(right_raw, right_id),
            'diffs': diffs,
            'summary': f"{left_raw.get('name')} -> {right_raw.get('name')} comparison complete",
        }

    def _read_payload(self, save_id: str) -> dict[str, Any]:
        path = self._resolve_save_path(save_id)
        if not path.exists():
            raise FileNotFoundError(f'Save not found: {save_id}')
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as exc:
            raise ValueError(f'Save is corrupted: {save_id}') from exc
        if not isinstance(payload, dict):
            raise ValueError(f'Save is corrupted: {save_id}')
        return payload

    def _record_view(self, payload: dict[str, Any], save_id: str) -> dict[str, Any]:
        required = ('id', 'name', 'created_at', 'summary')
        if any(key not in payload for key in required):
            raise ValueError(f'Save is corrupted: {save_id}')
        return {k: payload[k] for k in required}

    def _resolve_save_path(self, save_id: str) -> Path:
        if not self._save_id_re.fullmatch(save_id or ''):
            raise ValueError(f'Invalid save id: {save_id}')

        base = self._save_dir.resolve()
        path = (self._save_dir / f'{save_id}.json').resolve()
        if not path.is_relative_to(base):
            raise ValueError(f'Invalid save id: {save_id}')
        return path

    def _build_summary(self, state: AppState) -> dict[str, Any]:
        _, active = get_active_result(state.analysis)
        openvsp = state.analysis.results.openvsp
        neuralfoil = state.analysis.results.neuralfoil
        return {
            'airfoil': self._airfoil_summary(state),
            'wing': state.wing.params.model_dump(),
            'active_solver': state.analysis.active_solver,
            'active_mode': active.analysis_mode if active else None,
            'openvsp_status': openvsp.analysis_mode if openvsp else None,
            'neuralfoil_status': neuralfoil.analysis_mode if neuralfoil else None,
            'conditions': state.analysis.conditions.model_dump(),
        }

    def _normalized_summary(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary_raw = payload.get('summary')
        summary = dict(summary_raw) if isinstance(summary_raw, dict) else {}
        state = self._state_from_payload(payload)

        airfoil_raw = summary.get('airfoil')
        airfoil_summary = dict(airfoil_raw) if isinstance(airfoil_raw, dict) else {}
        if state is not None:
            fallback_airfoil = self._airfoil_summary(state)
            for key, value in fallback_airfoil.items():
                airfoil_summary.setdefault(key, value)
        if airfoil_summary:
            summary['airfoil'] = airfoil_summary

        wing_raw = summary.get('wing')
        wing_summary = dict(wing_raw) if isinstance(wing_raw, dict) else {}
        if state is not None and not wing_summary:
            wing_summary = state.wing.params.model_dump()
        if wing_summary:
            summary['wing'] = wing_summary

        if state is not None:
            summary.setdefault('active_solver', state.analysis.active_solver)
            _, active = get_active_result(state.analysis)
            summary.setdefault('active_mode', active.analysis_mode if active else None)
            summary.setdefault('openvsp_status', state.analysis.results.openvsp.analysis_mode if state.analysis.results.openvsp else None)
            summary.setdefault(
                'neuralfoil_status',
                state.analysis.results.neuralfoil.analysis_mode if state.analysis.results.neuralfoil else None,
            )
            cond_raw = summary.get('conditions')
            cond_summary = dict(cond_raw) if isinstance(cond_raw, dict) else {}
            if not cond_summary:
                cond_summary = state.analysis.conditions.model_dump()
            summary['conditions'] = cond_summary

        return summary

    @staticmethod
    def _summary_section(summary: dict[str, Any], key: str) -> dict[str, Any]:
        value = summary.get(key)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _state_from_payload(payload: dict[str, Any]) -> AppState | None:
        state_raw = payload.get('state')
        if not isinstance(state_raw, dict):
            return None
        try:
            return AppState.model_validate(migrate_legacy_state_payload(state_raw))
        except Exception:
            return None

    def _airfoil_summary(self, state: AppState) -> dict[str, Any]:
        summary = state.airfoil.summary.model_dump()
        summary['shape_signature'] = self._airfoil_shape_signature(state)
        return summary

    @staticmethod
    def _airfoil_shape_signature(state: AppState) -> str:
        shape_points = state.airfoil.coords or (state.airfoil.upper + state.airfoil.lower)
        signature_payload = {
            'summary': state.airfoil.summary.model_dump(),
            'coords': shape_points,
        }
        encoded = json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    @staticmethod
    def _sort_key(payload: dict[str, Any]) -> tuple[float, str]:
        raw_created_at = payload.get('created_at')
        if isinstance(raw_created_at, str):
            try:
                created_at = datetime.fromisoformat(raw_created_at.replace('Z', '+00:00')).timestamp()
            except ValueError:
                created_at = 0.0
        else:
            created_at = 0.0
        record_id = str(payload.get('id') or '')
        return created_at, record_id
