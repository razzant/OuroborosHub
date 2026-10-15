"""Import the reviewed payload without placing these offline tests in its install manifest."""
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills" / "telegram-user-session"
sys.path.insert(0, str(SKILL_ROOT))
