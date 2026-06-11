"""Pytest bootstrap: make the project root importable (`import main`)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
