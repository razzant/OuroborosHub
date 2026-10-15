"""Offline tests for private owner config and single-use login handoff."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace

from userbot.config import (
    CHALLENGE_NAME, CONFIG_NAME, CONFIG_LOCK_NAME, HANDOFF_NAME, Binding, Config, ConfigError,
    clear_login_challenge, consume_login_secret, load_config,
    load_login_challenge, save_config, save_login_challenge, stage_login_secret,
    update_config,
)


API_HASH = "a" * 32
BINDING = "b" * 32
SECOND_BINDING = "c" * 32
THIRD_BINDING = "d" * 32


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "private-state"
        self.config = Config(123456, API_HASH, "+15551234567", BINDING,
                             (-1001234567890, 54321), 998877)

    def test_unconfigured_then_round_trip_and_no_secret_repr(self):
        self.assertIsNone(load_config(self.root))
        save_config(self.root, self.config)
        self.assertEqual(load_config(self.root), self.config)
        self.assertNotIn(API_HASH, repr(self.config))
        self.assertNotIn("+15551234567", repr(self.config))
        raw = json.loads((self.root / CONFIG_NAME).read_text(encoding="utf-8"))
        self.assertEqual(raw["allowed_peer_ids"], [-1001234567890, 54321])
        self.assertEqual(raw["account_id"], 998877)
        self.assertEqual(raw["binding_id"], BINDING)

    def test_initial_account_and_binding_may_be_absent(self):
        initial = Config(123456, API_HASH, "+15551234567")
        save_config(self.root, initial)
        self.assertEqual(load_config(self.root), initial)
        self.assertEqual(load_config(self.root).allowed_peer_ids, ())
        self.assertIsNone(load_config(self.root).account_id)

    def test_exact_bindings_two_peers_and_two_forum_topics(self):
        peer = -1001234567890
        second_peer = 54321
        config = Config(123456, API_HASH, "+15551234567", BINDING,
                        (peer, second_peer), 998877, (
                            Binding(peer, None, BINDING),
                            Binding(peer, 17, SECOND_BINDING),
                            Binding(peer, 24, THIRD_BINDING),
                            Binding(second_peer, None, SECOND_BINDING),
                        ))
        save_config(self.root, config)
        loaded = load_config(self.root)
        self.assertEqual(loaded, config)
        self.assertEqual(loaded.binding_for(peer, None), BINDING)
        self.assertEqual(loaded.binding_for(peer, 17), SECOND_BINDING)
        self.assertEqual(loaded.binding_for(peer, 24), THIRD_BINDING)
        self.assertEqual(loaded.binding_for(second_peer, None), SECOND_BINDING)
        for peer_id, topic_id in ((peer, 18), (second_peer, 17), (987654, None)):
            with self.subTest(peer_id=peer_id, topic_id=topic_id):
                self.assertIsNone(loaded.binding_for(peer_id, topic_id))
        raw = json.loads((self.root / CONFIG_NAME).read_text(encoding="utf-8"))
        self.assertEqual([(b["peer_id"], b["topic_id"]) for b in raw["bindings"]],
                         [(peer, None), (peer, 17), (peer, 24), (second_peer, None)])

    def test_legacy_one_binding_only_one_peer_general_and_old_json(self):
        old = Config(123456, API_HASH, "+15551234567", BINDING, (42,), 7)
        save_config(self.root, old)
        path = self.root / CONFIG_NAME
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.pop("bindings")  # pre-migration schema-1 remains loadable
        path.write_text(json.dumps(raw), encoding="utf-8")
        loaded = load_config(self.root)
        self.assertEqual(loaded, old)
        self.assertEqual(loaded.binding_for(42, None), BINDING)
        self.assertIsNone(loaded.binding_for(42, 15))
        self.assertIsNone(loaded.binding_for(43, None))
        ambiguous = Config(123456, API_HASH, "+15551234567", BINDING, (42, 43))
        self.assertIsNone(ambiguous.binding_for(42, None))
        mapped = Config(123456, API_HASH, "+15551234567", BINDING, (42,),
                        bindings=(Binding(42, 15, SECOND_BINDING),))
        self.assertIsNone(mapped.binding_for(42, None))  # no legacy fallback

    def test_bindings_reject_duplicates_unsorted_and_outside_allowlist(self):
        base = dict(api_id=123456, api_hash=API_HASH, phone="+15551234567",
                    binding_id=BINDING, allowed_peer_ids=(42, 43))
        cases = [
            (Binding(42, None, BINDING), Binding(42, None, SECOND_BINDING)),
            (Binding(43, 19, BINDING), Binding(42, 18, SECOND_BINDING)),
            (Binding(42, 18, BINDING), Binding(42, None, SECOND_BINDING)),
            (Binding(99, None, BINDING),),
        ]
        for bindings in cases:
            with self.subTest(bindings=bindings), self.assertRaises(ConfigError):
                Config(**base, bindings=bindings)
        for invalid in (0, True, -1, 1, (1 << 31), "18"):
            with self.subTest(topic_id=invalid), self.assertRaises(ConfigError):
                Binding(42, invalid, BINDING)
        for invalid in (0, True, "42", (1 << 63)):
            with self.subTest(peer_id=invalid), self.assertRaises(ConfigError):
                Binding(invalid, None, BINDING)
        with self.assertRaises(ConfigError):
            Binding(42, None, "*")
        with self.assertRaises(ConfigError):
            Config(**base, bindings=[Binding(42, None, BINDING)])
        with self.assertRaises(ConfigError):
            Config(**base, bindings=(dict(peer_id=42, topic_id=None, binding_id=BINDING),))
        valid = Config(**base, bindings=(Binding(42, 18, BINDING),))
        for peer_id, topic_id in ((True, None), (42, True), (42, 1)):
            with self.subTest(peer_id=peer_id, topic_id=topic_id), self.assertRaises(ConfigError):
                valid.binding_for(peer_id, topic_id)

    def test_binding_json_rejects_unknown_keys_and_noncanonical_order(self):
        config = Config(123456, API_HASH, "+15551234567", BINDING, (42,),
                        bindings=(Binding(42, None, BINDING), Binding(42, 17, SECOND_BINDING)))
        save_config(self.root, config)
        path = self.root / CONFIG_NAME
        original = json.loads(path.read_text(encoding="utf-8"))
        invalid = [
            original | {"bindings": None},
            original | {"bindings": [original["bindings"][1], original["bindings"][0]]},
            original | {"bindings": [original["bindings"][0]] * 2},
            original | {"bindings": [original["bindings"][0] | {"extra": 1}]},
            original | {"bindings": [{"peer_id": 42, "topic_id": None}]},
            original | {"bindings": [{"peer_id": 43, "topic_id": None, "binding_id": BINDING}]},
        ]
        for value in invalid:
            with self.subTest(value=value["bindings"]), self.assertRaises(ConfigError):
                path.write_text(json.dumps(value), encoding="utf-8")
                load_config(self.root)

    def test_strict_validation_rejects_ambiguous_identifiers(self):
        cases = [
            {"api_id": True}, {"api_id": -1}, {"api_hash": "zz" * 16},
            {"phone": "15551234567"}, {"binding_id": "not-a-binding"},
            {"allowed_peer_ids": (0,)}, {"allowed_peer_ids": (True,)},
            {"allowed_peer_ids": (12, 12)}, {"account_id": "42"},
        ]
        base = dict(api_id=123456, api_hash=API_HASH, phone="+15551234567",
                    binding_id=BINDING, allowed_peer_ids=(), account_id=None)
        for replacement in cases:
            with self.subTest(replacement=replacement), self.assertRaises(ConfigError):
                Config(**(base | replacement))

    def test_strict_json_duplicate_extra_nan_wrong_type_and_trailing(self):
        save_config(self.root, self.config)
        path = self.root / CONFIG_NAME
        original = path.read_text(encoding="utf-8")
        invalid = [original.replace('"schema":1', '"schema":1,"schema":1'),
                   original.rstrip() + " garbage", original.replace('"api_id":123456', '"api_id":NaN'),
                   original.replace('"allowed_peer_ids":[', '"unexpected":1,"allowed_peer_ids":['),
                   original.replace('"api_id":123456', '"api_id":true'),
                   '[]', '{']
        for content in invalid:
            with self.subTest(content=content[:35]):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(self.root)

    def test_no_secret_echo_in_validation_or_json_errors(self):
        value = "secret-sentinel-will-not-echo"
        with self.assertRaises(ConfigError) as caught:
            Config(100, value, "+15551234567")
        self.assertNotIn(value, str(caught.exception))
        save_config(self.root, self.config)
        path = self.root / CONFIG_NAME
        path.write_text('{"api_hash":"' + value + '",', encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            load_config(self.root)
        self.assertNotIn(value, str(caught.exception))

    def test_symlink_is_never_read_or_replaced(self):
        save_config(self.root, self.config)
        other = self.root / "outside"
        other.write_text("untouched", encoding="utf-8")
        path = self.root / CONFIG_NAME
        path.unlink()
        try:
            path.symlink_to(other)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable in this environment")
        with self.assertRaises(ConfigError):
            load_config(self.root)
        with self.assertRaises(ConfigError):
            save_config(self.root, self.config)
        self.assertEqual(other.read_text(encoding="utf-8"), "untouched")

    def test_atomic_replacement_leaves_no_temp_files(self):
        save_config(self.root, self.config)
        updated = Config(123456, API_HASH, "+15551234567", BINDING, (999,), None)
        save_config(self.root, updated)
        self.assertEqual(load_config(self.root), updated)
        self.assertEqual({p.name for p in self.root.iterdir()}, {CONFIG_NAME, CONFIG_LOCK_NAME})

    def test_cross_process_update_waits_for_lock_and_merges_current_revocation(self):
        save_config(self.root, self.config)
        script = ("import sys, time\nfrom dataclasses import replace\n"
                  "from userbot.config import update_config\n"
                  "def transform(current):\n"
                  " print('LOCKED', flush=True)\n time.sleep(0.5)\n"
                  " return replace(current, allowed_peer_ids=(), bindings=())\n"
                  "update_config(sys.argv[1], transform)")
        # A real second interpreter proves the file-lock boundary, not merely
        # the order of Python threads or a shared in-memory mutex.
        proc = subprocess.Popen([sys.executable, "-c", script, str(self.root)],
                                cwd=Path(__file__).resolve().parents[2] / "skills" / "telegram-user-session",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "LOCKED")
            merged = update_config(self.root, lambda current: replace(current, account_id=77))
            self.assertEqual(merged.allowed_peer_ids, ())
            self.assertEqual(merged.account_id, 77)
            _, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertEqual(load_config(self.root), merged)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_lock_timeout_never_calls_transform_or_writes_config(self):
        save_config(self.root, self.config)
        entered = threading.Event()
        release = threading.Event()
        failures = []

        def hold():
            try:
                update_config(self.root, lambda current: (entered.set(), release.wait(5), current)[2])
            except Exception as error:
                failures.append(error)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(entered.wait(5))
            before = (self.root / CONFIG_NAME).read_bytes()
            invoked = []
            with self.assertRaises(TimeoutError):
                update_config(self.root, lambda current: (invoked.append(True), current)[1],
                              timeout_sec=0.05)
            self.assertEqual(invoked, [])
            self.assertEqual((self.root / CONFIG_NAME).read_bytes(), before)
        finally:
            release.set()
            holder.join(5)
        self.assertFalse(holder.is_alive())
        self.assertEqual(failures, [])

    def test_lock_path_cannot_be_symlink_or_exposed_file(self):
        self.root.mkdir()
        outside = Path(self.temp.name) / "outside-lock"
        outside.write_bytes(b"must-survive")
        try:
            (self.root / CONFIG_LOCK_NAME).symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ConfigError):
            update_config(self.root, lambda _: self.config)
        self.assertFalse((self.root / CONFIG_NAME).exists())
        self.assertEqual(outside.read_bytes(), b"must-survive")

    def test_invalid_or_nonfinite_lock_timeout_rejected(self):
        for timeout in (True, -1, 61, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ConfigError):
                update_config(self.root, lambda _: self.config, timeout_sec=timeout)
        self.assertFalse((self.root / CONFIG_NAME).exists())

    def test_session_and_sqlite_sidecars_freeze_credentials_until_offline_reset(self):
        for filename in ("mtproto.session", "mtproto.session-journal",
                         "mtproto.session-wal", "mtproto.session-shm"):
            with self.subTest(filename=filename):
                save_config(self.root, self.config)
                session = self.root / filename
                session.write_bytes(b"existing-session-must-survive")
                changes = (
                    Config(111111, API_HASH, self.config.phone),
                    Config(self.config.api_id, "c" * 32, self.config.phone),
                    Config(self.config.api_id, API_HASH, "+15550001111"),
                )
                for change in changes:
                    with self.assertRaisesRegex(ConfigError, "offline reset"):
                        save_config(self.root, change)
                self.assertEqual(session.read_bytes(), b"existing-session-must-survive")
                self.assertEqual(load_config(self.root), self.config)
                permitted = Config(self.config.api_id, API_HASH, self.config.phone,
                                   BINDING, (111,), self.config.account_id)
                save_config(self.root, permitted)
                self.assertEqual(load_config(self.root), permitted)
                session.unlink()  # test stands in for companion-stopped controlled reset
                save_config(self.root, self.config)

    def test_orphan_session_without_config_blocks_new_credentials(self):
        self.root.mkdir()
        session = self.root / "mtproto.session"
        session.write_bytes(b"keep")
        with self.assertRaisesRegex(ConfigError, "offline reset"):
            save_config(self.root, self.config)
        self.assertFalse((self.root / CONFIG_NAME).exists())
        self.assertEqual(session.read_bytes(), b"keep")

    def test_posix_private_permissions_and_reject_exposed_existing_file(self):
        if os.name != "posix":
            self.skipTest("POSIX permissions")
        save_config(self.root, self.config)
        path = self.root / CONFIG_NAME
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.chmod(0o644)
        with self.assertRaises(ConfigError):
            load_config(self.root)

    def test_one_shot_code_password_handoff_and_challenge(self):
        save_login_challenge(self.root, "challenge-token")
        stage_login_secret(self.root, code="12345", password="private-password")
        self.assertEqual(load_login_challenge(self.root), "challenge-token")
        secret = consume_login_secret(self.root)
        self.assertEqual(secret.code, "12345")
        self.assertEqual(secret.password, "private-password")
        self.assertNotIn("12345", repr(secret))
        self.assertNotIn("private-password", repr(secret))
        self.assertIsNone(consume_login_secret(self.root))
        self.assertFalse((self.root / HANDOFF_NAME).exists())
        clear_login_challenge(self.root)
        self.assertIsNone(load_login_challenge(self.root))
        self.assertFalse((self.root / CHALLENGE_NAME).exists())

    def test_password_only_and_explicit_supersession(self):
        stage_login_secret(self.root, code="12345", phone_code_hash="hash")
        stage_login_secret(self.root, password="second-stage-password")
        secret = consume_login_secret(self.root)
        self.assertIsNone(secret.code)
        self.assertIsNone(secret.phone_code_hash)
        self.assertEqual(secret.password, "second-stage-password")

    def test_bad_handoff_is_consumed_and_never_echoed(self):
        stage_login_secret(self.root, code="12345")
        (self.root / HANDOFF_NAME).write_text('{"password":"private-sentinel",', encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            consume_login_secret(self.root)
        self.assertNotIn("private-sentinel", str(caught.exception))
        self.assertIsNone(consume_login_secret(self.root))
        self.assertFalse(any("claim-" in p.name for p in self.root.iterdir()))

    def test_auth_values_reject_invalid_or_empty(self):
        for kwargs in ({}, {"code": "not digits"}, {"password": ""},
                       {"phone_code_hash": "hash"}, {"password": "new\nline"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigError):
                stage_login_secret(self.root, **kwargs)
        self.assertIsNone(consume_login_secret(self.root))

    def test_state_directory_symlink_refused(self):
        outside = Path(self.temp.name) / "other"
        outside.mkdir()
        try:
            self.root.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable in this environment")
        with self.assertRaises(ConfigError):
            save_config(self.root, self.config)
        self.assertEqual(list(outside.iterdir()), [])

    def test_relative_or_git_tree_state_directory_refused(self):
        with self.assertRaises(ConfigError):
            save_config("relative-runtime-state", self.config)
        skill_tree = Path(__file__).resolve().parents[2] / "skills" / "telegram-user-session"
        with self.assertRaises(ConfigError):
            save_config(skill_tree / "unwanted-private-state", self.config)
        self.assertFalse((skill_tree / "unwanted-private-state").exists())


if __name__ == "__main__":
    unittest.main()
