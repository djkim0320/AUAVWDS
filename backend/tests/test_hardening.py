from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.analysis.common import derive_metrics
from app.analysis.naca import generate_custom_airfoil, generate_naca4
from app.analysis.neuralfoil_adapter import run_neuralfoil_analysis
from app.analysis.openvsp_adapter import (
    _extract_curve_families_from_polar,
    _extract_solver_effective_conditions,
    _finalize_polar_curve_payload,
    _parse_polar_rows,
    run_precision_analysis,
)
from app.api import _build_export_path, create_app
from app.geometry.wing_builder import _mock_pressure, build_wing_mesh
from app.models.state import AnalysisResult, AeroCurve, AirfoilState, AirfoilSummary, AppState, WingParams, get_active_result, set_solver_result
from app.runtime.native import _reset_native_runtime_for_tests, prepare_native_runtime_dirs
from app.services.state_store import SaveManager
from app.services.state_summary import build_llm_state_summary


class ApiHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.tmp.name)
        self.client = TestClient(create_app(self.work_dir))

    def tearDown(self) -> None:
        self.client.close()
        self.tmp.cleanup()

    def test_run_precision_rejects_hidden_payload_keys(self) -> None:
        res = self.client.post(
            '/command',
            json={'command': {'type': 'RunPrecisionAnalysis', 'payload': {'solver_bin_dir': 'C:\\evil'}}},
        )

        self.assertEqual(res.status_code, 400)
        self.assertIn('RunPrecisionAnalysis에서 지원하지 않는 payload 키입니다', res.json()['detail'])

    def test_load_rejects_traversal_style_save_id(self) -> None:
        res = self.client.post('/saves/load', json={'save_id': '..\\outside'})

        self.assertEqual(res.status_code, 400)
        self.assertIn('save_id must be a 32-character lowercase hex string', res.json()['detail'])

    def test_chat_dedupes_latest_user_message_from_history(self) -> None:
        captured: dict[str, object] = {}

        def fake_run_agent_turn(self, **kwargs):
            captured['history'] = kwargs['history']
            return {'text': 'ok', 'applied_tools': []}

        with patch('app.services.llm_chat.LLMChatOrchestrator.run_agent_turn', new=fake_run_agent_turn):
            res = self.client.post(
                '/chat',
                json={
                    'message': 'current input',
                    'history': [
                        {'role': 'assistant', 'content': 'previous reply'},
                        {'role': 'user', 'content': 'current input'},
                    ],
                    'provider': 'openai',
                    'model': 'gpt-5.2',
                    'base_url': 'https://example.invalid/v1',
                    'api_key': 'test-key',
                },
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(captured['history'], [{'role': 'assistant', 'content': 'previous reply'}])

    def test_export_writes_inside_generated_exports_dir(self) -> None:
        self._prepare_mesh()

        res = self.client.post('/export/cfd', json={'format': 'json'})

        self.assertEqual(res.status_code, 200)
        payload = res.json()
        exported = Path(payload['path']).resolve()
        export_dir = (self.work_dir / 'exports').resolve()

        self.assertTrue(exported.is_relative_to(export_dir))
        self.assertEqual(exported.suffix, '.json')
        self.assertTrue(exported.exists())

    def test_analysis_conditions_and_active_solver_roundtrip_through_commands(self) -> None:
        res = self.client.post(
            '/command',
            json={
                'command': {
                    'type': 'SetAnalysisConditions',
                    'payload': {'aoa_start': -4, 'aoa_end': 12, 'aoa_step': 2, 'mach': 0.12, 'reynolds': 450000},
                }
            },
        )
        self.assertEqual(res.status_code, 200)
        state = res.json()['state']
        self.assertEqual(state['analysis']['conditions']['aoa_start'], -4.0)
        self.assertEqual(state['analysis']['conditions']['aoa_end'], 12.0)
        self.assertEqual(state['analysis']['conditions']['aoa_step'], 2.0)
        self.assertEqual(state['analysis']['conditions']['mach'], 0.12)
        self.assertEqual(state['analysis']['conditions']['reynolds'], 450000.0)

        res = self.client.post('/command', json={'command': {'type': 'SetActiveSolver', 'payload': {'solver': 'neuralfoil'}}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['state']['analysis']['active_solver'], 'neuralfoil')

    def test_state_client_route_and_command_response_strip_heavy_state_fields(self) -> None:
        set_airfoil = self.client.post('/command', json={'command': {'type': 'SetAirfoil', 'payload': {'code': '2412'}}})
        self.assertEqual(set_airfoil.status_code, 200)

        build_mesh = self.client.post('/command', json={'command': {'type': 'BuildWingMesh', 'payload': {}}})
        self.assertEqual(build_mesh.status_code, 200)
        self._assert_client_state_shape(build_mesh.json()['state'])

        neuralfoil = self.client.post('/command', json={'command': {'type': 'RunNeuralFoilAnalysis', 'payload': {}}})
        self.assertEqual(neuralfoil.status_code, 200)
        self._assert_client_state_shape(neuralfoil.json()['state'])

        full_state = self.client.get('/state').json()
        client_state = self.client.get('/state/client').json()
        self._assert_client_state_shape(client_state)

        self.assertGreaterEqual(len(full_state['history']), 1)
        self.assertTrue(full_state['airfoil']['coords'])
        self.assertIsNotNone(full_state['wing']['planform_2d'])
        self.assertTrue(full_state['wing']['preview_mesh']['pressure_overlay'])
        self.assertIsNone(client_state['wing']['preview_mesh'])

        full_extra = full_state['analysis']['results']['neuralfoil']['extra_data']
        client_extra = client_state['analysis']['results']['neuralfoil']['extra_data']
        self.assertIn('raw_neuralfoil_output', full_extra)
        self.assertNotIn('raw_neuralfoil_output', client_extra)
        self.assertIn('solver_scalar_data', client_extra)
        self.assertEqual(client_state['analysis']['results']['neuralfoil']['curve']['aoa_deg'], [])

    def test_reset_chat_and_load_return_client_state_shape(self) -> None:
        self._prepare_mesh()
        save_res = self.client.post('/saves', json={'name': 'baseline'})
        self.assertEqual(save_res.status_code, 200)
        save_id = save_res.json()['id']

        with patch('app.services.llm_chat.LLMChatOrchestrator.run_agent_turn', return_value={'text': 'ok', 'applied_tools': []}):
            chat_res = self.client.post(
                '/chat',
                json={
                    'message': 'status',
                    'history': [],
                    'provider': 'openai',
                    'model': 'gpt-5.2',
                    'base_url': 'https://example.invalid/v1',
                    'api_key': 'test-key',
                },
            )

        reset_res = self.client.post('/reset')
        load_res = self.client.post('/saves/load', json={'save_id': save_id})

        self.assertEqual(chat_res.status_code, 200)
        self.assertEqual(reset_res.status_code, 200)
        self.assertEqual(load_res.status_code, 200)
        self._assert_client_state_shape(chat_res.json()['state'])
        self._assert_client_state_shape(reset_res.json()['state'])
        self._assert_client_state_shape(load_res.json()['state'])

    def test_set_wing_accepts_explicit_wingtip_style(self) -> None:
        res = self.client.post(
            '/command',
            json={'command': {'type': 'SetWing', 'payload': {'span_m': 2.4, 'wingtip_style': 'pinched'}}},
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['state']['wing']['params']['wingtip_style'], 'pinched')

    def test_chat_and_command_updates_are_not_lost_under_concurrency(self) -> None:
        app = create_app(self.work_dir)
        chat_started = threading.Event()
        allow_chat_finish = threading.Event()
        responses: dict[str, object] = {}

        def fake_run_agent_turn(self, **kwargs):
            chat_started.set()
            if not allow_chat_finish.wait(timeout=2):
                raise RuntimeError('chat test timed out')
            kwargs['tool_executor']('SetAirfoil', {'code': '2412'})
            return {'text': 'ok', 'applied_tools': [{'name': 'SetAirfoil', 'arguments': {'code': '2412'}}]}

        with (
            patch('app.services.llm_chat.LLMChatOrchestrator.run_agent_turn', new=fake_run_agent_turn),
            TestClient(app) as chat_client,
            TestClient(app) as cmd_client,
            TestClient(app) as read_client,
        ):
            def run_chat() -> None:
                responses['chat'] = chat_client.post(
                    '/chat',
                    json={
                        'message': 'set airfoil',
                        'history': [],
                        'provider': 'openai',
                        'model': 'gpt-5.2',
                        'base_url': 'https://example.invalid/v1',
                        'api_key': 'test-key',
                    },
                )

            def run_command() -> None:
                chat_started.wait(timeout=2)
                responses['command'] = cmd_client.post(
                    '/command',
                    json={'command': {'type': 'SetWing', 'payload': {'span_m': 2.7, 'sweep_deg': 12}}},
                )

            chat_thread = threading.Thread(target=run_chat)
            command_thread = threading.Thread(target=run_command)
            chat_thread.start()
            self.assertTrue(chat_started.wait(timeout=2))
            command_thread.start()
            time.sleep(0.1)
            allow_chat_finish.set()
            chat_thread.join(timeout=5)
            command_thread.join(timeout=5)

            self.assertFalse(chat_thread.is_alive())
            self.assertFalse(command_thread.is_alive())

            chat_res = responses.get('chat')
            command_res = responses.get('command')
            self.assertIsNotNone(chat_res)
            self.assertIsNotNone(command_res)
            self.assertEqual(chat_res.status_code, 200)
            self.assertEqual(command_res.status_code, 200)

            state = read_client.get('/state').json()

        self.assertEqual(state['airfoil']['summary']['code'], 'NACA 2412')
        self.assertAlmostEqual(state['wing']['params']['span_m'], 2.7)
        self.assertAlmostEqual(state['wing']['params']['sweep_deg'], 12.0)

    def test_run_precision_alias_normalizes_without_duplicate_history(self) -> None:
        res = self.client.post('/command', json={'command': {'type': 'RunPrecisionAnalysis', 'payload': {}}})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()['applied_commands'][0]['type'], 'RunOpenVspAnalysis')

        full_state = self.client.get('/state').json()
        self.assertEqual(len(full_state['history']), 1)

    def test_set_wing_invalidates_preview_mesh_and_planform(self) -> None:
        self._prepare_mesh()

        res = self.client.post('/command', json={'command': {'type': 'SetWing', 'payload': {'span_m': 2.4}}})

        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()['state']['wing']['preview_mesh'])

        full_state = self.client.get('/state').json()
        self.assertIsNone(full_state['wing']['preview_mesh'])
        self.assertIsNone(full_state['wing']['planform_2d'])

    def test_build_wing_mesh_reuses_cached_geometry_for_identical_inputs(self) -> None:
        set_airfoil = self.client.post('/command', json={'command': {'type': 'SetAirfoil', 'payload': {'code': '2412'}}})
        self.assertEqual(set_airfoil.status_code, 200)

        with patch('app.services.command_engine.build_wing_mesh', wraps=build_wing_mesh) as mocked_build:
            first = self.client.post('/command', json={'command': {'type': 'BuildWingMesh', 'payload': {}}})
            second = self.client.post('/command', json={'command': {'type': 'BuildWingMesh', 'payload': {}}})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(mocked_build.call_count, 1)
        self._assert_client_state_shape(first.json()['state'])
        self._assert_client_state_shape(second.json()['state'])

    def test_noop_commands_do_not_pollute_history(self) -> None:
        scenarios = [
            {'type': 'SetAirfoil', 'payload': {'code': '2412'}},
            {'type': 'SetWing', 'payload': {'span_m': 2.4, 'sweep_deg': 12.0}},
            {'type': 'SetAnalysisConditions', 'payload': {'aoa_start': -4.0, 'aoa_end': 12.0, 'aoa_step': 2.0, 'mach': 0.12, 'reynolds': 450000}},
            {'type': 'SetActiveSolver', 'payload': {'solver': 'neuralfoil'}},
        ]

        for command in scenarios:
            with self.subTest(command=command['type']):
                self.client.post('/reset')
                first = self.client.post('/command', json={'command': command})
                second = self.client.post('/command', json={'command': command})
                self.assertEqual(first.status_code, 200)
                self.assertEqual(second.status_code, 200)
                full_state = self.client.get('/state').json()
                self.assertEqual(len(full_state['history']), 1)

    def test_mutating_commands_append_single_history_snapshot(self) -> None:
        commands = [
            {'type': 'SetAirfoil', 'payload': {'code': '2412'}},
            {'type': 'SetWing', 'payload': {'span_m': 2.6}},
            {'type': 'SetAnalysisConditions', 'payload': {'aoa_start': -6.0, 'aoa_end': 10.0, 'aoa_step': 2.0, 'mach': 0.11, 'reynolds': 350000}},
            {'type': 'SetActiveSolver', 'payload': {'solver': 'neuralfoil'}},
        ]

        self.client.post('/reset')
        for expected_length, command in enumerate(commands, start=1):
            res = self.client.post('/command', json={'command': command})
            self.assertEqual(res.status_code, 200)
            full_state = self.client.get('/state').json()
            self.assertEqual(len(full_state['history']), expected_length)

    def test_repeated_build_wing_mesh_does_not_append_history_for_identical_geometry(self) -> None:
        self.client.post('/reset')
        self.client.post('/command', json={'command': {'type': 'SetAirfoil', 'payload': {'code': '2412'}}})

        first = self.client.post('/command', json={'command': {'type': 'BuildWingMesh', 'payload': {}}})
        second = self.client.post('/command', json={'command': {'type': 'BuildWingMesh', 'payload': {}}})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        full_state = self.client.get('/state').json()
        self.assertEqual(len(full_state['history']), 2)

    def test_llm_state_summary_excludes_full_curve_arrays(self) -> None:
        state = AppState(airfoil=AirfoilState.model_validate(generate_naca4('2412')))
        set_solver_result(state.analysis, 'neuralfoil', run_neuralfoil_analysis(state, self.work_dir))

        summary = build_llm_state_summary(state)

        self.assertNotIn('active_curve', summary)
        self.assertIsNotNone(summary['active_curve_range'])
        self.assertIsNotNone(summary['active_curve_samples'])
        self.assertIn('precision_data', summary)
        self.assertIn('vspaero_focus_data', summary)

    def test_llm_state_summary_omits_out_of_range_targets_and_marks_nearest_samples_explicitly(self) -> None:
        state = AppState()
        set_solver_result(
            state.analysis,
            'openvsp',
            AnalysisResult(
                source_label='test result',
                analysis_mode='openvsp',
                curve=AeroCurve(
                    aoa_deg=[0.0, 4.0, 8.0],
                    cl=[0.1, 0.45, 0.8],
                    cd=[0.01, 0.02, 0.04],
                    cm=[-0.01, -0.03, -0.05],
                ),
                extra_data={},
                notes='',
            ),
        )

        summary = build_llm_state_summary(state)
        samples = summary['active_curve_samples']

        self.assertEqual(summary['active_curve_range'], {'aoa_min': 0.0, 'aoa_max': 8.0, 'point_count': 3})
        self.assertEqual([sample['requested_aoa_deg'] for sample in samples], [0.0, 5.0])
        self.assertEqual(samples[0]['sampled_aoa_deg'], 0.0)
        self.assertTrue(samples[0]['exact_match'])
        self.assertEqual(samples[1]['sampled_aoa_deg'], 4.0)
        self.assertFalse(samples[1]['exact_match'])

    def _prepare_mesh(self) -> None:
        set_airfoil = self.client.post('/command', json={'command': {'type': 'SetAirfoil', 'payload': {'code': '2412'}}})
        self.assertEqual(set_airfoil.status_code, 200)
        build_mesh = self.client.post('/command', json={'command': {'type': 'BuildWingMesh', 'payload': {}}})
        self.assertEqual(build_mesh.status_code, 200)

    def _assert_client_state_shape(self, state: dict[str, object]) -> None:
        self.assertEqual(state['history'], [])
        self.assertEqual(state['airfoil']['coords'], [])
        self.assertEqual(state['airfoil']['upper'], [])
        self.assertEqual(state['airfoil']['lower'], [])
        self.assertEqual(state['airfoil']['camber'], [])
        self.assertIsNone(state['wing']['planform_2d'])
        self.assertIsNone(state['wing']['preview_mesh'])
        for solver in ('openvsp', 'neuralfoil'):
            result = state['analysis']['results'][solver]
            if result is not None:
                self.assertEqual(result['curve']['aoa_deg'], [])
                self.assertEqual(result['curve']['cl'], [])
                self.assertEqual(result['curve']['cd'], [])
                self.assertEqual(result['curve']['cm'], [])


class PrecisionAnalysisTests(unittest.TestCase):
    def test_naca_airfoil_changes_generated_solver_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            'app.analysis.openvsp_adapter._resolve_solver_paths',
            return_value={'bin_dir': None, 'vsp_exe': None, 'vspaero_exe': None},
        ):
            work_dir = Path(tmp_dir)
            state_2412 = AppState(airfoil=AirfoilState.model_validate(generate_naca4('2412')))
            state_0012 = AppState(airfoil=AirfoilState.model_validate(generate_naca4('0012')))

            result_2412 = run_precision_analysis(state_2412, work_dir / 'naca2412')
            result_0012 = run_precision_analysis(state_0012, work_dir / 'naca0012')

            script_2412 = Path(result_2412.extra_data['script_path']).read_text(encoding='utf-8')
            script_0012 = Path(result_0012.extra_data['script_path']).read_text(encoding='utf-8')

        self.assertNotEqual(script_2412, script_0012)
        self.assertIn('GetXSecParm( xsec0, "Camber" ), 0.020000', script_2412)
        self.assertIn('GetXSecParm( xsec0, "Camber" ), 0.000000', script_0012)
        self.assertEqual(result_2412.extra_data['solver_airfoil']['representation_label'], 'NACA 2412')
        self.assertEqual(result_0012.extra_data['solver_airfoil']['representation_label'], 'NACA 0012')

    def test_custom_airfoil_creates_solver_file_and_reports_fallback_reason(self) -> None:
        payload = generate_custom_airfoil(
            max_camber_percent=3.0,
            max_camber_x_percent=35.0,
            thickness_percent=11.0,
            reflex_percent=0.5,
        )
        payload['summary']['code'] = 'Mission Custom Airfoil'

        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            'app.analysis.openvsp_adapter._resolve_solver_paths',
            return_value={'bin_dir': None, 'vsp_exe': None, 'vspaero_exe': None},
        ):
            result = run_precision_analysis(
                AppState(airfoil=AirfoilState.model_validate(payload)),
                Path(tmp_dir),
            )

            solver_airfoil = result.extra_data['solver_airfoil']
            solver_file = Path(solver_airfoil['file_path'])
            script = Path(result.extra_data['script_path']).read_text(encoding='utf-8')
            solver_text = solver_file.read_text(encoding='utf-8')
            solver_exists = solver_file.exists()

        self.assertEqual(result.analysis_mode, 'fallback')
        self.assertTrue(result.fallback_reason)
        self.assertEqual(solver_airfoil['geometry_kind'], 'custom_file')
        self.assertTrue(solver_exists)
        self.assertIn('Mission Custom Airfoil', solver_text)
        self.assertIn('XS_FILE_AIRFOIL', script)
        self.assertIn(solver_file.name, script)

    def test_real_solver_and_fallback_results_are_clearly_distinct(self) -> None:
        state = AppState(airfoil=AirfoilState.model_validate(generate_naca4('2412')))

        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)

            with patch(
                'app.analysis.openvsp_adapter._resolve_solver_paths',
                return_value={'bin_dir': None, 'vsp_exe': None, 'vspaero_exe': None},
            ):
                fallback_result = run_precision_analysis(state, work_dir / 'fallback')

            solver_dir = work_dir / 'solver_bin'
            solver_dir.mkdir(parents=True, exist_ok=True)
            vsp_exe = solver_dir / 'vsp.exe'
            vspaero_exe = solver_dir / 'vspaero.exe'
            vsp_exe.write_text('', encoding='utf-8')
            vspaero_exe.write_text('', encoding='utf-8')

            stdout = '\n'.join(
                [
                    '1 0.0000 -2.0000 0.0800 0.0000 0.0000 -0.2000 0.0000 0.0000 0.0100 0.0000 0.0000 0.0000 -0.0200 0.8000',
                    '1 0.0000 0.0000 0.0800 0.0000 0.0000 0.0000 0.0000 0.0000 0.0090 0.0000 0.0000 0.0000 -0.0100 0.8200',
                    '1 0.0000 2.0000 0.0800 0.0000 0.0000 0.2000 0.0000 0.0000 0.0110 0.0000 0.0000 0.0000 0.0000 0.7800',
                ]
            )

            def fake_subprocess_run(cmd, cwd, **kwargs):
                Path(cwd, 'auav_case.vsp3').write_text('vsp3', encoding='utf-8')
                return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

            with (
                patch(
                    'app.analysis.openvsp_adapter._resolve_solver_paths',
                    return_value={'bin_dir': solver_dir, 'vsp_exe': vsp_exe, 'vspaero_exe': vspaero_exe},
                ),
                patch('app.analysis.openvsp_adapter.subprocess.run', side_effect=fake_subprocess_run),
            ):
                real_result = run_precision_analysis(state, work_dir / 'real')
                vsp3_exists = Path(real_result.extra_data['vsp3_path']).exists()

        self.assertEqual(fallback_result.analysis_mode, 'fallback')
        self.assertTrue(fallback_result.fallback_reason)
        self.assertEqual(fallback_result.source_label, '정밀 해석(OpenVSP/VSPAERO, 대체 경로)')
        self.assertEqual(fallback_result.extra_data['solver_id'], 'openvsp')

        self.assertEqual(real_result.analysis_mode, 'openvsp')
        self.assertIsNone(real_result.fallback_reason)
        self.assertEqual(real_result.source_label, '정밀 해석(OpenVSP/VSPAERO)')
        self.assertNotEqual(real_result.source_label, fallback_result.source_label)
        self.assertEqual(real_result.extra_data['solver_airfoil']['geometry_kind'], 'naca4')
        self.assertEqual(real_result.extra_data['solver_id'], 'openvsp')
        self.assertTrue(vsp3_exists)

    def test_openvsp_prefers_filtered_polar_curve_when_stdout_rows_are_unstable(self) -> None:
        state = AppState(airfoil=AirfoilState.model_validate(generate_naca4('2412')))
        state.analysis.conditions.aoa_start = -2.0
        state.analysis.conditions.aoa_end = 12.0
        state.analysis.conditions.aoa_step = 2.0

        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            solver_dir = work_dir / 'solver_bin'
            solver_dir.mkdir(parents=True, exist_ok=True)
            vsp_exe = solver_dir / 'vsp.exe'
            vspaero_exe = solver_dir / 'vspaero.exe'
            vsp_exe.write_text('', encoding='utf-8')
            vspaero_exe.write_text('', encoding='utf-8')

            stdout = '\n'.join(
                [
                    '3 0.08000 -2.00000 0.00000 0.00000 0.10000 0.10000 0.01000 -0.00900 0.00001 10000.00000 0.00000 0.00000 -0.01000 0.50000',
                    '3 0.08000 0.00000 0.00000 0.00000 0.20000 0.20000 0.01000 -0.00800 0.00001 20000.00000 0.00000 0.00000 -0.02000 0.50000',
                    '3 0.08000 2.00000 0.00000 0.00000 0.30000 0.30000 0.01000 -0.00700 0.00001 30000.00000 0.00000 0.00000 -0.03000 0.50000',
                ]
            )
            polar = '\n'.join(
                [
                    'Beta Mach AoA Re/1e6 CLtot CDo CDi CDtot L/D E CMytot',
                    '0.0 0.08 -2.0 10.0 0.080 0.0055 0.0010 0.0065 12.31 0.85 -0.020',
                    '0.0 0.08 0.0 10.0 0.180 0.0056 0.0025 0.0081 22.22 0.88 -0.030',
                    '0.0 0.08 2.0 10.0 0.330 0.0060 0.0039 0.0099 33.33 0.90 -0.045',
                    '0.0 0.08 4.0 10.0 0.470 0.0070 0.0054 0.0124 37.90 0.91 -0.060',
                    '0.0 0.08 6.0 10.0 0.610 0.0084 0.0070 0.0154 39.61 0.92 -0.075',
                    '0.0 0.08 8.0 10.0 0.740 0.0102 0.0088 0.0190 38.95 0.93 -0.090',
                    '0.0 0.08 10.0 10.0 0.840 0.0125 0.0105 0.0230 36.52 0.94 -0.105',
                    '0.0 0.08 12.0 10.0 0.910 0.0149 -0.0035 0.0114 79.82 -2.10 -0.120',
                ]
            )

            def fake_subprocess_run(cmd, cwd, **kwargs):
                Path(cwd, 'auav_case.vsp3').write_text('vsp3', encoding='utf-8')
                Path(cwd, 'auav_case.polar').write_text(polar, encoding='utf-8')
                return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

            with (
                patch(
                    'app.analysis.openvsp_adapter._resolve_solver_paths',
                    return_value={'bin_dir': solver_dir, 'vsp_exe': vsp_exe, 'vspaero_exe': vspaero_exe},
                ),
                patch('app.analysis.openvsp_adapter.subprocess.run', side_effect=fake_subprocess_run),
            ):
                result = run_precision_analysis(state, work_dir / 'real')

        self.assertEqual(result.analysis_mode, 'openvsp')
        self.assertEqual(result.extra_data['curve_source'], 'polar_filtered')
        self.assertGreater(result.extra_data['curve_filtering']['dropped_row_count'], 0)
        self.assertLess(result.metrics.ld_max, 100.0)
        self.assertGreater(result.metrics.cd_min, 0.005)
        self.assertEqual(result.curve.aoa_deg[-1], 10.0)
        self.assertEqual(result.extra_data['requested_aoa_range'], {'start': -2.0, 'end': 12.0})
        self.assertEqual(result.extra_data['valid_aoa_range'], {'start': -2.0, 'end': 10.0})
        self.assertIn('요청한 해석 범위는 -2.0°~12.0°입니다.', result.notes)
        self.assertIn('유효 구간만 결과에 반영했습니다.', result.notes)

    def test_openvsp_normalizes_inverted_polar_sign_and_keeps_requested_range_metadata(self) -> None:
        state = AppState(airfoil=AirfoilState.model_validate(generate_naca4('2412')))
        state.analysis.conditions.aoa_start = -4.0
        state.analysis.conditions.aoa_end = 4.0
        state.analysis.conditions.aoa_step = 2.0

        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            solver_dir = work_dir / 'solver_bin'
            solver_dir.mkdir(parents=True, exist_ok=True)
            vsp_exe = solver_dir / 'vsp.exe'
            vspaero_exe = solver_dir / 'vspaero.exe'
            vsp_exe.write_text('', encoding='utf-8')
            vspaero_exe.write_text('', encoding='utf-8')

            polar = '\n'.join(
                [
                    'Beta Mach AoA Re/1e6 CLtot CDo CDi CDtot L/D E CMytot',
                    '0.0 0.08 -4.0 10.0 0.360 0.0060 0.0020 0.0080 45.00 0.87 0.040',
                    '0.0 0.08 -2.0 10.0 0.190 0.0056 0.0014 0.0070 27.14 0.88 0.025',
                    '0.0 0.08 0.0 10.0 0.010 0.0055 0.0010 0.0065 1.54 0.89 0.010',
                    '0.0 0.08 2.0 10.0 -0.170 0.0058 0.0016 0.0074 -22.97 0.89 -0.015',
                    '0.0 0.08 4.0 10.0 -0.340 0.0064 0.0024 0.0088 -38.64 0.88 -0.032',
                ]
            )

            def fake_subprocess_run(cmd, cwd, **kwargs):
                Path(cwd, 'auav_case.vsp3').write_text('vsp3', encoding='utf-8')
                Path(cwd, 'auav_case.polar').write_text(polar, encoding='utf-8')
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with (
                patch(
                    'app.analysis.openvsp_adapter._resolve_solver_paths',
                    return_value={'bin_dir': solver_dir, 'vsp_exe': vsp_exe, 'vspaero_exe': vspaero_exe},
                ),
                patch('app.analysis.openvsp_adapter.subprocess.run', side_effect=fake_subprocess_run),
            ):
                result = run_precision_analysis(state, work_dir / 'real')

        self.assertEqual(result.analysis_mode, 'openvsp')
        self.assertEqual(result.extra_data['requested_aoa_range'], {'start': -4.0, 'end': 4.0})
        self.assertEqual(result.extra_data['valid_aoa_range'], {'start': -4.0, 'end': 4.0})
        self.assertGreater(result.metrics.cl_alpha, 0.0)
        self.assertLess(result.curve.cl[0], result.curve.cl[-1])
        self.assertAlmostEqual(result.curve.cl[0], -0.36, places=2)
        self.assertAlmostEqual(result.curve.cl[-1], 0.34, places=2)

    def test_polar_parser_retains_surface_and_wake_families_for_regression_fixture(self) -> None:
        parsed = _parse_polar_rows(FIXTURES / 'openvsp' / 'auav_case.polar')
        self.assertIsNotNone(parsed)
        headers, rows = parsed or ([], [])

        families = {item['id']: item for item in _extract_curve_families_from_polar(headers, rows)}

        self.assertIn('surface_integration', families)
        self.assertIn('wake_far_field', families)
        self.assertEqual(len(families['surface_integration']['curve_rows']), 31)
        self.assertEqual(len(families['wake_far_field']['curve_rows']), 31)

    def test_dynamic_family_selection_prefers_wake_family_for_regression_fixture(self) -> None:
        parsed = _parse_polar_rows(FIXTURES / 'openvsp' / 'auav_case.polar')
        self.assertIsNotNone(parsed)
        headers, rows = parsed or ([], [])

        payload = _finalize_polar_curve_payload(
            headers=headers,
            rows=rows,
            aoa_start=-10.0,
            aoa_end=20.0,
            aoa_step=1.0,
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload['selected_coefficient_family'], 'wake_far_field')
        self.assertEqual(payload['selected_coefficient_family_label'], 'wake/far-field')
        self.assertEqual(payload['coefficient_family_selection'], 'dynamic_family_selection')
        self.assertEqual(payload['filtering']['used_aoa_range'], {'start': -10.0, 'end': 20.0})
        self.assertEqual(payload['coefficient_family_candidates']['wake_far_field']['selected'], True)
        self.assertGreater(payload['coefficient_family_candidates']['surface_integration']['dropped_row_count'], 0)
        self.assertIn('nonpositive_cd', payload['coefficient_family_candidates']['surface_integration']['exclusion_reason_summary'])
        self.assertIn('cdwtot_min', payload['vspaero_all_data'])
        self.assertNotIn('cdtot_min', payload['vspaero_all_data'])

    def test_solver_effective_conditions_report_reynolds_mismatch_from_vspaero_fixture(self) -> None:
        effective = _extract_solver_effective_conditions(
            requested_conditions={'reynolds': 70000.0},
            vspaero_case_path=FIXTURES / 'openvsp' / 'auav_case.vspaero',
            scripted_re_cref=None,
            fallback_mach=0.03,
        )

        self.assertEqual(effective['re_cref'], 10000000.0)
        self.assertFalse(effective['reynolds_applied'])
        self.assertEqual(effective['aoa_range'], {'start': -10.0, 'end': 20.0})
        self.assertIn('ReCref는 10,000,000', effective['reynolds_note'])

    def test_run_precision_applies_requested_reynolds_and_uses_selected_wake_family(self) -> None:
        state = AppState(airfoil=AirfoilState.model_validate(generate_naca4('3408')))
        state.analysis.conditions.aoa_start = -10.0
        state.analysis.conditions.aoa_end = 20.0
        state.analysis.conditions.aoa_step = 1.0
        state.analysis.conditions.mach = 0.03
        state.analysis.conditions.reynolds = 70000.0

        fixture_polar = (FIXTURES / 'openvsp' / 'auav_case.polar').read_text(encoding='utf-8')
        fixture_vspaero = (FIXTURES / 'openvsp' / 'auav_case.vspaero').read_text(encoding='utf-8').replace(
            'ReCref = 10000000.000000',
            'ReCref = 70000.000000',
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            solver_dir = work_dir / 'solver_bin'
            solver_dir.mkdir(parents=True, exist_ok=True)
            vsp_exe = solver_dir / 'vsp.exe'
            vspaero_exe = solver_dir / 'vspaero.exe'
            vsp_exe.write_text('', encoding='utf-8')
            vspaero_exe.write_text('', encoding='utf-8')
            captured: dict[str, str] = {}

            def fake_subprocess_run(cmd, cwd, **kwargs):
                captured['script'] = Path(cwd, 'run_precision.vspscript').read_text(encoding='utf-8')
                Path(cwd, 'auav_case.vsp3').write_text('vsp3', encoding='utf-8')
                Path(cwd, 'auav_case.polar').write_text(fixture_polar, encoding='utf-8')
                Path(cwd, 'auav_case.vspaero').write_text(fixture_vspaero, encoding='utf-8')
                return SimpleNamespace(returncode=0, stdout='', stderr='')

            with (
                patch(
                    'app.analysis.openvsp_adapter._resolve_solver_paths',
                    return_value={'bin_dir': solver_dir, 'vsp_exe': vsp_exe, 'vspaero_exe': vspaero_exe},
                ),
                patch('app.analysis.openvsp_adapter.subprocess.run', side_effect=fake_subprocess_run),
            ):
                result = run_precision_analysis(state, work_dir / 'real')

        self.assertIn('SetDoubleAnalysisInput( analysis_name, "ReCref"', captured['script'])
        self.assertIn('70000.000000', captured['script'])
        self.assertEqual(result.extra_data['selected_coefficient_family'], 'wake_far_field')
        self.assertEqual(result.extra_data['valid_aoa_range'], {'start': -10.0, 'end': 20.0})
        self.assertEqual(result.extra_data['solver_effective_conditions']['re_cref'], 70000.0)
        self.assertTrue(result.extra_data['solver_effective_conditions']['reynolds_applied'])
        self.assertGreater(result.metrics.ld_max, 20.0)
        self.assertLess(result.metrics.cd_min, 0.02)


class NeuralFoilAnalysisTests(unittest.TestCase):
    def tearDown(self) -> None:
        _reset_native_runtime_for_tests()

    def test_neuralfoil_analysis_produces_solver_specific_result(self) -> None:
        state = AppState(airfoil=AirfoilState.model_validate(generate_naca4('2412')))

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = run_neuralfoil_analysis(state, Path(tmp_dir))
            inputs_path = Path(result.extra_data['inputs_path'])
            outputs_path = Path(result.extra_data['outputs_path'])
            processed_path = Path(result.extra_data['processed_result_path'])
            inputs_exists = inputs_path.exists()
            outputs_exists = outputs_path.exists()
            processed_exists = processed_path.exists()

        self.assertEqual(result.analysis_mode, 'neuralfoil')
        self.assertIsNone(result.fallback_reason)
        self.assertEqual(result.extra_data['solver_id'], 'neuralfoil')
        self.assertEqual(result.extra_data['result_level'], 'wing_estimate_from_2d_solver')
        self.assertTrue(inputs_exists)
        self.assertTrue(outputs_exists)
        self.assertTrue(processed_exists)
        self.assertTrue(result.curve.aoa_deg)
        self.assertTrue(result.curve.cl)

    def test_neuralfoil_and_openvsp_results_can_coexist_and_active_result_switches(self) -> None:
        state = AppState(airfoil=AirfoilState.model_validate(generate_naca4('2412')))

        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            solver_dir = work_dir / 'solver_bin'
            solver_dir.mkdir(parents=True, exist_ok=True)
            vsp_exe = solver_dir / 'vsp.exe'
            vspaero_exe = solver_dir / 'vspaero.exe'
            vsp_exe.write_text('', encoding='utf-8')
            vspaero_exe.write_text('', encoding='utf-8')

            stdout = '\n'.join(
                [
                    '1 0.0000 -2.0000 0.0800 0.0000 0.0000 -0.2000 0.0000 0.0000 0.0100 0.0000 0.0000 0.0000 -0.0200 0.8000',
                    '1 0.0000 0.0000 0.0800 0.0000 0.0000 0.0000 0.0000 0.0000 0.0090 0.0000 0.0000 0.0000 -0.0100 0.8200',
                    '1 0.0000 2.0000 0.0800 0.0000 0.0000 0.2000 0.0000 0.0000 0.0110 0.0000 0.0000 0.0000 0.0000 0.7800',
                ]
            )

            def fake_subprocess_run(cmd, cwd, **kwargs):
                Path(cwd, 'auav_case.vsp3').write_text('vsp3', encoding='utf-8')
                return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

            with (
                patch(
                    'app.analysis.openvsp_adapter._resolve_solver_paths',
                    return_value={'bin_dir': solver_dir, 'vsp_exe': vsp_exe, 'vspaero_exe': vspaero_exe},
                ),
                patch('app.analysis.openvsp_adapter.subprocess.run', side_effect=fake_subprocess_run),
            ):
                set_solver_result(state.analysis, 'openvsp', run_precision_analysis(state, work_dir / 'openvsp'))
                set_solver_result(state.analysis, 'neuralfoil', run_neuralfoil_analysis(state, work_dir / 'neuralfoil'))

        solver_id, active = get_active_result(state.analysis)
        self.assertEqual(solver_id, 'neuralfoil')
        self.assertIsNotNone(active)
        self.assertIsNotNone(state.analysis.results.openvsp)
        self.assertIsNotNone(state.analysis.results.neuralfoil)
        self.assertEqual(state.analysis.results.openvsp.extra_data['solver_id'], 'openvsp')
        self.assertEqual(state.analysis.results.neuralfoil.extra_data['solver_id'], 'neuralfoil')

    def test_prepare_native_runtime_dirs_registers_casadi_bundle_paths_for_frozen_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            bundle_dir = Path(tmp_dir)
            backend_exe = bundle_dir / 'backend.exe'
            internal_dir = bundle_dir / '_internal'
            casadi_dir = internal_dir / 'casadi'
            backend_exe.write_text('', encoding='utf-8')
            casadi_dir.mkdir(parents=True, exist_ok=True)

            added: list[str] = []

            def fake_add_dll_directory(path: str) -> SimpleNamespace:
                added.append(path)
                return SimpleNamespace(close=lambda: None)

            _reset_native_runtime_for_tests()
            with (
                patch('app.runtime.native.os.add_dll_directory', side_effect=fake_add_dll_directory),
                patch.object(sys, 'frozen', True, create=True),
                patch.object(sys, '_MEIPASS', str(internal_dir), create=True),
                patch.object(sys, 'executable', str(backend_exe)),
            ):
                prepared = prepare_native_runtime_dirs()

        internal_path = str(internal_dir.resolve())
        casadi_path = str(casadi_dir.resolve())
        self.assertIn(internal_path, prepared)
        self.assertIn(casadi_path, prepared)
        self.assertIn(internal_path, added)
        self.assertIn(casadi_path, added)


class SaveManagerTests(unittest.TestCase):
    def test_list_is_sorted_by_created_at_descending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            manager = SaveManager(work_dir)
            save_dir = work_dir / 'saves'

            newer = {
                'id': 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                'name': 'new',
                'created_at': '2026-03-09T09:00:00+00:00',
                'summary': {},
            }
            older = {
                'id': 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'name': 'old',
                'created_at': '2026-03-09T08:00:00+00:00',
                'summary': {},
            }

            (save_dir / f"{older['id']}.json").write_text(json.dumps(older), encoding='utf-8')
            (save_dir / f"{newer['id']}.json").write_text(json.dumps(newer), encoding='utf-8')

            rows = manager.list()

            self.assertEqual([row['id'] for row in rows], [newer['id'], older['id']])

    def test_record_access_rejects_corrupted_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            manager = SaveManager(work_dir)
            save_id = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
            save_path = work_dir / 'saves' / f'{save_id}.json'
            save_path.write_text('{"id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}', encoding='utf-8')

            with self.assertRaisesRegex(ValueError, 'Save is corrupted'):
                manager.get_record(save_id)

    def test_compare_detects_custom_airfoil_shape_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            manager = SaveManager(work_dir)

            left_state = AppState(airfoil=AirfoilState.model_validate(generate_custom_airfoil(3.0, 35.0, 12.0)))
            right_state = AppState(airfoil=AirfoilState.model_validate(generate_custom_airfoil(3.0, 35.0, 13.0)))

            left = manager.save(left_state, 'custom-12')
            right = manager.save(right_state, 'custom-13')
            comparison = manager.compare(left['id'], right['id'])

            changed = {diff['key']: diff for diff in comparison['diffs'] if diff['left'] != diff['right']}

            self.assertIn('airfoil_thickness_percent', changed)
            self.assertNotEqual(
                left['summary']['airfoil']['shape_signature'],
                right['summary']['airfoil']['shape_signature'],
            )

    def test_list_uses_cached_records_when_snapshot_files_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            manager = SaveManager(work_dir)
            manager.save(AppState(), 'cached')

            first_rows = manager.list()
            self.assertEqual(len(first_rows), 1)

            with patch.object(Path, 'read_text', side_effect=AssertionError('cached list should not reread snapshot files')):
                second_rows = manager.list()

            self.assertEqual(len(second_rows), 1)
            self.assertEqual(second_rows[0]['name'], 'cached')


class MetricFormulaTests(unittest.TestCase):
    def test_endurance_and_range_params_use_positive_lift_efficiency_scores(self) -> None:
        curve = AeroCurve(
            aoa_deg=[-2.0, 0.0, 2.0, 4.0],
            cl=[-0.1, 0.2, 0.6, 0.8],
            cd=[0.020, 0.012, 0.018, 0.030],
            cm=[0.0, -0.01, -0.02, -0.03],
        )

        metrics = derive_metrics(curve, reynolds=250000.0, oswald=0.82)

        self.assertAlmostEqual(metrics.ld_max, round(0.6 / 0.018, 6))
        self.assertAlmostEqual(metrics.endurance_param, round((0.6 ** 1.5) / 0.018, 6))
        self.assertAlmostEqual(metrics.range_param, round((0.6 ** 0.5) / 0.018, 6))
        self.assertLess(metrics.endurance_param, 100.0)


class GeometryConsistencyTests(unittest.TestCase):
    def test_pressure_overlay_matches_vertex_order(self) -> None:
        airfoil = AirfoilState(
            upper=[[0.0, 0.0], [0.25, 0.04], [0.5, 0.05], [0.75, 0.03], [1.0, 0.0]],
            lower=[[0.0, 0.0], [0.25, -0.02], [0.5, -0.03], [0.75, -0.01], [1.0, 0.0]],
            summary=AirfoilSummary(code='Test'),
        )
        params = WingParams()

        mesh, _ = build_wing_mesh(airfoil, params)

        self.assertEqual(len(mesh.vertices), len(mesh.pressure_overlay))
        first_vertex = mesh.vertices[0]
        expected_pressure = round(_mock_pressure(first_vertex[0], first_vertex[1], first_vertex[2], params.span_m), 6)
        self.assertEqual(mesh.pressure_overlay[0], expected_pressure)

    def test_mesh_uses_single_shared_root_ring_without_centerline_cap(self) -> None:
        airfoil = AirfoilState.model_validate(generate_naca4('2412'))
        mesh, _ = build_wing_mesh(airfoil, WingParams())

        root_indices = [idx for idx, vertex in enumerate(mesh.vertices) if abs(vertex[1]) < 1e-9]
        expected_root_points = len(airfoil.upper) + len(airfoil.lower) - 1

        self.assertEqual(len(root_indices), expected_root_points)
        self.assertFalse(
            any(all(abs(mesh.vertices[idx][1]) < 1e-9 for idx in tri) for tri in mesh.triangles),
            'centerline root cap triangles should not exist',
        )

    def test_wingtip_style_changes_preview_topology(self) -> None:
        airfoil = AirfoilState.model_validate(generate_naca4('2412'))

        straight_mesh, straight_planform = build_wing_mesh(airfoil, WingParams(wingtip_style='straight'))
        pinched_mesh, pinched_planform = build_wing_mesh(airfoil, WingParams(wingtip_style='pinched'))

        self.assertLess(len(straight_mesh.vertices), len(pinched_mesh.vertices))
        self.assertLess(len(straight_planform.polygon), len(pinched_planform.polygon))
        self.assertEqual(straight_planform.polygon[2][1], 0.5)
        self.assertEqual(pinched_planform.polygon[2][1], 0.44)


class ExportPathTests(unittest.TestCase):
    def test_build_export_path_stays_inside_exports_dir_for_all_supported_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            export_dir = (work_dir / 'exports').resolve()

            cases = [
                (None, '.obj'),
                ('json', '.json'),
                ('vsp3', '.vsp3'),
            ]

            for requested_format, suffix in cases:
                target = _build_export_path(work_dir, requested_format).resolve()
                self.assertTrue(target.is_relative_to(export_dir))
                self.assertEqual(target.suffix, suffix)


if __name__ == '__main__':
    unittest.main()
