"""API subpackage (FastAPI app, routes, proxy)."""

from __future__ import annotations

from cua_house_server.api.app import create_app

__all__ = ["create_app"]
