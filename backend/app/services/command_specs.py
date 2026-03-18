from __future__ import annotations

from typing import Any


COMMAND_ALIASES: dict[str, str] = {
    "RunPrecisionAnalysis": "RunOpenVspAnalysis",
}

CUSTOM_AIRFOIL_KEYS = {
    "max_camber_percent",
    "max_camber_x_percent",
    "thickness_percent",
    "reflex_percent",
    "camber",
    "camber_pos",
    "thickness",
}

COMMAND_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "SetAirfoil",
        "description": "Set airfoil profile by code or custom parameters.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Airfoil code like 2412, clark-y, sd7037."},
                "custom": {
                    "type": "object",
                    "properties": {
                        "max_camber_percent": {"type": "number"},
                        "max_camber_x_percent": {"type": "number"},
                        "thickness_percent": {"type": "number"},
                        "reflex_percent": {"type": "number"},
                    },
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "SetWing",
        "description": "Set wing parameters.",
        "parameters": {
            "type": "object",
            "properties": {
                "span_m": {"type": "number"},
                "aspect_ratio": {"type": "number"},
                "sweep_deg": {"type": "number"},
                "taper_ratio": {"type": "number"},
                "dihedral_deg": {"type": "number"},
                "twist_deg": {"type": "number"},
                "wingtip_style": {
                    "type": "string",
                    "enum": ["straight", "pinched"],
                    "description": "Wingtip planform style. Keep straight as the default unless the user explicitly asks for a narrowed or pinched tip.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "BuildWingMesh",
        "description": "Generate 3D wing mesh preview.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "SetAnalysisConditions",
        "description": "Set aerodynamic analysis conditions shared by all solvers.",
        "parameters": {
            "type": "object",
            "properties": {
                "aoa_start": {"type": "number"},
                "aoa_end": {"type": "number"},
                "aoa_step": {"type": "number"},
                "mach": {"type": "number"},
                "reynolds": {"type": "number"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "SetActiveSolver",
        "description": "Choose which solver result is active in the UI.",
        "parameters": {
            "type": "object",
            "properties": {
                "solver": {
                    "type": "string",
                    "enum": ["openvsp", "neuralfoil"],
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "RunOpenVspAnalysis",
        "description": "Run OpenVSP/VSPAERO wing analysis.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "RunNeuralFoilAnalysis",
        "description": "Run NeuralFoil-based wing estimate analysis.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "RunPrecisionAnalysis",
        "description": "Legacy alias for RunOpenVspAnalysis.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "Explain",
        "description": "Explain current design and aerodynamic state.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "Undo",
        "description": "Revert last state-changing operation.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "Reset",
        "description": "Reset all design state to default.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def normalize_command_name(name: str) -> str:
    return COMMAND_ALIASES.get(name, name)


def allowed_payload_keys(name: str) -> set[str] | None:
    normalized = normalize_command_name(name)
    for tool in COMMAND_TOOL_DEFINITIONS:
        if tool["name"] == normalized:
            return set(tool["parameters"].get("properties", {}).keys())
    return None

