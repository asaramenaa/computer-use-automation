"""Local stand-in for a legacy member-servicing application.

Frameset layout, nested tables, no ids/test ids, non-semantic markup.
All data is seeded and fake. Fault injection is a test harness only.
"""
from .app import create_app

__all__ = ["create_app"]
