"""Make `episode_recorder` importable when a tool runs from a checkout, with no install step.

Only the repo root goes on sys.path here. Where the submodules live is bootstrap.py's
business and nobody else's.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
