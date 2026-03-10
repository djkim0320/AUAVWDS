from __future__ import annotations

import os
import sys
from pathlib import Path


_DLL_DIR_HANDLES: list[object] = []
_PREPARED = False


def prepare_native_runtime_dirs() -> list[str]:
    global _PREPARED

    if _PREPARED or os.name != 'nt' or not hasattr(os, 'add_dll_directory'):
        return []

    candidates: list[Path] = []
    meipass = getattr(sys, '_MEIPASS', None)
    if isinstance(meipass, str) and meipass:
        base = Path(meipass)
        candidates.extend([base, base / 'casadi', base / 'neuralfoil'])

    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).resolve().parent
        internal_dir = exe_dir / '_internal'
        candidates.extend([internal_dir, internal_dir / 'casadi', internal_dir / 'neuralfoil', exe_dir])

    prepared: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if not resolved.is_dir():
            continue

        path_str = str(resolved)
        path_key = path_str.lower()
        if path_key in seen:
            continue
        seen.add(path_key)

        try:
            _DLL_DIR_HANDLES.append(os.add_dll_directory(path_str))
        except OSError:
            continue
        prepared.append(path_str)

    if prepared:
        path_parts = [part for part in os.environ.get('PATH', '').split(os.pathsep) if part]
        existing = {part.lower() for part in path_parts}
        prepend = [path for path in prepared if path.lower() not in existing]
        if prepend:
            os.environ['PATH'] = os.pathsep.join(prepend + path_parts)

    _PREPARED = True
    return prepared


def _reset_native_runtime_for_tests() -> None:
    global _PREPARED

    while _DLL_DIR_HANDLES:
        handle = _DLL_DIR_HANDLES.pop()
        close = getattr(handle, 'close', None)
        if callable(close):
            close()
    _PREPARED = False
