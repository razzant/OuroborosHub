"""Set only this run's explicit controls after safe_test scrubs the environment."""
import json
import os
import sys
from pathlib import Path

spec = json.loads(Path(sys.argv[1]).read_text())
os.chdir(spec['cwd'])
os.environ.pop('ATLAS_HOST_ROOT', None)
os.environ.update(spec['env'])
os.execv(sys.executable, [sys.executable, '-B', *spec['argv']])
