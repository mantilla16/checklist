"""Punto de entrada para gunicorn.

    gunicorn --workers 3 --timeout 180 --bind 127.0.0.1:5000 wsgi:app

El timeout largo es necesario: el OCR del RADIAN puede tardar 20 segundos por
documento y gunicorn mataria al trabajador con el valor por omision (30 s).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

from server import app  # noqa: E402

__all__ = ["app"]
