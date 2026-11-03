"""Install one skill payload into one isolated data root with SYNTHETIC admission records for its exact bytes.

Run as a child of the fixture with the isolated environment (``python -I -B install_skill.py --core <copy>
--data <data root> --skill <payload dir>``): the host's own loader functions, never the installed application's
state. The payload replaces any earlier copy (an update). The ``pass`` review state and the grants written here are
synthetic fixture setup keyed to the new content hash so the host will load the payload: no review was performed
and none is claimed.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--core", required=True, type=Path)
ap.add_argument("--data", required=True, type=Path)
ap.add_argument("--skill", required=True, type=Path)
ap.add_argument("--enable", action="store_true", help="also persist enablement (a fresh install)")
args = ap.parse_args()
sys.path.insert(0, str(args.core))

from ouroboros.skill_loader import (SkillReviewState, compute_content_hash, load_skill, requested_core_setting_keys,
                                    requested_skill_permissions, save_enabled, save_review_state, save_skill_grants)

NAME = "telegram-bot"
target = args.data / "skills" / "external" / NAME
if target.exists():
    shutil.rmtree(target)
shutil.copytree(args.skill, target, ignore=shutil.ignore_patterns("__pycache__", "tests", ".git"))
digest = compute_content_hash(target)
# Synthetic fixture admission, not a review result.
save_review_state(args.data, NAME, SkillReviewState(status="pass", content_hash=digest))
loaded = load_skill(target, args.data)
keys = requested_core_setting_keys(list(loaded.manifest.env_from_settings or []))
permissions = requested_skill_permissions(list(loaded.manifest.permissions or []),
                                          list(getattr(loaded.manifest, "subscribe_events", []) or []))
save_skill_grants(args.data, NAME, keys, content_hash=digest, requested_keys=keys,
                  granted_permissions=permissions, requested_permissions=permissions)
if args.enable:
    save_enabled(args.data, NAME, True)
print(json.dumps({"admission": "synthetic fixture setup: review state and grants written for the content hash; no review performed",
                  "content_hash": digest, "version": loaded.manifest.version, "keys": keys, "permissions": permissions,
                  "load_error": loaded.load_error}))
