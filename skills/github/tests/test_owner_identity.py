"""Actor and credential confirmations identify the same GitHub user in core."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gh_presence.config import account_id
from gh_presence.identity import connector_identity
from gh_presence.provider import GitHub


class OwnerIdentityTests(unittest.TestCase):
    def test_actor_and_credential_confirmations_match_in_both_directions(self):
        configured = os.environ.get("OUROBOROS_IDENTITY_CORE_ROOT", "").strip()
        if not configured:
            self.skipTest("set OUROBOROS_IDENTITY_CORE_ROOT for the core owner-account contract")
        core = Path(configured).resolve()
        writer = {"id": 11, "login": "human-writer", "type": "User"}
        reader = {"id": 22, "login": "human-reader", "type": "User"}
        identity = connector_identity(
            {"expected_writer_id": 11, "auth_mode": "pat", "notifications_user_id": 22},
            {"auth:writer": {**writer, "state": "valid", "auth_mode": "pat"},
             "auth:notifications": {**reader, "state": "valid", "auth_mode": "pat"}},
        )
        cases = []
        for role, user, credential in (("writer", writer, identity["sends_as"]),
                                       ("reader", reader, identity["reads_as"][0])):
            actor = GitHub(None, None).actor({"user": user})
            for confirmation in ("actor", "credential"):
                cases.append({"role": role, "confirmation": confirmation,
                              "account_id": account_id(writer["id"]),
                              "actor_id": actor["platform_actor_id"], "credential_id": credential["id"]})
        code = '''import json, sys
from ouroboros.places import OwnerAccount, owner_account_matches
results = []
for case in json.load(sys.stdin):
    confirmed = case[case["confirmation"] + "_id"]
    owners = (OwnerAccount(confirmed, account_id=case["account_id"]),)
    results.append({
        "actor": owner_account_matches(owners, case["actor_id"], case["account_id"]),
        "credential": owner_account_matches(owners, case["credential_id"], case["account_id"]),
        "other_account": owner_account_matches(owners, case["actor_id"], "another-account"),
        "other_user": owner_account_matches(owners, "33", case["account_id"]),
        "revoked": owner_account_matches((), case["credential_id"], case["account_id"]),
    })
print(json.dumps(results))
'''
        with tempfile.TemporaryDirectory(prefix="github-core-identity-") as tmp:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": tmp, "PYTHONPATH": str(core),
                   "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "LC_ALL": "C", "LANG": "C",
                   "OUROBOROS_APP_ROOT": tmp, "OUROBOROS_DATA_DIR": tmp + "/data",
                   "OUROBOROS_REPO_DIR": tmp + "/repo", "OUROBOROS_SETTINGS_PATH": tmp + "/settings.json"}
            result = subprocess.run([sys.executable, "-B", "-c", code], input=json.dumps(cases),
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        results = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(len(results), len(cases))
        for case, matches in zip(cases, results):
            with self.subTest(role=case["role"], confirmation=case["confirmation"]):
                self.assertEqual(matches, {"actor": True, "credential": True,
                                           "other_account": False, "other_user": False, "revoked": False}, case)


if __name__ == "__main__":
    unittest.main()
