"""Host-owned gateway/tunnel lifetime; one generation, one bounded startup attempt.

The host restarts at most the manifest budget. A live failed tunnel waits for the
owner's Retry rather than silently rotating the installed phone's hostname.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import time
from aiohttp import ClientError, ClientTimeout

from server import Gateway
from cloudflare_tunnel import QuickTunnel, ensure_cloudflared
from platform_support import (acquire_file_lock, release_file_lock,
                              require_isolated_process_group, start_parent_lifeline)


def write_status(state, data):
    target = state / 'companion-status.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(data), encoding='utf-8')
    os.replace(temp, target)


async def run():
    raw = os.environ.get('OUROBOROS_SKILL_STATE_DIR')
    if not raw:
        raise RuntimeError('Host skill state is missing; enable this skill through Ouroboros.')
    state = Path(raw).resolve()
    state.mkdir(parents=True, exist_ok=True)
    require_isolated_process_group()
    lock = acquire_file_lock(state / 'companion.lock', timeout_sec=0)
    stop, retry = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    parent = int(os.environ.get('OUROBOROS_PARENT_PID') or os.getppid())
    cancelled, thread = start_parent_lifeline(parent, loop, stop)
    gateway = tunnel = None
    status = {'phase': 'starting', 'message': 'Checking the local interface.', 'core_available': None,
              'tunnel_available': None, 'checked_at': None}
    def update(**values):
        status.update(values)
        write_status(state, status)
    async def bounded_or_stop(coro):
        job = asyncio.create_task(coro)
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait((job, stopped), return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                job.cancel()
                # Downloads are delegated to a bounded thread. Keep ownership
                # until it has actually joined, rather than claim it stopped.
                with contextlib.suppress(asyncio.CancelledError):
                    await job
                raise asyncio.CancelledError()
            return await job
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
    failed = False
    stage = 'Local interface'
    try:
        update()
        config = json.loads((state / 'runtime.json').read_text('utf-8'))
        # Optional local migration ports keep an existing trial listener address;
        # packaged defaults use ephemeral ports, with no author-machine paths.
        migration = state / 'listener-ports.json'
        ports = json.loads(migration.read_text('utf-8')) if migration.exists() else {}
        gateway = Gateway(state / 'gateway', config['upstream'], ports.get('public', 0), ports.get('admin', 0))
        gateway.runtime_status = lambda: dict(status)
        gateway.retry_tunnel = retry.set
        await gateway.start()
        update(core_available=True, phase='preparing', message='Preparing the verified tunnel runtime.')
        # No host token or provider secret is passed to this downloader/child.
        stage = 'Verified tunnel runtime preparation'
        binary = await ensure_cloudflared(state)
        while not stop.is_set():
            retry.clear()
            tunnel = QuickTunnel(binary, state, gateway.public_port)
            try:
                update(phase='connecting', message='Opening a private tunnel.', tunnel_available=False)
                await tunnel.start()
                origin = (await bounded_or_stop(tunnel.wait_url(45))).rstrip('/')
                gateway.origin = origin
                gateway.store.pending.clear()
                gateway.current_pair = None
                update(phase='connecting', message='Checking the public address.', tunnel_available=None)
                while not stop.is_set() and not retry.is_set() and tunnel.returncode is None:
                    available = False
                    try:
                        async with gateway.session.get(gateway.upstream + '/api/health',
                                                       timeout=ClientTimeout(total=4)) as r:
                            available = r.status == 200
                    except (ClientError, asyncio.TimeoutError):
                        pass
                    reachable = False
                    try:
                        async with gateway.session.get(origin + '/_phone/health',
                                                       timeout=ClientTimeout(total=4), allow_redirects=False) as r:
                            reachable = r.status == 200 and (await r.json()).get('phone_gateway') == gateway.gateway_id
                    except (ClientError, asyncio.TimeoutError, ValueError):
                        pass
                    update(core_available=available, tunnel_available=reachable, checked_at=time.time(),
                           phase='ready' if reachable else 'unavailable',
                           message=('Create a QR to connect your phone.' if available else 'Ouroboros is unavailable. Wait for the PC, then refresh.')
                           if reachable else 'Public address not reachable yet. Check internet access or Retry the tunnel.')
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(stop.wait(), 5)
                if stop.is_set():
                    break
                update(phase='unavailable', tunnel_available=False,
                       message='Tunnel stopped. Retry creates a new address; reconnect and reinstall the phone icon.')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                update(phase='unavailable', tunnel_available=False,
                       message='Tunnel unavailable (' + type(exc).__name__ + '). Check internet access, then Retry.')
            finally:
                await tunnel.stop()
                # Withdraw old address, pending invitations and all in-flight
                # streams before a new tunnel generation can be started.
                gateway.origin = ''
                gateway.current_pair = None
                gateway.store.pending.clear()
                for task in list(gateway.inflight):
                    task.cancel()
                await asyncio.gather(*list(gateway.inflight), return_exceptions=True)
            if not stop.is_set() and not retry.is_set():
                a, b = asyncio.create_task(stop.wait()), asyncio.create_task(retry.wait())
                try:
                    await asyncio.wait((a, b), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    a.cancel(); b.cancel()
                    await asyncio.gather(a, b, return_exceptions=True)
    except Exception as exc:
        failed = True
        update(phase='failed', core_available=None, tunnel_available=False, checked_at=time.time(),
               message=stage + ' failed (' + type(exc).__name__ + '). '
                       + ('Check the selected UI compatibility and local Ouroboros availability. '
                          if stage == 'Local interface' else 'Check internet access and supported architecture; the pinned download must verify. ')
                       + 'Turn the skill off and on to retry.')
        raise
    finally:
        try:
            if tunnel:
                await tunnel.stop()
            if gateway:
                await gateway.stop()
            if not failed:
                update(phase='stopped', tunnel_available=False, checked_at=time.time(), message='Phone access stopped. Enable the skill to reconnect.')
        finally:
            cancelled.set()
            thread.join(timeout=1)
            release_file_lock(lock)


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
