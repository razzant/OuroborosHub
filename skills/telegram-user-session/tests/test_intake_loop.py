"""Actual companion orchestration with a held Host turn; no Telegram/network effects."""
import asyncio
import contextlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from userbot import participant as module
from userbot.config import Config, save_config


class IntakeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def check_loop(self, fail=False, hold_media=False, hold_place=False, hold_observe=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_config(root, Config(123, 'a' * 32, '+1234567890'))
            started, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()
            place_started = asyncio.Event()
            state = {'calls': 0, 'reports': 0, 'job': False, 'finished': False,
                     'disconnected': False}

            class Host:
                def __init__(self, *args): pass
                def identity(self): pass

            class Client:
                def add_event_handler(self, *args): pass
                async def catch_up(self): pass

            class Adapter:
                client = Client()
                async def connect(self): pass
                async def get_me(self): return 123
                async def disconnect(self):
                    state['disconnected'] = True
                    state['live_at_disconnect'] = [t.get_name() for t in asyncio.all_tasks()
                                                   if not t.done() and t.get_name().startswith('telegram-')]

            class Engine:
                next_send = 0
                def __init__(self, *args): pass
                def current(self): pass
                async def hydrate_forever(self):
                    if hold_media:
                        state['hydrations']=state.get('hydrations',0)+1
                        started.set()
                        try: await release.wait()
                        finally: state['hydration_finished']=True
                async def stop_hydration(self): state['hydration_stopped']=True
                async def refresh_chats(self): return False
                async def observe_place(self):
                    if not hold_place:
                        return
                    # A display-only Host place read that has not answered yet.
                    state['place_reads'] = state.get('place_reads', 0) + 1
                    place_started.set()
                    try: await release.wait()
                    finally: state['place_finished'] = True
                async def intake_forever(self):
                    state['calls'] += 1
                    started.set()
                    try:
                        if fail: raise RuntimeError('held intake failed')
                        await release.wait()
                    finally:
                        state['finished'] = True
                async def observe_once(self):
                    # Reading custody references runs as its own worker beside intake.
                    state['observations'] = state.get('observations', 0) + 1
                    if hold_observe:
                        try: await release.wait()
                        finally: state['observe_finished'] = True
                async def stop_intake(self): state['intake_stopped'] = True
                async def dispatch_one(self):
                    if state['job']: sent.set()
                async def reports_once(self): state['reports'] += 1

            async def login(*args): return True
            original_sleep = asyncio.sleep
            async def next_tick(_seconds): await original_sleep(0)
            with patch.object(module, 'Host', Host), patch.object(module, 'Participant', Engine), \
                 patch.object(module.asyncio, 'sleep', next_tick):
                task = asyncio.create_task(module.serve(root, 'http://fixture', None,
                    lambda *args: Adapter(), login, lambda root, cfg, account: cfg,
                    lambda *args: None))
                try:
                    await asyncio.wait_for(started.wait(), 3)
                    if hold_place:
                        await asyncio.wait_for(place_started.wait(), 3)
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, 'held intake failed'):
                            await asyncio.wait_for(task, 3)
                    else:
                        state['job'] = True
                        await asyncio.wait_for(sent.wait(), 3)
                        self.assertFalse(release.is_set(), 'tool progressed before Host replied')
                        self.assertEqual(state['calls'], 1, 'no duplicate concurrent intake')
                        self.assertGreater(state['reports'], 0)
                        if hold_place:
                            self.assertEqual(state['place_reads'], 1, 'one display read in flight, never stacked')
                            self.assertNotIn('place_finished', state)
                        if hold_observe:
                            self.assertEqual(state['observations'], 1, 'one observation pass in flight, never stacked')
                        else:
                            self.assertGreater(state['observations'], 1, 'observation keeps its cadence while intake is held')
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                        await task
                self.assertTrue(state['finished'], 'intake cancelled/joined before shutdown')
                self.assertTrue(state['disconnected'])
                self.assertEqual(state['live_at_disconnect'], [], 'every worker joined before disconnect')
                if hold_media:
                    self.assertEqual(state['hydrations'],1)
                    self.assertTrue(state['hydration_finished'])
                if hold_place:
                    self.assertTrue(state['place_finished'], 'display read cancelled/joined before shutdown')
                self.assertTrue(state.get('hydration_stopped'))
                self.assertTrue(state.get('intake_stopped'))
                if hold_observe:
                    self.assertTrue(state['observe_finished'], 'observation cancelled/joined before shutdown')
                self.assertFalse([t for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and t.get_name().startswith(('telegram-host-intake','telegram-host-observe','telegram-media-hydration','telegram-chat-list','telegram-place-display','telegram-history-import'))])

    async def test_tool_and_reports_progress_while_actual_serve_waits_on_host(self):
        await self.check_loop()

    async def test_outbound_progresses_while_media_hydration_is_held(self):
        await self.check_loop(hold_media=True)

    async def test_outbound_progresses_while_display_place_read_is_held(self):
        await self.check_loop(hold_place=True)

    async def test_outbound_and_intake_progress_while_an_observation_pass_is_held(self):
        await self.check_loop(hold_observe=True)

    async def test_intake_failure_propagates_and_closes_companion(self):
        await self.check_loop(fail=True)


if __name__ == '__main__': unittest.main()
