"""Bot package bootstrap."""
from pathlib import Path
import sys

_pkg_path = Path(__file__).resolve().parent
if str(_pkg_path) not in sys.path:
    sys.path.append(str(_pkg_path))

__all__ = []
