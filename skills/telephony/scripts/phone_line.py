"""Entry point of the ``phone_line`` companion (the host starts it from the skill directory)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from phone.daemon import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
