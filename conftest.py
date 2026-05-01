"""
Root conftest.py — adds webapp/ to sys.path so that pytest-django
can find dhn_web.settings regardless of where pytest is invoked.
"""

import sys
from pathlib import Path

# Allow 'import dhn_web' and 'import dashboard' without installing the app
sys.path.insert(0, str(Path(__file__).parent / "webapp"))
sys.path.insert(0, str(Path(__file__).parent / "src"))
