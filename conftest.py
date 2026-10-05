"""Pytest configuration: add the repo root to sys.path so test modules can
import top-level packages like whatsapp, bot, main, etc. without installation.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
