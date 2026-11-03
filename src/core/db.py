"""Async SQLAlchemy engine, declarative Base and mixins (Id, Timestamp, Tenant).
tenant_session(): one transaction with SET LOCAL app.workspace_id for Postgres RLS. See §5.
"""
