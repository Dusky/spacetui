import os
import sys

# make the repo root importable (mirrors tui/__init__.py) so `import store` etc.
# resolve whether launched as `python -m web` or imported by st.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from .server import create_app  # noqa: E402

__all__ = ["create_app"]
