from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.models.state import AppState, default_app_state, migrate_legacy_state_payload


class StateStore:
    def __init__(self, work_dir: Path):
        self._work_dir = work_dir
        self._state_path = self._work_dir / 'state.json'
        self._lock = threading.RLock()
        self._state = default_app_state()
        self._work_dir.mkdir(parents=True, exist_ok=True)
        # Runtime state is intentionally session-only.
        # Persisted snapshots are handled by SaveManager.
        try:
            if self._state_path.exists():
                self._state_path.unlink()
        except Exception:
            pass

    def _load_if_exists(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding='utf-8'))
            payload = migrate_legacy_state_payload(payload if isinstance(payload, dict) else {})
            self._state = AppState.model_validate(payload)
        except Exception:
            self._state = default_app_state()

    def _persist(self) -> None:
        self._state_path.write_text(self._state.model_dump_json(indent=2), encoding='utf-8')

    def get(self) -> AppState:
        with self._lock:
            return AppState.model_validate(self._state.model_dump())

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
            return AppState.model_validate(self._state.model_dump())

    def reset(self) -> AppState:
        with self._lock:
            self._state = default_app_state()
            return self.get()


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

        summary = {
            'airfoil': state.airfoil.summary.model_dump(),
            'wing': state.wing.params.model_dump(),
            'mode': state.analysis.mode,
        }

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

        left_summary = left_raw.get('summary', {}) if isinstance(left_raw.get('summary'), dict) else {}
        right_summary = right_raw.get('summary', {}) if isinstance(right_raw.get('summary'), dict) else {}

        fields = [
            ('span_m', left_summary.get('wing', {}).get('span_m'), right_summary.get('wing', {}).get('span_m')),
            ('aspect_ratio', left_summary.get('wing', {}).get('aspect_ratio'), right_summary.get('wing', {}).get('aspect_ratio')),
            ('sweep_deg', left_summary.get('wing', {}).get('sweep_deg'), right_summary.get('wing', {}).get('sweep_deg')),
            ('taper_ratio', left_summary.get('wing', {}).get('taper_ratio'), right_summary.get('wing', {}).get('taper_ratio')),
            ('dihedral_deg', left_summary.get('wing', {}).get('dihedral_deg'), right_summary.get('wing', {}).get('dihedral_deg')),
            ('airfoil', left_summary.get('airfoil', {}).get('code'), right_summary.get('airfoil', {}).get('code')),
            ('analysis_mode', left_summary.get('mode'), right_summary.get('mode')),
        ]

        diffs: list[dict[str, Any]] = []
        for key, lval, rval in fields:
            delta = None
            if isinstance(lval, (int, float)) and isinstance(rval, (int, float)):
                delta = float(rval) - float(lval)
            diffs.append({'key': key, 'left': lval, 'right': rval, 'delta': delta})

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
