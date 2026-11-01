"""Shared setup: import paths and fake credentials. Every test runs against
fakes (no Shopify, Supabase, Google or Telegram calls), so the suite is safe
to run anywhere, including GitHub Actions on every push."""
import os
import sys
import types
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "seo")]
os.environ.setdefault("GEMINI_API_KEY", "AIzaFAKEKEY_for_test_1234567890abcdef")
os.environ.pop("ANTHROPIC_API_KEY", None)


def tool_text(data):
    """What mcp_server.call_tool returns: a list of text content blocks."""
    return [types.SimpleNamespace(text=json.dumps(data))]
