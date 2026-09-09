"""fo4-db — Fallout 4 data wiki: canonical SQLite DB of the local game install.

Public API:
  parser — plugin record walker (harvested from espdump.py)
  db     — SQLite schema + ingestion
  cli    — query verbs (fo4-db command)
"""
from . import parser, db

__all__ = ["parser", "db"]
__version__ = "0.1.0"
