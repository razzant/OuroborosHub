"""Thin host routes: the companion alone owns pairing, devices and tunnel state."""
import json
import os
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar
from starlette.responses import JSONResponse


def diagnostic_status(state):
    """A saved failure is a dated observation, never live readiness."""
    try:
        saved = json.loads((Path(state) / 'companion-status.json').read_text('utf-8'))
        phase = saved.get('phase')
        return {'phase': phase if phase in ('failed', 'stopped') else 'unconfirmed',
                'message': saved.get('message', 'Phone companion is unavailable.') if phase in ('failed', 'stopped')
                           else 'Phone companion availability is unconfirmed. Check the skill, then refresh.',
                'checked_at': saved.get('checked_at'), 'observation_only': True,
                'core_available': None, 'tunnel_available': None,
                'origin': '', 'pair': None, 'devices': []}
    except (OSError, ValueError, TypeError):
        return None


def register(api):
    info = api.get_runtime_info()
    state = Path(info['state_dir'])
    state.mkdir(parents=True, exist_ok=True)
    config = {'upstream': 'http://127.0.0.1:' + str(info['server_port'])}
    config_path = state / 'runtime.json'
    current = json.loads(config_path.read_text('utf-8')) if config_path.exists() else None
    if current != config:
        temp = config_path.with_suffix('.tmp-' + str(os.getpid()))
        temp.write_text(json.dumps(config), encoding='utf-8')
        os.replace(temp, config_path)

    def handler(operation):
        async def call(request):
            try:
                launch = json.loads((state / 'gateway' / 'launch.json').read_text('utf-8'))
                # Private bearer never enters a browser response or log.
                from urllib.parse import urlsplit
                parsed = urlsplit(launch['admin_url'])
                port = int(launch['admin_port'])
                if parsed.scheme != 'http' or parsed.netloc != '127.0.0.1:' + str(port) or not parsed.fragment:
                    raise ValueError('Invalid local gateway receipt')
                headers = {'Authorization': 'Bearer ' + parsed.fragment}
                payload = await request.json() if request.method == 'POST' else None
                if payload is not None and not isinstance(payload, dict):
                    return JSONResponse({'error': 'A JSON object is required'}, status_code=400)
                async with ClientSession(cookie_jar=DummyCookieJar(), trust_env=False,
                                         timeout=ClientTimeout(total=8)) as session:
                    async with session.request(request.method, f'http://127.0.0.1:{port}/' + operation,
                                               headers=headers, json=payload, allow_redirects=False) as response:
                        data = await response.json()
                        return JSONResponse(data, status_code=response.status,
                                            headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
            except Exception as exc:
                # Do not forward exception strings containing local URLs/capabilities.
                if operation == 'status':
                    saved = diagnostic_status(state)
                    if saved:
                        return JSONResponse(saved, headers={'Cache-Control': 'no-store'})
                return JSONResponse({'error': 'Local gateway request failed (' + type(exc).__name__ + '). Check the skill, then refresh.'}, status_code=503)
        return call

    for operation in ('status', 'pair', 'revoke-pair', 'revoke', 'retry'):
        api.register_route(operation, handler=handler(operation), methods=('GET',) if operation == 'status' else ('POST',))
    api.register_ui_tab('connect', 'Phone access', icon='▣', render={
        'kind': 'module', 'entry': 'widget.js', 'start': 'auto',
        'appearance': 'host', 'height': 680, 'span': 2,
    })
    api.register_companion_process('phone_gateway')
