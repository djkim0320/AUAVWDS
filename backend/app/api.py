from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    output_path: str | None = None


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
        s = store.reset()
        return {
            "state": s.model_dump(),
            "applied_commands": [{"type": "Reset", "payload": {}}],
            "explanation": "State reset complete.",
            "warnings": [],
            "assistant_message": "초기 상태로 리셋했어요.",
        }

    @app.post("/command")
    def command(req: CommandRequest) -> dict[str, Any]:
        state = store.get()
        try:
            next_state, explanation = engine.execute(state, req.command)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        store.set(next_state)
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

        state = store.get()
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

        try:
            llm_out = llm.run_agent_turn(
                provider=req.provider,
                model=req.model,
                base_url=req.base_url,
                api_key=req.api_key,
                history=[m.model_dump() for m in req.history],
                message=req.message,
                state_summary=summarize_state(state),
                tool_executor=execute_tool,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"LLM chat failed: {exc}") from exc

        assistant = (llm_out.get("text") or "").strip()
        if not assistant and tool_messages:
            assistant = tool_messages[-1].strip()
        if not assistant and applied_commands:
            assistant = "설계를 반영했어요. Airfoil / Wing 3D / Aerodynamics 탭에서 결과를 확인해 주세요."
        if not assistant:
            assistant = "모델 응답을 받지 못했어요. 다시 시도해 주세요."

        store.set(state)
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
        try:
            loaded = saves.load(req.save_id)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        store.set(loaded)
        return {
            "state": loaded.model_dump(),
            "applied_commands": [{"type": "Reset", "payload": {}}],
            "explanation": "Saved snapshot loaded.",
            "warnings": [],
            "assistant_message": "저장한 상태를 불러왔어요.",
        }

    @app.post("/saves/compare")
    def compare_state(req: CompareSaveRequest) -> dict[str, Any]:
        try:
            return saves.compare(req.left_id, req.right_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/export/cfd")
    def export_cfd(req: ExportCfdRequest) -> dict[str, Any]:
        state = store.get()
        export_dir = work_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)

        if req.output_path:
            out = Path(req.output_path)
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            out = export_dir / f"wing_{stamp}.obj"

        suffix = out.suffix.lower()
        allowed = {".obj", ".json", ".vsp3"}
        if suffix not in allowed:
            raise HTTPException(status_code=400, detail=f"output_path suffix must be one of: {sorted(allowed)}")

        if suffix == ".vsp3":
            precision = state.analysis.precision_result
            src = None
            if precision:
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
        "active_notes": active.notes if active else None,
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
