# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file serverless API deployed to Vercel that acts as a server inventory backend, backed by a Neon (managed Postgres) database. All logic lives in `api/index.py`.

## Architecture

- **Entry point**: `api/index.py` defines a class named `handler` that subclasses `http.server.BaseHTTPRequestHandler`. Vercel's `@vercel/python` runtime requires this exact class name and base class to route requests to it — do not rename it or change the invocation pattern.
- **Routing**: there is no framework/router. `do_GET`, `do_POST`, and `do_DELETE` manually inspect `urlparse(self.path).path` and dispatch to private `_handle_*` methods. New endpoints must be added by extending these `do_*` methods with additional path checks.
- **Database**: a single `servers` table (`name` PK, `ip`, `location`, `status`, `last_report`) is created via `CREATE TABLE IF NOT EXISTS` in `ensure_servers_table()`, which runs lazily on the first request of each function instance (never at import time — import-time DB work makes a briefly unreachable database fail the whole invocation). There is no migration framework — schema changes mean editing this function directly.
- **Connections**: a single `psycopg2` connection is cached at module level (`_conn`) and reused across warm invocations; `get_db_connection()` reconnects only when the cached connection is closed/stale. Handlers must NOT close the connection; on a database error they call `_reset_connection()` so the next request reconnects cleanly. Keep this pattern for new endpoints — per-request connects to Neon pay a full TLS handshake each time.
- **Auth**: only `GET /api/inventory` is protected, via a static bearer token check against the `API_KEY` env var (`Authorization: Bearer <API_KEY>`). `POST /api/report` (device check-in) and `DELETE /api/delete/<name>` are currently unauthenticated.

## Endpoints

- `POST /api/report` — upsert a server's inventory record. Body: `{"name": ..., "ip": ..., "location"?: ..., "status"?: ...}`. Uses `INSERT ... ON CONFLICT (name) DO UPDATE`.
- `GET /api/inventory` — list all servers as JSON. Requires `Authorization: Bearer <API_KEY>`.
- `DELETE /api/delete/<server_name>` — delete a server by name (path segment after `/api/delete/`).

## Related repos

- The client that consumes this API is a Go app at `/home/wolf/wolf-inv/go-inv-app` (separate repo, not a subdirectory here). Changes to endpoint paths, request/response shapes, or auth here likely require matching changes there.

## Required environment variables

- `DATABASE_URL` — Neon Postgres connection string.
- `API_KEY` — bearer token required for `GET /api/inventory`.

## Deployment / config

- Deployment target is Vercel; routing and build config live in `vercel.json` (single build pointing at `api/index.py`, Python 3.9 runtime, all paths routed to it).
- Dependencies are listed in `requirements.txt` (`psycopg2-binary`); Vercel installs them via `pip install -r requirements.txt`.
- There is no local dev server, test suite, or linter configured in this repo — verification happens by deploying to Vercel and hitting the live endpoints (e.g. with `curl`).
