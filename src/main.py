"""App factory: mounts every module router under /api/v1."""

from fastapi import FastAPI

from modules.approvals.router import router as approvals
from modules.casestudy.router import router as casestudy
from modules.docgen.router import router as docgen
from modules.governance.router import router as governance
from modules.identity.router import router as identity
from modules.knowledge.router import router as knowledge
from modules.notifications.router import router as notifications
from modules.revenue.router import router as revenue
from modules.rfp.router import router as rfp
from modules.search.router import router as search

ROUTERS = [identity, knowledge, search, rfp, revenue, casestudy, approvals, docgen, notifications, governance]


def create_app() -> FastAPI:
    app = FastAPI(title="Revenue Engine API", version="0.1.0")
    for r in ROUTERS:
        app.include_router(r, prefix="/api/v1")

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
