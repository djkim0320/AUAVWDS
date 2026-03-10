from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.models.state import AppState, CommandEnvelope, get_solver_result
from app.services.command_engine import CommandEngine
from app.services.fair_comparison import enrich_state_with_fair_comparison
from app.services.llm_chat import LLMChatOrchestrator
from app.services.state_store import SaveManager, StateStore
from app.services.state_summary import ClientAppState, build_llm_state_summary, serialize_client_state


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


class ClientStateResponse(BaseModel):
    state: ClientAppState
    applied_commands: list[CommandEnvelope] = Field(default_factory=list)
    explanation: str
    warnings: list[str] = Field(default_factory=list)
    assistant_message: str | None = None


_SAVE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_EXPORT_SUFFIXES = {".obj", ".json", ".vsp3"}


def create_app(work_dir: Path) -> FastAPI:
    app = FastAPI(title="AUAVWDS Backend", version="0.2.0")

    work_dir.mkdir(parents=True, exist_ok=True)
    if os.getenv('AUAV_ENABLE_WEB_BRIDGE') == '1':
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                'http://127.0.0.1:5173',
                'http://localhost:5173',
            ],
            allow_credentials=False,
            allow_methods=['*'],
            allow_headers=['*'],
        )

    store = StateStore(work_dir)
    saves = SaveManager(work_dir)
    engine = CommandEngine(work_dir)
    llm = LLMChatOrchestrator(timeout_sec=90)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

    @app.get("/state", response_model=AppState)
    def state() -> AppState:
        return enrich_state_with_fair_comparison(store.get())

    @app.get("/state/client", response_model=ClientAppState)
    def client_state() -> ClientAppState:
        return serialize_client_state(enrich_state_with_fair_comparison(store.get()))

    @app.post("/reset", response_model=ClientStateResponse)
    def reset() -> ClientStateResponse:
        s, _ = store.transact(lambda _state: (AppState(), None))
        enriched = enrich_state_with_fair_comparison(s)
        return ClientStateResponse(
            state=serialize_client_state(enriched),
            applied_commands=[CommandEnvelope(type="Reset", payload={})],
            explanation="State reset complete.",
            warnings=[],
            assistant_message="초기 상태로 리셋했어요.",
        )

    @app.post("/command", response_model=ClientStateResponse)
    def command(req: CommandRequest) -> ClientStateResponse:
        try:
            prepared_command = engine.prepare_command(req.command)
            next_state, explanation = store.transact(lambda state: engine.execute_prepared(state, prepared_command))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        enriched = enrich_state_with_fair_comparison(next_state)
        return ClientStateResponse(
            state=serialize_client_state(enriched),
            applied_commands=[prepared_command],
            explanation=explanation,
            warnings=[],
            assistant_message=explanation,
        )

    @app.post("/chat", response_model=ClientStateResponse)
    def chat(req: ChatRequest) -> ClientStateResponse:
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

        enriched = enrich_state_with_fair_comparison(state)
        return ClientStateResponse(
            state=serialize_client_state(enriched),
            applied_commands=applied_commands,
            explanation=assistant,
            warnings=[],
            assistant_message=assistant,
        )

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

    @app.post("/saves/load", response_model=ClientStateResponse)
    def load_state(req: LoadSaveRequest) -> ClientStateResponse:
        _validate_save_id(req.save_id)
        try:
            loaded = saves.load(req.save_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        loaded_state, _ = store.transact(lambda _state: (loaded, None))
        enriched = enrich_state_with_fair_comparison(loaded_state)
        return ClientStateResponse(
            state=serialize_client_state(enriched),
            applied_commands=[CommandEnvelope(type="Reset", payload={})],
            explanation="Saved snapshot loaded.",
            warnings=[],
            assistant_message="저장한 상태를 불러왔어요.",
        )

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
        out = _build_export_path(work_dir, req.format)
        suffix = out.suffix.lower()

        if suffix == ".vsp3":
            precision = get_solver_result(state.analysis, 'openvsp')
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
        state, explanation = engine.execute_prepared(state, cmd)
        applied_commands.append(cmd)
        tool_messages.append(explanation)
        enriched = enrich_state_with_fair_comparison(state)
        return {
            "ok": True,
            "command": cmd.type,
            "message": explanation,
            "state_summary": build_llm_state_summary(enriched),
        }

    initial_summary_state = enrich_state_with_fair_comparison(state)
    llm_out = llm.run_agent_turn(
        provider=req.provider,
        model=req.model,
        base_url=req.base_url,
        api_key=req.api_key,
        history=history,
        message=req.message,
        state_summary=build_llm_state_summary(initial_summary_state),
        tool_executor=execute_tool,
    )

    assistant = (llm_out.get("text") or "").strip()
    return state, {
        "assistant": assistant,
        "applied_commands": applied_commands,
        "tool_messages": tool_messages,
    }


def _build_export_path(work_dir: Path, requested_format: str | None = None) -> Path:
    export_dir = (work_dir / "exports").resolve()
    export_dir.mkdir(parents=True, exist_ok=True)

    format_hint = (requested_format or "").strip().lower()
    suffix = ".obj"

    if format_hint:
        suffix = f".{format_hint}"

    if suffix not in _EXPORT_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"format must be one of: {sorted(_EXPORT_SUFFIXES)}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return export_dir / f"wing_{stamp}{suffix}"


def mesh_to_obj(vertices: list[list[float]], triangles: list[list[int]]) -> str:
    lines: list[str] = ["# AUAVWDS wing mesh export"]
    for v in vertices:
        lines.append(f"v {v[0]} {v[1]} {v[2]}")
    for tri in triangles:
        a, b, c = tri
        lines.append(f"f {a + 1} {b + 1} {c + 1}")
    return "\n".join(lines) + "\n"
