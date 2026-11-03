"""knowledge — HTTP endpoints.

TODO: implement per docs/ARCHITECTURE.md.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
