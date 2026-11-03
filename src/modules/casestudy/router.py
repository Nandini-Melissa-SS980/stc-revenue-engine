"""casestudy — HTTP endpoints.

TODO: implement per docs/ARCHITECTURE.md.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/case-studies", tags=["casestudy"])
