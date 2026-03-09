from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.models.state import AppState, CommandEnvelope
from app.services.command_engine import CommandEngine
from app.services.llm_chat import LLMChatOrchestrator
from app.services.state_store import SaveManager, StateStore


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = Field(default_factory=list)
    provider: str
    model: str
    base_url: str
    api_key: str


class CommandRequest(BaseModel):
    command: CommandEnvelope


class SaveRequest(BaseModel):
    name: str | None = None


class LoadSaveRequest(BaseModel):
    save_id: str


class CompareSaveRequest(BaseModel):
    left_id: str
    right_id: str


class ModelDiscoverRequest(BaseModel):
    provider: str
    base_url: str
    api_key: str


class ExportCfdRequest(BaseModel):
    format: Literal['obj', 'json', 'vsp3'] | None = None
    output_path: str | None = None


_SAVE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_EXPORT_SUFFIXES = {".obj", ".json", ".vsp3"}


def create_app(work_dir: Path) -> FastAPI:
    app = FastAPI(title="AUAVWDS Backend", version="0.2.0")

    work_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(work_dir)
    saves = SaveManager(work_dir)
    engine = CommandEngine(work_dir)
    llm = LLMChatOrchestrator(timeout_sec=90)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

    @app.get("/state")
    def state() -> dict[str, Any]:
        return store.get().model_dump()

    @app.post("/reset")
    def reset() -> dict[str, Any]:
        s, _ = store.transact(lambda _state: (AppState(), None))
        return {
            "state": s.model_dump(),
            "applied_commands": [{"type": "Reset", "payload": {}}],
            "explanation": "State reset complete.",
            "warnings": [],
            "assistant_message": "초기 상태로 리셋했어요.",
        }

    @app.post("/command")
    def command(req: CommandRequest) -> dict[str, Any]:
        try:
            next_state, explanation = store.transact(lambda state: engine.execute(state, req.command))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return {
            "state": next_state.model_dump(),
            "applied_commands": [req.command.model_dump()],
            "explanation": explanation,
            "warnings": [],
            "assistant_message": explanation,
        }

    @app.post("/chat")
    def chat(req: ChatRequest) -> dict[str, Any]:
        if not req.api_key.strip():
            raise HTTPException(status_code=400, detail="API key is required.")

        history = _dedupe_history([m.model_dump() for m in req.history], req.message)

        try:
            state, chat_meta = store.transact(
                lambda state: _run_chat_transaction(
                    state=state,
                    req=req,
                    history=history,
                    llm=llm,
                    engine=engine,
                )
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"LLM chat failed: {exc}") from exc

        assistant = str(chat_meta.get("assistant") or "").strip()
        applied_commands = chat_meta.get("applied_commands") or []
        if not assistant and chat_meta.get("tool_messages"):
            assistant = str(chat_meta["tool_messages"][-1]).strip()
        if not assistant and applied_commands:
            assistant = "설계를 반영했어요. Airfoil / Wing 3D / Aerodynamics 탭에서 결과를 확인해 주세요."
        if not assistant:
            assistant = "모델 응답을 받지 못했어요. 다시 시도해 주세요."

        return {
            "state": state.model_dump(),
            "applied_commands": [c.model_dump() for c in applied_commands],
            "explanation": assistant,
            "warnings": [],
            "assistant_message": assistant,
        }

    @app.post("/llm/discover")
    def discover(req: ModelDiscoverRequest) -> dict[str, Any]:
        return llm.discover_models(req.provider, req.base_url, req.api_key)

    @app.get("/saves")
    def list_saves() -> dict[str, Any]:
        rows = saves.list()
        minimal = [{k: r.get(k) for k in ("id", "name", "created_at", "summary")} for r in rows]
        return {"saves": minimal}

    @app.post("/saves")
    def save_state(req: SaveRequest) -> dict[str, Any]:
        return saves.save(store.get(), req.name)

    @app.post("/saves/load")
    def load_state(req: LoadSaveRequest) -> dict[str, Any]:
        _validate_save_id(req.save_id)
        try:
            loaded = saves.load(req.save_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        loaded_state, _ = store.transact(lambda _state: (loaded, None))
        return {
            "state": loaded_state.model_dump(),
            "applied_commands": [{"type": "Reset", "payload": {}}],
            "explanation": "Saved snapshot loaded.",
            "warnings": [],
            "assistant_message": "저장한 상태를 불러왔어요.",
        }

    @app.post("/saves/compare")
    def compare_state(req: CompareSaveRequest) -> dict[str, Any]:
        _validate_save_id(req.left_id)
        _validate_save_id(req.right_id)
        try:
            return saves.compare(req.left_id, req.right_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/export/cfd")
    def export_cfd(req: ExportCfdRequest) -> dict[str, Any]:
        state = store.get()
        out = _build_export_path(work_dir, req.output_path, req.format)
        suffix = out.suffix.lower()

        if suffix == ".vsp3":
            precision = state.analysis.precision_result
            src = None
            if precision and precision.analysis_mode == 'openvsp':
                maybe = precision.extra_data.get("vsp3_path")
                if isinstance(maybe, str):
                    p = Path(maybe)
                    if p.exists():
                        src = p
            if src is None:
                raise HTTPException(status_code=400, detail="No precision VSP3 file available. Run precision analysis first.")
            shutil.copy2(src, out)
            return {"ok": True, "path": str(out), "format": "vsp3"}

        mesh = state.wing.preview_mesh
        if not mesh or not mesh.vertices or not mesh.triangles:
            raise HTTPException(status_code=400, detail="Wing mesh is empty. BuildWingMesh first.")

        if suffix == ".json":
            out.write_text(json.dumps(mesh.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
            return {"ok": True, "path": str(out), "format": "json"}

        out.write_text(mesh_to_obj(mesh.vertices, mesh.triangles), encoding="utf-8")
        return {"ok": True, "path": str(out), "format": "obj"}

    return app


def _dedupe_history(history: list[dict[str, Any]], message: str) -> list[dict[str, Any]]:
    if not history:
        return history

    latest = history[-1]
    if latest.get("role") != "user":
        return history

    latest_text = str(latest.get("content") or "").strip()
    if latest_text != message.strip():
        return history
    return history[:-1]


def _validate_save_id(save_id: str) -> None:
    if not _SAVE_ID_RE.fullmatch(save_id or ""):
        raise HTTPException(status_code=400, detail="save_id must be a 32-character lowercase hex string.")


def _run_chat_transaction(
    *,
    state: AppState,
    req: ChatRequest,
    history: list[dict[str, Any]],
    llm: LLMChatOrchestrator,
    engine: CommandEngine,
) -> tuple[AppState, dict[str, Any]]:
    applied_commands: list[CommandEnvelope] = []
    tool_messages: list[str] = []

    def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        nonlocal state
        cmd = CommandEngine.command_from_tool(name, args)
        state, explanation = engine.execute(state, cmd)
        applied_commands.append(cmd)
        tool_messages.append(explanation)
        return {
            "ok": True,
            "command": cmd.type,
            "message": explanation,
            "state_summary": summarize_state(state),
        }

    llm_out = llm.run_agent_turn(
        provider=req.provider,
        model=req.model,
        base_url=req.base_url,
        api_key=req.api_key,
        history=history,
        message=req.message,
        state_summary=summarize_state(state),
        tool_executor=execute_tool,
    )

    assistant = (llm_out.get("text") or "").strip()
    return state, {
        "assistant": assistant,
        "applied_commands": applied_commands,
        "tool_messages": tool_messages,
    }


def _build_export_path(work_dir: Path, requested_output: str | None, requested_format: str | None = None) -> Path:
    export_dir = (work_dir / "exports").resolve()
    export_dir.mkdir(parents=True, exist_ok=True)

    requested = (requested_output or "").strip().lower()
    format_hint = (requested_format or "").strip().lower()
    suffix = ".obj"

    if format_hint:
        suffix = f".{format_hint}"
    elif requested:
        suffix = Path(requested).suffix.lower()

    if suffix not in _EXPORT_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"output_path suffix must be one of: {sorted(_EXPORT_SUFFIXES)}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return export_dir / f"wing_{stamp}{suffix}"


def summarize_state(state: AppState) -> dict[str, Any]:
    active = state.analysis.precision_result
    active_metric = active.metrics.model_dump() if active and active.metrics else None
    active_curve = None
    active_curve_range = None
    active_curve_samples = None
    precision_data = None
    vspaero_all_data = None
    vspaero_focus_data = None

    if active:
        active_curve = {
            "aoa_deg": [float(x) for x in (active.curve.aoa_deg or [])],
            "cl": [float(x) for x in (active.curve.cl or [])],
            "cd": [float(x) for x in (active.curve.cd or [])],
            "cm": [float(x) for x in (active.curve.cm or [])],
        }
        aoa = active_curve["aoa_deg"]
        cl = active_curve["cl"]
        cd = active_curve["cd"]
        cm = active_curve["cm"]
        if aoa and cl and cd and cm:
            active_curve_range = {
                "aoa_min": float(min(aoa)),
                "aoa_max": float(max(aoa)),
                "point_count": len(aoa),
            }
            target_aoa = [-10.0, -5.0, 0.0, 5.0, 10.0, 15.0, 20.0]
            samples: dict[str, dict[str, float]] = {}
            for a in target_aoa:
                idx = min(range(len(aoa)), key=lambda i: abs(aoa[i] - a))
                cd_i = cd[idx]
                ld_i = (cl[idx] / cd_i) if abs(cd_i) > 1e-9 else 0.0
                samples[f"{a:.0f}"] = {
                    "aoa_deg": float(aoa[idx]),
                    "cl": float(cl[idx]),
                    "cd": float(cd[idx]),
                    "cm": float(cm[idx]),
                    "ld": float(ld_i),
                }
            active_curve_samples = samples
        extra = active.extra_data or {}
        pd = extra.get("precision_data")
        va = extra.get("vspaero_all_data")
        if isinstance(pd, dict):
            precision_data = pd
        if isinstance(va, dict):
            vspaero_all_data = va
            focus_keys = [
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
            focus: dict[str, float] = {}
            for k in focus_keys:
                v = va.get(k)
                if isinstance(v, (int, float)):
                    focus[k] = float(v)
            vspaero_focus_data = focus or None

    return {
        "airfoil": state.airfoil.summary.model_dump(),
        "wing": state.wing.params.model_dump(),
        "analysis_mode": state.analysis.mode,
        "analysis_available": bool(active),
        "active_source_label": active.source_label if active else None,
        "active_result_mode": active.analysis_mode if active else None,
        "active_fallback_reason": active.fallback_reason if active else None,
        "active_notes": active.notes if active else None,
        "active_solver_airfoil": active.extra_data.get("solver_airfoil") if active else None,
        "has_mesh": bool(state.wing.preview_mesh and state.wing.preview_mesh.vertices),
        "active_metrics": active_metric,
        "active_curve": active_curve,
        "active_curve_range": active_curve_range,
        "active_curve_samples": active_curve_samples,
        "precision_data": precision_data,
        "vspaero_all_data": vspaero_all_data,
        "vspaero_focus_data": vspaero_focus_data,
    }


def mesh_to_obj(vertices: list[list[float]], triangles: list[list[int]]) -> str:
    lines: list[str] = ["# AUAVWDS wing mesh export"]
    for v in vertices:
        lines.append(f"v {v[0]} {v[1]} {v[2]}")
    for tri in triangles:
        a, b, c = tri
        lines.append(f"f {a + 1} {b + 1} {c + 1}")
    return "\n".join(lines) + "\n"
