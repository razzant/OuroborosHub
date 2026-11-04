"""Actual route handler fallback; run with the host's Starlette available."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from plugin import register


class PluginTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_companion_status_remains_dated_not_live(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            routes = {}
            api = SimpleNamespace(get_runtime_info=lambda: {'state_dir': raw, 'server_port': 12345},
                                  register_route=lambda name, handler, methods: routes.update({name: handler}),
                                  register_ui_tab=lambda *a, **kw: None,
                                  register_companion_process=lambda *a, **kw: None)
            register(api)
            saved = {'phase': 'failed', 'checked_at': 123, 'message': 'Local interface failed. Turn the skill off and on to retry.'}
            (state/'companion-status.json').write_text(json.dumps(saved))
            response = await routes['status'](SimpleNamespace(method='GET'))
            body = json.loads(response.body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(body['phase'], 'failed')
            self.assertTrue(body['observation_only'])
            self.assertIsNone(body['core_available'])
            self.assertEqual(body['message'], saved['message'])
            saved['phase'] = 'ready'
            (state/'companion-status.json').write_text(json.dumps(saved))
            body = json.loads((await routes['status'](SimpleNamespace(method='GET'))).body)
            self.assertEqual(body['phase'], 'unconfirmed')
            self.assertEqual(body['origin'], '')
            self.assertIsNone(body['pair'])


if __name__ == '__main__': unittest.main()
