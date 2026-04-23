"""
config.py — Configuration for PM Brain
---------------------------------------
Centralizes all settings so brain.py stays clean.
"""

import os
import sys

# ── API CONFIGURATION ────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 4000

# ── FILE PATHS ───────────────────────────────────────────────────────
MOCK_DATA_PATH = os.path.join(os.path.dirname(__file__), "mock_jira.json")
OUTPUT_JSON_PATH = os.path.join(os.path.dirname(__file__), "insights_output.json")
OUTPUT_HTML_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")

# ── DISPLAY SETTINGS ────────────────────────────────────────────────
USE_COLORS = sys.stdout.isatty()  # Only use colors in real terminal

def validate():
    """Check all required config is present."""
    errors = []
    if not ANTHROPIC_API_KEY:
        errors.append(
            "ANTHROPIC_API_KEY not set.\n"
            "  → Get one at: https://console.anthropic.com\n"
            "  → Then run:   set ANTHROPIC_API_KEY=sk-ant-your-key-here"
        )
    return errors
