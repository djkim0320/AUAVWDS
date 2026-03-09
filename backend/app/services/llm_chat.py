from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        'name': 'SetAirfoil',
        'description': 'Set airfoil profile by code or custom parameters.',
        'parameters': {
            'type': 'object',
            'properties': {
                'code': {'type': 'string', 'description': 'Airfoil code like 2412, clark-y, sd7037.'},
                'custom': {
                    'type': 'object',
                    'properties': {
                        'max_camber_percent': {'type': 'number'},
                        'max_camber_x_percent': {'type': 'number'},
                        'thickness_percent': {'type': 'number'},
                        'reflex_percent': {'type': 'number'},
                    },
                },
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'SetWing',
        'description': 'Set wing parameters.',
        'parameters': {
            'type': 'object',
            'properties': {
                'span_m': {'type': 'number'},
                'aspect_ratio': {'type': 'number'},
                'sweep_deg': {'type': 'number'},
                'taper_ratio': {'type': 'number'},
                'dihedral_deg': {'type': 'number'},
                'twist_deg': {'type': 'number'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'BuildWingMesh',
        'description': 'Generate 3D wing mesh preview.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'SetAnalysisConditions',
        'description': 'Set aerodynamic analysis conditions shared by all solvers.',
        'parameters': {
            'type': 'object',
            'properties': {
                'aoa_start': {'type': 'number'},
                'aoa_end': {'type': 'number'},
                'aoa_step': {'type': 'number'},
                'mach': {'type': 'number'},
                'reynolds': {'type': 'number'},
            },
            'additionalProperties': False,
        },
    },
    {
        'name': 'RunOpenVspAnalysis',
        'description': 'Run OpenVSP/VSPAERO wing analysis.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'RunNeuralFoilAnalysis',
        'description': 'Run NeuralFoil-based wing estimate analysis.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'RunPrecisionAnalysis',
        'description': 'Legacy alias for RunOpenVspAnalysis.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'Explain',
        'description': 'Explain current design and aerodynamic state.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'Undo',
        'description': 'Revert last state-changing operation.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'Reset',
        'description': 'Reset all design state to default.',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
]


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    tool_call_id: str | None = None


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    assistant_payload: Any


class LLMChatOrchestrator:
    def __init__(self, timeout_sec: float = 90.0):
        self.timeout_sec = timeout_sec

    def discover_models(self, provider: str, base_url: str, api_key: str) -> dict[str, Any]:
        provider = provider.lower().strip()
        try:
            if provider == 'gemini':
                url = f"{base_url.rstrip('/')}/v1beta/models?key={api_key}"
                res = requests.get(url, timeout=self.timeout_sec)
                res.raise_for_status()
                data = res.json()
                models = [m.get('name', '').replace('models/', '') for m in data.get('models', []) if m.get('name')]
                return {'models': sorted(models), 'source_url': url, 'error': None}

            if provider in ('openai', 'grok'):
                url = f"{base_url.rstrip('/')}/models"
                headers = {'Authorization': f'Bearer {api_key}'}
                res = requests.get(url, headers=headers, timeout=self.timeout_sec)
                res.raise_for_status()
                data = res.json()
                models = [x.get('id') for x in data.get('data', []) if x.get('id')]
                return {'models': sorted(models), 'source_url': url, 'error': None}

            if provider == 'anthropic':
                url = f"{base_url.rstrip('/')}/v1/models"
                headers = {
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                }
                res = requests.get(url, headers=headers, timeout=self.timeout_sec)
                res.raise_for_status()
                data = res.json()
                models = [x.get('id') for x in data.get('data', []) if x.get('id')]
                return {'models': sorted(models), 'source_url': url, 'error': None}

            return {'models': [], 'source_url': '', 'error': f'Unsupported provider: {provider}'}
        except Exception as exc:
            return {'models': [], 'source_url': base_url, 'error': str(exc)}

    def run_agent_turn(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        history: list[dict[str, str]],
        message: str,
        state_summary: dict[str, Any],
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        provider = provider.lower().strip()

        if provider == 'gemini':
            return self._run_gemini(
                model=model,
                base_url=base_url,
                api_key=api_key,
                history=history,
                message=message,
                state_summary=state_summary,
                tool_executor=tool_executor,
            )

        if provider in ('openai', 'grok'):
            return self._run_openai_like(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=api_key,
                history=history,
                message=message,
                state_summary=state_summary,
                tool_executor=tool_executor,
            )

        if provider == 'anthropic':
            return self._run_anthropic(
                model=model,
                base_url=base_url,
                api_key=api_key,
                history=history,
                message=message,
                state_summary=state_summary,
                tool_executor=tool_executor,
            )

        raise ValueError(f'Unsupported provider: {provider}')

    def _run_gemini(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        history: list[dict[str, str]],
        message: str,
        state_summary: dict[str, Any],
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        contents = self._to_gemini_contents(history)
        contents.append({'role': 'user', 'parts': [{'text': message}]})

        applied_tools: list[dict[str, Any]] = []
        final_text = ''

        for _ in range(8):
            response = self._gemini_generate(
                model=model,
                base_url=base_url,
                api_key=api_key,
                contents=contents,
                state_summary=state_summary,
            )

            if response.tool_calls:
                model_parts = self._gemini_assistant_parts(response)
                contents.append({'role': 'model', 'parts': model_parts})

                result_parts = []
                for tc in response.tool_calls:
                    result = tool_executor(tc.name, tc.arguments)
                    applied_tools.append({'name': tc.name, 'arguments': tc.arguments})
                    result_parts.append(
                        {
                            'functionResponse': {
                                'name': tc.name,
                                'response': {
                                    'name': tc.name,
                                    'content': result,
                                },
                            }
                        }
                    )
                contents.append({'role': 'user', 'parts': result_parts})
                continue

            final_text = response.text.strip()
            if final_text:
                break

        return {'text': final_text, 'applied_tools': applied_tools}

    def _run_openai_like(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        history: list[dict[str, str]],
        message: str,
        state_summary: dict[str, Any],
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        messages = [{'role': 'system', 'content': self._system_prompt(state_summary)}]
        for m in history:
            role = 'assistant' if m.get('role') == 'assistant' else 'user'
            messages.append({'role': role, 'content': m.get('content', '')})
        messages.append({'role': 'user', 'content': message})

        tools = [{'type': 'function', 'function': td} for td in TOOL_DEFINITIONS]
        applied_tools: list[dict[str, Any]] = []
        final_text = ''

        for _ in range(8):
            body = {
                'model': model,
                'messages': messages,
                'tools': tools,
                'tool_choice': 'auto',
                'temperature': 0.4,
                'max_tokens': 2048,
            }
            headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
            url = f"{base_url.rstrip('/')}/chat/completions"
            payload = self._post_json(url, headers, body)

            choice = ((payload.get('choices') or [{}])[0])
            msg = choice.get('message') or {}
            tool_calls = msg.get('tool_calls') or []
            text = (msg.get('content') or '').strip()

            if tool_calls:
                messages.append(msg)
                for tc in tool_calls:
                    fn = tc.get('function') or {}
                    name = fn.get('name')
                    if not name:
                        continue
                    args_raw = fn.get('arguments') or '{}'
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                    except json.JSONDecodeError:
                        args = {}
                    result = tool_executor(name, args)
                    applied_tools.append({'name': name, 'arguments': args})
                    messages.append(
                        {
                            'role': 'tool',
                            'tool_call_id': tc.get('id'),
                            'content': json.dumps(result, ensure_ascii=False),
                        }
                    )
                continue

            final_text = text
            if final_text:
                break

        return {'text': final_text, 'applied_tools': applied_tools}

    def _run_anthropic(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        history: list[dict[str, str]],
        message: str,
        state_summary: dict[str, Any],
        tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        messages = []
        for m in history:
            role = 'assistant' if m.get('role') == 'assistant' else 'user'
            messages.append({'role': role, 'content': m.get('content', '')})
        messages.append({'role': 'user', 'content': message})

        tools = [
            {
                'name': td['name'],
                'description': td['description'],
                'input_schema': td['parameters'],
            }
            for td in TOOL_DEFINITIONS
        ]

        applied_tools: list[dict[str, Any]] = []
        final_text = ''

        for _ in range(8):
            url = f"{base_url.rstrip('/')}/v1/messages"
            headers = {
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            }
            body = {
                'model': model,
                'system': self._system_prompt(state_summary),
                'max_tokens': 2048,
                'temperature': 0.4,
                'messages': messages,
                'tools': tools,
            }
            payload = self._post_json(url, headers, body)
            content = payload.get('content') or []

            tool_uses = [p for p in content if p.get('type') == 'tool_use']
            texts = [p.get('text', '') for p in content if p.get('type') == 'text']

            if tool_uses:
                messages.append({'role': 'assistant', 'content': content})
                tool_results = []
                for tu in tool_uses:
                    name = tu.get('name')
                    tool_input = tu.get('input') or {}
                    result = tool_executor(name, tool_input)
                    applied_tools.append({'name': name, 'arguments': tool_input})
                    tool_results.append(
                        {
                            'type': 'tool_result',
                            'tool_use_id': tu.get('id'),
                            'content': json.dumps(result, ensure_ascii=False),
                        }
                    )
                messages.append({'role': 'user', 'content': tool_results})
                continue

            final_text = '\n'.join([t for t in texts if t]).strip()
            if final_text:
                break

        return {'text': final_text, 'applied_tools': applied_tools}

    def _gemini_generate(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        contents: list[dict[str, Any]],
        state_summary: dict[str, Any],
    ) -> LLMResponse:
        url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent?key={api_key}"

        body = {
            'systemInstruction': {'parts': [{'text': self._system_prompt(state_summary)}]},
            'contents': contents,
            'tools': [{'functionDeclarations': self._gemini_function_declarations()}],
            'toolConfig': {'functionCallingConfig': {'mode': 'AUTO'}},
            'generationConfig': {
                'temperature': 0.35,
                'topP': 0.9,
                'maxOutputTokens': 4096,
            },
        }

        payload = self._post_json(url, {'Content-Type': 'application/json'}, body)

        candidates = payload.get('candidates') or []
        if not candidates:
            raise ValueError('Empty response from model API.')

        content = (candidates[0] or {}).get('content') or {}
        parts = content.get('parts') or []

        tool_calls: list[ToolCall] = []
        texts: list[str] = []
        for p in parts:
            if 'functionCall' in p:
                fc = p.get('functionCall') or {}
                name = fc.get('name')
                args = fc.get('args') or {}
                if name:
                    tool_calls.append(ToolCall(name=name, arguments=args if isinstance(args, dict) else {}))
            elif 'text' in p and p.get('text'):
                texts.append(str(p.get('text')))

        text = '\n'.join(texts).strip()
        return LLMResponse(text=text, tool_calls=tool_calls, assistant_payload=parts)

    def _post_json(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        try:
            res = requests.post(url, headers=headers, json=body, timeout=self.timeout_sec)
            if res.status_code >= 400:
                text = res.text
                try:
                    payload = res.json()
                except Exception:
                    payload = {'detail': text}
                raise ValueError(payload.get('error', {}).get('message') or payload.get('detail') or text)
            return res.json()
        except requests.Timeout as exc:
            raise ValueError('LLM chat failed: The read operation timed out') from exc
        except requests.RequestException as exc:
            raise ValueError(f'LLM chat failed: {exc}') from exc

    def _to_gemini_contents(self, history: list[dict[str, str]]) -> list[dict[str, Any]]:
        contents = []
        for m in history:
            role = 'model' if m.get('role') == 'assistant' else 'user'
            text = (m.get('content') or '').strip()
            if not text:
                continue
            contents.append({'role': role, 'parts': [{'text': text}]})
        return contents

    def _gemini_assistant_parts(self, response: LLMResponse) -> list[dict[str, Any]]:
        if isinstance(response.assistant_payload, list) and response.assistant_payload:
            return response.assistant_payload

        parts = []
        if response.text:
            parts.append({'text': response.text})
        for tc in response.tool_calls:
            parts.append({'functionCall': {'name': tc.name, 'args': tc.arguments}})
        return parts

    def _gemini_function_declarations(self) -> list[dict[str, Any]]:
        out = []
        for td in TOOL_DEFINITIONS:
            out.append(
                {
                    'name': td['name'],
                    'description': td['description'],
                    'parameters': _strip_additional_properties(td['parameters']),
                }
            )
        return out

    def _system_prompt(self, state_summary: dict[str, Any]) -> str:
        return (
            "You are AUAVWDS wing-design copilot for beginners. Respond in natural Korean.\n"
            "Output plain text only. Do not use markdown or emphasis symbols such as **, __, `, #, *, -, >.\n"
            "Do not use decorative punctuation to simulate emphasis (for example repeated symbols).\n"
            "Do not reveal internal reasoning, hidden workspace state, or implementation details.\n"
            "Never output pseudo tool syntax or JSON commands in user-facing text.\n"
            "Use available tools proactively when they help the user goal.\n"
            "If key specs are missing, ask at most 1-2 concise clarification questions.\n"
            "If the user only says they want a wing/UAV/glider without numbers, first ask for basic specs: span in meters, flight goal, and one core geometry preference.\n"
            "Do not call BuildWingMesh or RunPrecisionAnalysis before span and flight goal are known.\n"
            "When user intent is clear enough, proceed without unnecessary confirmation loops.\n"
            "Tool policy:\n"
            "- Geometry update flow: SetAirfoil -> SetWing -> BuildWingMesh.\n"
            "- SetAnalysisConditions when the user explicitly asks for AoA, Mach, or Reynolds changes.\n"
            "- Use RunOpenVspAnalysis for higher-fidelity wing analysis.\n"
            "- Use RunNeuralFoilAnalysis for fast airfoil-based wing estimate analysis.\n"
            "- RunPrecisionAnalysis is a legacy alias for RunOpenVspAnalysis.\n"
            "- If an analysis already exists and airfoil/wing geometry is changed, rebuild the mesh and rerun the most relevant analysis.\n"
            "- Explain for data-grounded interpretation of latest results.\n"
            "- Undo/Reset only when explicitly requested.\n"
            "- If user does not specify dihedral, prefer an initial dihedral around 5 deg unless user asks for straight wing.\n"
            "Do not hardcode specific airfoils or wing shapes. Infer from user requirements.\n"
            "If the user asks for analysis explanation or interpretation, prioritize numeric data from state_summary.\n"
            "For analysis explanation, base statements on active_metrics, active_curve_samples, precision_data, and vspaero_focus_data.\n"
            "Do not invent any metric that is missing in state_summary. If data is missing, say what is missing and request or run analysis.\n"
            "When explaining analysis, include practical meaning for lift, drag, L/D, stall-related behavior, and stability using the provided numbers.\n"
            "If analysis exists, you may call Explain tool first and then provide a natural Korean explanation grounded in returned data.\n"
            "If the user asks to compare or improve, reference current state and suggest one clear next step.\n"
            f"Current state summary (JSON): {json.dumps(state_summary, ensure_ascii=False)}"
        )


def _strip_additional_properties(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == 'additionalProperties':
                continue
            out[k] = _strip_additional_properties(v)
        return out
    if isinstance(value, list):
        return [_strip_additional_properties(v) for v in value]
    return value


