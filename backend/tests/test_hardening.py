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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.analysis.naca import generate_custom_airfoil, generate_naca4
from app.analysis.neuralfoil_adapter import run_neuralfoil_analysis
from app.analysis.openvsp_adapter import run_precision_analysis
from app.api import _build_export_path, create_app
from app.geometry.wing_builder import _mock_pressure, build_wing_mesh
from app.models.state import AirfoilState, AirfoilSummary, AppState, WingParams, get_active_result, set_solver_result
from app.services.state_store import SaveManager


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

    def test_export_ignores_external_path_and_writes_inside_exports_dir(self) -> None:
        self._prepare_mesh()

        res = self.client.post('/export/cfd', json={'format': 'json', 'output_path': '..\\outside.obj'})

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

    def _prepare_mesh(self) -> None:
        set_airfoil = self.client.post('/command', json={'command': {'type': 'SetAirfoil', 'payload': {'code': '2412'}}})
        self.assertEqual(set_airfoil.status_code, 200)
        build_mesh = self.client.post('/command', json={'command': {'type': 'BuildWingMesh', 'payload': {}}})
        self.assertEqual(build_mesh.status_code, 200)


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


class NeuralFoilAnalysisTests(unittest.TestCase):
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


class ExportPathTests(unittest.TestCase):
    def test_build_export_path_stays_inside_exports_dir_for_all_supported_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            export_dir = (work_dir / 'exports').resolve()

            cases = [
                (None, None, '.obj'),
                ('ignored.json', None, '.json'),
                ('C:\\temp\\ignored.vsp3', None, '.vsp3'),
                (None, 'json', '.json'),
                ('ignored.obj', 'vsp3', '.vsp3'),
            ]

            for requested, requested_format, suffix in cases:
                target = _build_export_path(work_dir, requested, requested_format).resolve()
                self.assertTrue(target.is_relative_to(export_dir))
                self.assertEqual(target.suffix, suffix)


if __name__ == '__main__':
    unittest.main()
