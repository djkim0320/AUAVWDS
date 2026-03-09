from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api import _build_export_path, create_app
from app.geometry.wing_builder import _mock_pressure, build_wing_mesh
from app.models.state import AirfoilState, AirfoilSummary, WingParams
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
        self.assertIn('Unsupported payload keys for RunPrecisionAnalysis', res.json()['detail'])

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

        res = self.client.post('/export/cfd', json={'output_path': '..\\outside.json'})

        self.assertEqual(res.status_code, 200)
        payload = res.json()
        exported = Path(payload['path']).resolve()
        export_dir = (self.work_dir / 'exports').resolve()

        self.assertTrue(exported.is_relative_to(export_dir))
        self.assertEqual(exported.suffix, '.json')
        self.assertTrue(exported.exists())

    def _prepare_mesh(self) -> None:
        set_airfoil = self.client.post('/command', json={'command': {'type': 'SetAirfoil', 'payload': {'code': '2412'}}})
        self.assertEqual(set_airfoil.status_code, 200)
        build_mesh = self.client.post('/command', json={'command': {'type': 'BuildWingMesh', 'payload': {}}})
        self.assertEqual(build_mesh.status_code, 200)


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


class ExportPathTests(unittest.TestCase):
    def test_build_export_path_stays_inside_exports_dir_for_all_supported_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work_dir = Path(tmp_dir)
            export_dir = (work_dir / 'exports').resolve()

            for requested, suffix in ((None, '.obj'), ('ignored.json', '.json'), ('C:\\temp\\ignored.vsp3', '.vsp3')):
                target = _build_export_path(work_dir, requested).resolve()
                self.assertTrue(target.is_relative_to(export_dir))
                self.assertEqual(target.suffix, suffix)


if __name__ == '__main__':
    unittest.main()
