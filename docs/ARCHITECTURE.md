# Revenue Engine — Backend Architecture (BE repo)

**Status:** Approved design, v1 · **Scope:** this repository only (the AI service is a separate repo)
**Sources:** STC RFE v1.0 (Jul 2026) · BRD v0.2 (Oct 2026) · Kiwitech prototype (`src/js`, `docs/01–05`)

---

## 1. Purpose and boundaries

The backend is the **system of record and the policy engine** of Revenue Engine. It decides
*who may do what to which object*. The AI service decides *what text says and which sources
support it*.

| Concern | BE repo (this) | AI repo (separate) |
|---|---|---|
| Users, workspaces, SSO, sessions, RBAC | ✅ | — |
| Business objects: RFPs, questions, opportunities, outreach, case studies, proposal & answer libraries | ✅ system of record | receives masked copies |
| Lifecycles & gates (BR-07…14), approvals, SLA / escalation | ✅ | — |
| Estimate engine (deterministic maths) | ✅ | — |
| Connectors & tokens (Drive, Salesforce, Outreach.io, Slack, e-mail, Box, Contentful) | ✅ tokens never leave BE | — |
| Object storage (raw files, rendered docs) | ✅ | reads via signed URLs |
| Parsing, OCR, chunking, embeddings, vector / keyword index | — | ✅ |
| Retrieval, rerank, prompt masking, grounding, claim verification, confidence | — | ✅ |
| Agents (Ask, RFP analysis, drafting, signals), model gateway, registry, eval harness | — | ✅ |
| Audit trail, usage allowances, notifications, document generation | ✅ | sends usage + trace ids |
| Tenant isolation | ✅ Postgres RLS | ✅ per-workspace index + filter |

**Hard rules**
1. BE never calls a model. AI never changes business state.
2. Nothing client-facing leaves the platform without a recorded human approval of *that exact content* (HL-01, BR-01).
3. No autonomous sending, no uncited claims, no generated pricing, no self-approval — not configurable (BRD 14.3).

---

## 2. Topology

```
 Web app ──► BE API (FastAPI, stateless ×N) ──── HTTPS + service JWT ────► AI service (GPU)
  (SSE)        │    │   ▲                                                    │
               │    │   └────── HMAC-signed job callbacks ◄──────────────────┤
               │    └──► Workers (Celery): outbox relay, Drive sync,         │
               │          SLA scan (60 s), exports, retention                │
               ▼                                                             ▼
   PostgreSQL 16 (RLS)  ·  Redis (sessions, cache, locks, queues)  ·   AI's own store:
   Object storage (MinIO/S3)  ·  Vault (tokens, per-workspace keys)    chunks, vectors, traces
                │
   Connectors: Google Drive (system of record for files) · Salesforce · Outreach.io ·
               Slack · M365/e-mail · Box · Contentful · REST/webhook kit · MCP (Phase 2)
```

**Style: modular monolith + workers.** One deployable API and one worker image, with
strict module boundaries. Rationale: one tenant context per request, transactions that span
modules (an approval decision moves its subject in the same commit), ≥30 users/workspace,
small team. Any module can be extracted later because modules talk only through service
functions and outbox events.

---

## 3. Repository layout

```
backend/
  src/
    main.py                 app factory: routers, middleware, error handlers
    core/                   cross-cutting — no business logic
      config.py             settings (infra only; business thresholds live in DB)
      context.py            RequestContext (workspace, actor, role, permissions) — contextvar
      db.py                 async SQLAlchemy, Base + mixins, tenant_session() (SET LOCAL)
      security.py           sessions, dev auth, require(permission)
      rbac.py               Role, Permission, default matrix
      masking.py            MaskedModel / Restricted* fields (KB-06)
      audit.py              hash-chained audit events
      events.py             transactional outbox + subscribers
      lifecycle.py          generic stage machine + gates
      signing.py            HMAC (callbacks, webhook kit)
      service_tokens.py     JWTs for AI service calls
      errors.py             problem+json errors (GateBlocked → 409)
    integrations/
      ai_client/            contract models, HTTP client, job tracking + callbacks
      connectors/           Connector protocol, gdrive (salesforce, box … later)
      storage.py            object store + signed URLs
      secrets.py            vault abstraction
    modules/                one folder per domain: models · schemas · service · router (+ rules/lifecycle)
      identity/             workspaces, users, invitations, role overrides, support grants, OIDC
      knowledge/            sources, synced files, uploads, documents, entities & merges, profiles, win/loss
      search/               proxy to AI search / ask (SSE), saved searches
      rfp/                  RFP lifecycle, questions, evaluation, clarifications, estimate, libraries
      revenue/              opportunities (signals), account briefs, outreach
      casestudy/            case studies, metrics with sources, quote permissions
      approvals/            draft versions, approval items, SLA, export guard
      docgen/               governed templates → DOCX/PDF/XLSX → Drive
      notifications/        in-app bell + Slack/e-mail mirroring
      governance/           audit API, usage metering, configuration
    workers/                celery app, beat schedule, tasks
  migrations/               Alembic (tables + RLS policies + grants)          — to add
  mock_ai/                  tiny stand-in for the AI service (local dev / CI) — to add
  tests/                    unit · integration · isolation · ai-contract      — to add
  docker-compose.yml        postgres, redis, minio, vault-dev, mock-ai, api, worker — to add
```

**Current state:** every file under `src/` is a stub (docstring + empty router). Only
`core/config.py` and `main.py` contain code.

**Dependency rule:** `core` ← `integrations` ← `modules`. A module may call another module's
`service.py`, never its tables. `rules.py` / `lifecycle.py` / `estimate.py` are **pure** (no I/O)
so every business rule is unit-testable without a database.

---

## 4. Request lifecycle

```
request → authenticated()            session cookie (or X-Dev-* in local/test) → RequestContext
        → require(Permission.X)      route-level RBAC
        → db()                       BEGIN; SET LOCAL app.workspace_id / app.actor_id
        → router → service           object-level checks, gates, business rules
              ├─ audit.record()      same transaction
              └─ events.emit()       same transaction (outbox)
        → response model             MaskedModel blanks restricted fields if no right
        → COMMIT                     outbox relay picks events up afterwards
```

The workspace comes **only** from the session — never from a body, prompt or document.

---

## 5. Multi-tenancy and isolation (BR-26, BR-34)

- Every tenant table has `workspace_id` (via `TenantMixin`); **Postgres RLS** policy
  `workspace_id = current_setting('app.workspace_id')::uuid` on all of them.
- `tenant_session()` sets it with `set_config(…, is_local => true)` per transaction, so pooled
  connections never leak context. No context → zero rows (fail closed).
- App connects as `revenue_app` (no BYPASSRLS). `revenue_admin` is used only by migrations,
  sign-in identity lookup, operator console (metadata only) and retention jobs.
- Redis keys prefixed `ws:{id}:`; object keys prefixed `ws/{id}/`.
- Per-workspace data key in Vault transit (envelope encryption of tokens / sensitive columns).
- Platform Operators: separate auth realm; content only via time-boxed, audited `support_grant`.
- **CI isolation suite:** two seeded workspaces; every list/get endpoint and the AI proxy are
  probed for cross-workspace leakage; `tenant_tables()` must equal the set of RLS-enabled tables.

---

## 6. Authentication, authorisation, masking

**AuthN (GV-01, BRD 12.1)** — OIDC only: Google by default, Azure AD / Okta per workspace, MFA
at the IdP. No local passwords. Server-side sessions in Redis behind an httpOnly cookie:
30 min idle (sliding), 12 h absolute. Invitations expire after 7 days; disabled / not-activated
accounts cannot sign in (BR-22). Role or status change deletes the user's live sessions.

**AuthZ** — `require(Permission.X)` on every route; object rules in services:
own drafts only, gap assignee roles (BR-11), fallback-approver roles (BR-16), no self-approval
(BR-01). Default role → permission matrix in `core/rbac.py` (from prototype + BRD 12.2);
workspaces may override per role except Platform Administrator, who always holds all (BR-21).

| Role | Key rights |
|---|---|
| Platform Administrator | everything (never approves own items) |
| Pre-Sales Lead | RFPs, assign gaps, assemble, approve RFP answers & proposals, restricted values |
| Pre-Sales Analyst / SME | draft answers assigned to them; values masked |
| Sales Manager | outreach, export, approve outreach, restricted values |
| Sales Representative | own outreach; values masked |
| Marketing Manager | case studies, approve case studies (not own); values masked |
| Executive | read-only across modules, restricted values |

**Masking (KB-06, BR-06)** at three points:
1. **API responses** — fields typed `RestrictedDecimal/Str` serialise as `null` + listed in
   `masked_fields` unless the context has `search.restricted`.
2. **Prompts** — the AI service masks before the model, driven by the `can_see_restricted`
   claim in the service JWT (never by the request body).
3. **Exports / reports** — serialised through the same schemas.

---

## 7. Data model

UUIDv7 keys · UTC timestamps · money `NUMERIC` (EUR) · append-only versions (BR-25).

| Area | Tables |
|---|---|
| Tenancy | `workspace`, `workspace_settings`, `app_user`, `invitation`, `role_override`, `support_grant`, `operator` |
| Knowledge | `source`, `synced_file`, `upload`, `document`, `client_entity`, `merge_candidate`, `project_profile`, `win_loss` |
| RFP | `rfp`, `rfp_question`, `evaluation`, `clarification`, `library_answer`, `proposal_library_entry`, `proposal_draft` |
| Revenue | `opportunity`, `account_brief`, `outreach` |
| Case studies | `case_study`, `case_metric`, `quote_permission` |
| Human-in-the-loop | `draft_version` (immutable), `approval_item` (pinned to `content_hash`) |
| Integration | `ai_job`, `outbox_event` |
| Governance | `audit_event` (hash chain), `usage_record`, `notification` |

Restricted columns: `rfp.value`, `opportunity.value/margin/contact_*`, `project_profile.value`,
`win_loss.value`, `proposal_library_entry.value`, rate card, estimate price.

Chunks / embeddings are **not** in this database — they live in the AI service, keyed by
our `document.id` and `workspace_id`.

---

## 8. Lifecycles and gates

Generic `Lifecycle(stages, gates, extra_transitions)`; `advance()` either moves the object or
raises `GateBlocked` (HTTP 409) whose `detail` is the plain-words blocker shown in the UI.
Gates are pure functions over a view object loaded by the service.

| Object | Stages | Gates |
|---|---|---|
| **RFP** (RW-1) | received → evaluated → drafting → review → assembly → submitted → closed (+ evaluated → closed on no-bid) | analysis done · bid decision recorded, no-bid needs reason · **BR-07** no gap without owner · **BR-08** all approved or waived, mandatory never waived · **BR-09** response doc exists, pricing placeholders filled, final uploaded · **BR-10** outcome + reason, loss needs lesson |
| **Opportunity** | signal → qualified → pursuing → converted / dismissed | **BR-12** human confirms · owner set · linked outreach or RFP · dismiss needs reason · **BR-24** no resurfacing for 90 days without new evidence |
| **Outreach** (RW-2) | target → research → drafting → review → exported → closed | **BR-13** ≥ 2 internal proofs before approval · export only of approved content |
| **Case study** (RW-3) | source → drafting → review → published | **BR-14** won + delivered project · each metric has a verified source · quotes need written permission |

Every transition writes an audit event and an outbox event in the same transaction.

---

## 9. Human-in-the-loop (HL-01…05)

```
AI draft ─► draft_version v1 (origin=ai, trace_id)
human edit ─► v2, v3 … (origin=human, edit_ratio vs v1)            HL-02
submit ─► BR-03 check: every claim cites a returned source
        ─► approval_item {content_hash = hash(latest version), sla_due}   HL-01
decide ─► approve | reject (reason) | request changes (reason, BR-17)
        ─► permission per kind, author ≠ approver (BR-01)
        ─► UPDATE … WHERE state='pending' RETURNING → first decision wins (BR-18)
        ─► module handler applies it (e.g. reusable answer → Answer library, BR-23)
export ─► approved_version_for_export(): approved hash == current hash, else blocked
```

- **One inbox** (HL-03) for web, Slack and e-mail — all call the same decision endpoint; the
  channel is recorded. ≤ 2 clicks.
- **Source verification** (HL-04): detail endpoint returns claims ↔ citations and weak-claim count;
  re-verification proxied to AI `/v1/verify`.
- **SLA** (HL-05, BR-15/16): beat task every 60 s → warning 4 h before the 24 h deadline,
  escalation to the fallback approver on breach (well within the 5-minute target).
- Approval is **pinned to content**: any edit after approval forces re-approval.

---

## 10. BE ↔ AI service contract (v1)

**Auth:** short-lived service JWT minted per call (`core/service_tokens.py`), claims:
`workspace_id, actor_id, actor_kind, role, can_see_restricted, request_id, purpose`.
Production signs RS256/ES256; AI verifies against BE's JWKS.

**Synchronous**

| Endpoint | Purpose | Requirement |
|---|---|---|
| `POST /v1/ask` (SSE) | cited answer, confidence, `no_confident_source`, experts | SR-01/02/05 |
| `POST /v1/search` | hybrid search with facets | SR-03 |
| `POST /v1/similar` | similar past projects with outcome + "why" | SR-04 |
| `POST /v1/draft/{kind}` | rfp_answer · outreach · follow_up · account_brief · case_study · clarification · proposal_section | RP-02, OA-01/02/04 |
| `POST /v1/verify` | claim ↔ source entailment | HL-04 |

**Asynchronous jobs:** `POST /v1/jobs {type, input, callback_url, idempotency_key, priority}` →
`job_id`; progress / completion POSTed to `callback_url` (HMAC-signed, deduped on `event_id`);
`GET /v1/jobs/{id}` fallback; `DELETE` cancels.
Types: `ingest_file`, `delete_file`, `rfp_analysis`, `reindex_answer`, `reindex_profile`, `bulk_draft`.

**Every response** carries `trace_id` (stored on `draft_version` → NF-05 reconstruction across
repos) and `usage` (→ `usage_record`, GV-05).

**Guarantees re-checked in BE:** uncited claims block submission (`uncited_claims()`), so a faulty
AI release cannot push unsourced content to a client.

**Contract source of truth:** AI repo publishes OpenAPI; BE regenerates
`integrations/ai_client/contract.py`; a contract test fails CI on drift. `mock_ai/` implements
the contract deterministically for local dev and CI.

**Job submission is transactional:** `jobs.submit()` writes `ai_job` + outbox event; the relay sends
it to AI only after commit (no orphan jobs on rollback).

---

## 11. Key flows

**Upload (BR-30)** — check `kb.upload` → write to the workspace Drive *Upload* folder → copy to object
storage → `document(status=pending)` → `ingest_file` job (signed URL) → callback sets metadata,
links/merges client entity (BR-19: > 0.90 auto, 0.75–0.90 queue, all reversible), upserts project
profile and win/loss → audit. Drive write failure: upload held 24 h, user sees the error.

**Drive sync (KB-05, BR-29)** — push channel + polling fallback; `list_changes(cursor)` per source;
SHA-256 skips unchanged files; removed / de-selected files → `delete_file` job + document retired.
Back-load cursor is a JSON checkpoint → resumable without duplicates (KB-01).

**RFP to response (RW-1)** — upload RFP → `rfp_analysis` job (9 stages, progress over SSE) → callback
upserts questions with coverage Covered / Partial / Gap + citations (BR-31), facts, clarifications
(drafts only, BR-32), similar projects, risks, scorecard → evaluation & bid decision → gaps assigned
(BR-11) → answers drafted / edited / approved → docgen with pricing placeholders → human fills
pricing (BR-05/09) → final upload → submitted → outcome (BR-10) → win/loss record.

**Proposal builder** (prototype 77-proposal-flow.js, to be ported to `rfp/estimate.py` with identical figures) —
core vs commodity features → library matching (+8 industry, +3 won) → greedy set cover of won bids
(≤ 4) → client queries (open +20 %, assumed +10 %) → standard features → +25 % overhead →
contingency by engagement model (fixed 15 %, equity / milestone 10 %, T&M / dedicated 0 %) →
team 4/6/8 → weeks. Price = total × blended rate is **indicative and restricted**; documents carry
placeholders.

**Target to outreach (RW-2)** — account brief (AI, stale after 30 days) → similar won deals →
draft variants with ≥ 2 internal proofs (BR-13) → rep edits → approval → export to Outreach.io as a
draft sequence (never sent, BR-02) → Salesforce activity write-back.

**Project to case study (RW-3)** — won + delivered project only (BR-14) → AI narrative + suggested
metrics → each metric verified to a source, quote permission evidence attached → approval →
publish to collateral library / Contentful.

---

## 12. Workers and scheduling

Celery on Redis. Queues: `interactive` (priority), `batch`, `events`. Per-workspace fair-share via
Redis semaphores so one back-load cannot starve others.

| Task | Schedule | Purpose |
|---|---|---|
| `outbox.relay` | every 2 s | deliver outbox events to handlers (notifications, AI submits, Slack) with retry |
| `approvals.sla_scan` | every 60 s | warnings and escalations (BR-15) |
| `knowledge.drive_poll` | every 5 min | Drive change fallback (push is primary) |
| `knowledge.purge_failed_uploads` | hourly | uploads held > 24 h |
| `revenue.brief_staleness` | daily | mark briefs stale after 30 days |
| `governance.retention` | daily | purge beyond 84 months, re-anchor audit chain (admin role) |
| `governance.audit_verify` | daily | verify every workspace chain; alert on break |

Temporal is the upgrade path if workflows become multi-day.

---

## 13. Governance

- **Audit (GV-03, NF-05, BR-25):** every state change → actor kind/id, action, object, requirement
  id, IP, channel, request id, before/after. Per-workspace hash chain
  (`hash = sha256(prev_hash ‖ canonical(row))`, advisory-locked appends). App role has
  INSERT/SELECT only. Signed CSV export, Platform Administrator only. 84-month retention.
- **NF-05 reconstruction:** `audit_event` + `draft_version.trace_id` (AI inputs, access evaluation,
  sources, model/prompt versions) + human edits + approval = full chain.
- **Usage (GV-05):** `usage_record` per function / agent / workflow / model, monthly allowance per
  plan (AI jobs, tokens, GPU minutes), CSV export.
- **Model registry & evaluation** live in the AI repo; BE proxies them to the admin screens.

---

## 14. API conventions

- REST under `/api/v1`, OpenAPI generated from code = contract with the frontend.
- Errors: RFC 9457 problem+json; `GateBlocked` 409 with `gate` object; `RuleViolation` 422 with `rule`.
- Keyset pagination (`next_cursor`).
- `Idempotency-Key` on POSTs that start jobs or exports.
- SSE for Ask streaming and job progress (first token target < 3 s).
- Webhooks in (IN-05): HMAC `X-RE-Signature`, idempotency keys.

### Endpoint map (v1)

| Module | Endpoints |
|---|---|
| auth | `GET /auth/login/{provider}` · `GET /auth/callback/{provider}` · `POST /auth/logout` |
| identity | `GET /me` · `GET /users` · `PATCH /users/{id}` · `POST /invitations` · `GET /roles` · `PUT /roles/{role}` |
| knowledge | `GET/POST /sources` · `POST /sources/{id}/sync` · `POST /uploads` · `GET /documents` · `GET /documents/{id}` · `POST /documents/{id}/retry` · `GET /merges` · `POST /merges/{id}/decision` · `POST /merges/{id}/revert` · `GET/PATCH /projects/{id}` |
| search | `POST /search` · `POST /ask` (SSE) · `POST /similar` · `GET/POST /saved-searches` |
| rfp | `GET/POST /rfps` · `GET /rfps/{id}` · `GET /rfps/{id}/gate` · `POST /rfps/{id}/advance` · `POST /rfps/{id}/evaluation` · `GET /rfps/{id}/questions` · `PATCH /questions/{id}` (assign, waive) · `POST /questions/{id}/draft` · `PUT /questions/{id}/answer` · `POST /questions/{id}/submit` · `GET /rfps/{id}/clarifications` · `POST /rfps/{id}/assemble` · `POST /rfps/{id}/final` · `POST /rfps/{id}/outcome` |
| proposals | `POST /proposal-drafts` · `PATCH /proposal-drafts/{id}` · `GET /proposal-drafts/{id}/matches` · `POST /proposal-drafts/{id}/best-combination` · `GET /proposal-drafts/{id}/estimate` · `GET/POST /proposal-library` · `POST /proposal-library/import` |
| answers | `GET /answers` · `GET /answers/{key}/versions` · `POST /answers` |
| revenue | `GET /opportunities` · `POST /opportunities/{id}/advance` · `POST /opportunities/{id}/dismiss` · `POST /briefs` · `GET/POST /outreach` · `POST /outreach/{id}/draft` · `PUT /outreach/{id}/content` · `POST /outreach/{id}/submit` · `POST /outreach/{id}/export` |
| case studies | `GET/POST /case-studies` · `POST /case-studies/{id}/draft` · `PUT /case-studies/{id}/metrics` · `POST /case-studies/{id}/submit` · `POST /case-studies/{id}/publish` |
| approvals | `GET /approvals` · `GET /approvals/mine` · `GET /approvals/{id}` · `POST /approvals/{id}/decision` · `GET /drafts/{type}/{id}/versions` |
| docgen | `GET /templates` · `POST /documents/generate` |
| notifications | `GET /notifications` · `POST /notifications/read` |
| governance | `GET /audit` · `GET /audit/export` · `GET /audit/verify` · `GET /usage` · `GET/PUT /config` |
| internal | `POST /internal/ai/callbacks` · `GET /internal/files` (signed) · `POST /internal/webhooks/{source}` |

---

## 15. Non-functional mapping

| NFR | How |
|---|---|
| NF-01 ≥ 30 concurrent users | stateless API replicas, async I/O, connection pool per replica |
| NF-02 search p95 < 4 s | BE adds < 50 ms (auth + proxy); budget lives in AI service |
| NF-03 99.5 % business hours | ≥ 2 API replicas, managed Postgres HA, Redis replica, health/readiness probes |
| NF-04 TLS 1.2+ / AES-256 / vault | TLS at ingress, encrypted volumes, per-workspace data key, Vault for secrets |
| NF-05 reconstructable drafts | audit chain + draft versions + AI trace ids |
| BRD 11.3 plain-word blockers | `GateBlocked.detail` / `RuleViolation.detail` rendered verbatim |
| BRD multi-tenancy < 5 min provisioning | operator endpoint creates workspace, settings, data key, admin invite |

---

## 16. Technology stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 (async) + asyncpg · Alembic · PostgreSQL 16 ·
Redis 7 · Celery 5 · httpx · PyJWT · structlog · OpenTelemetry → Grafana/Tempo · MinIO/S3 ·
HashiCorp Vault · Docker Compose (local) · Kubernetes (prod) · pytest · ruff · mypy.

---

## 17. Testing strategy

| Layer | What |
|---|---|
| Unit (no DB) | RBAC matrix, masking, every gate (BR-07…14), approval rules (BR-01/17/18), SLA maths, estimate (golden figures vs prototype), entity similarity (BR-19), audit chain, signing |
| Integration (Postgres) | services + routers through HTTP with dev auth; RLS on; outbox; first-decision-wins race |
| Isolation | two workspaces; every endpoint probed for leakage; RLS coverage = tenant tables |
| Contract | contract models vs AI OpenAPI; mock_ai satisfies contract |
| Acceptance | traceability matrix: each RFE acceptance criterion → test id → result |

---

## 18. Decisions and open questions

**Resolved in this design**
1. SSO only, no passwords (prototype's password/MFA screens dropped) — GV-01, BRD 12.1.
2. Estimate price is indicative + restricted; documents always carry pricing placeholders — BR-05.
3. Assembly gate needs response doc + placeholders filled + final upload — BR-09.
4. Freshness: Drive 15 min, other sources 24 h — BRD.
5. Project profiles stored in BE (user-editable), generated by AI, re-indexed on edit.
6. Job transport: HTTP + signed callbacks (repos deploy independently).

**Open (affect BE)**
- OQ-14 mirror per-file Drive permissions in search? (needs document ACL table)
- OQ-12 hosting provider / region · OQ-03 break-glass admin · OQ-04 MFA enforcement
- OQ-09 can RFP answers be approved from Slack? · OQ-10 second approver for library versions

---

## 19. Build order

1. Core → migrations + RLS → identity/auth.
2. Knowledge: sources, Drive connector, uploads, ingest callback.
3. AI client + search/ask proxy (against mock_ai).
4. RFP lifecycle, analysis callback, approvals, estimate, proposal builder.
5. Revenue (opportunities, outreach), case studies.
6. Docgen, notifications, governance APIs, workers.
7. Isolation + contract suites, load test, hardening.
