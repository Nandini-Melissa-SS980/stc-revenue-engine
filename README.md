# Revenue Engine — Backend

Skeleton of the backend repo. The design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md);
apart from `main.py` (app factory) and `core/config.py` (settings), every file under `src/`
is a stub whose docstring says what it is responsible for.
The AI service (parsing, retrieval, models, agents) is a separate repo.

```
src/
  main.py          app factory (mounts module routers under /api/v1)
  core/            cross-cutting: config, context, db/RLS, security, rbac, masking, audit, outbox, lifecycle
  integrations/    AI service client + contract, connectors, storage, secrets
  modules/         identity · knowledge · search · rfp · revenue · casestudy · approvals · docgen · notifications · governance
  workers/         Celery app and scheduled tasks
```

## Run

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
cd src && ../.venv/Scripts/uvicorn main:app --reload
# http://localhost:8000/health · http://localhost:8000/docs
```
