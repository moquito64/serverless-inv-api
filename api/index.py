# api/index.py
# Serverless function for Vercel that acts as an API endpoint for a server
# inventory, using a Neon Postgres database for persistence.
#
# Performance notes:
# - The database connection is cached at module level and reused across warm
#   invocations. Opening a TLS connection to Neon is the single most expensive
#   step of a request, so paying it only on cold starts (or after the
#   connection drops) matters far more than any query tuning here.
# - Table creation is deferred to the first request instead of import time,
#   so a briefly unreachable database can no longer fail the whole import
#   (FUNCTION_INVOCATION_FAILED) and cold starts skip one extra roundtrip.

import hmac
import json
import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

DATABASE_URL = os.environ.get("DATABASE_URL")
API_KEY = os.environ.get("API_KEY")

# Connection cached for the lifetime of the (warm) function instance.
_conn = None
_table_ready = False


def get_db_connection():
    """Returns the cached connection, reconnecting only if it's gone stale."""
    global _conn
    if not DATABASE_URL:
        logging.error("DATABASE_URL environment variable is not set.")
        raise ValueError("DATABASE_URL environment variable is not set.")

    if _conn is not None and _conn.closed == 0:
        return _conn

    try:
        _conn = psycopg2.connect(
            DATABASE_URL,
            connect_timeout=5,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
        logging.info("Opened new database connection.")
        return _conn
    except psycopg2.OperationalError as e:
        logging.error(f"Operational error while connecting to PostgreSQL: {e}")
        raise
    except Exception as e:
        logging.error(f"An unexpected error occurred during database connection: {e}")
        raise


def _reset_connection():
    """Drops the cached connection so the next request reconnects cleanly."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None


def ensure_servers_table():
    """Creates the 'servers' table once per function instance."""
    global _table_ready
    if _table_ready:
        return
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                name VARCHAR(255) PRIMARY KEY,
                ip VARCHAR(45) NOT NULL,
                location VARCHAR(255),
                status VARCHAR(50),
                last_report TIMESTAMP
            );
        """)
    conn.commit()
    _table_ready = True
    logging.info("Servers table ensured to exist.")


class handler(BaseHTTPRequestHandler):
    """
    Vercel expects a class named 'handler' that inherits from
    BaseHTTPRequestHandler. We override do_GET, do_POST and do_DELETE
    to handle our API endpoints.
    """

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/inventory":
            self._handle_get_inventory()
        else:
            self._send_404()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/report":
            self._handle_report()
        else:
            self._send_404()

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/delete/"):
            server_name = unquote(path.split("/api/delete/", 1)[1])
            self._handle_delete_server(server_name)
        else:
            self._send_404()

    # --- Private Handler Methods ---
    def _send_response(self, status_code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_404(self):
        self._send_response(404, {"error": "Endpoint not found."})

    def _check_auth(self):
        """Constant-time bearer-token check against API_KEY."""
        if not API_KEY:
            logging.error("API_KEY environment variable is not set.")
            return False
        auth = self.headers.get('Authorization') or ""
        return hmac.compare_digest(auth, f'Bearer {API_KEY}')

    def _handle_report(self):
        try:
            content_length = int(self.headers.get('Content-Length') or 0)
            if content_length <= 0:
                self._send_response(400, {"error": "Missing request body."})
                return
            server_data = json.loads(self.rfile.read(content_length))

            if "name" not in server_data or "ip" not in server_data:
                self._send_response(400, {"error": "Missing required fields (name, ip)"})
                return

            ensure_servers_table()
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO servers (name, ip, location, status, last_report)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        ip = EXCLUDED.ip,
                        location = EXCLUDED.location,
                        status = EXCLUDED.status,
                        last_report = EXCLUDED.last_report;
                """, (
                    server_data["name"],
                    server_data["ip"],
                    server_data.get("location", "Unknown"),
                    server_data.get("status", "Online"),
                    datetime.now(timezone.utc),
                ))
            conn.commit()
            logging.info(f"Received and updated data for server: {server_data['name']}")
            self._send_response(200, {"message": f"Server {server_data['name']} data updated successfully."})

        except (json.JSONDecodeError, ValueError):
            self._send_response(400, {"error": "Invalid JSON payload or missing fields."})
        except (Exception, psycopg2.Error) as error:
            logging.error(f"Error handling report: {error}")
            _reset_connection()
            self._send_response(500, {"error": "Failed to update server data due to a database error."})

    def _handle_get_inventory(self):
        if not self._check_auth():
            logging.warning("Unauthorized access attempt to /api/inventory")
            self._send_response(401, {"error": "Unauthorized"})
            return

        try:
            ensure_servers_table()
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT name, ip, location, status, last_report FROM servers;")
                servers_list = cur.fetchall()
            conn.commit()

            for server in servers_list:
                if server['last_report'] is not None:
                    server['last_report'] = server['last_report'].isoformat()

            self._send_response(200, servers_list)

        except (Exception, psycopg2.Error) as error:
            logging.error(f"Error getting inventory: {error}")
            _reset_connection()
            self._send_response(500, {"error": "Failed to retrieve inventory."})

    def _handle_delete_server(self, server_name):
        try:
            ensure_servers_table()
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM servers WHERE name = %s;", (server_name,))
                rows_deleted = cur.rowcount
            conn.commit()

            if rows_deleted > 0:
                logging.info(f"Server {server_name} deleted successfully.")
                self._send_response(200, {"message": f"Server {server_name} deleted successfully."})
            else:
                logging.warning(f"Attempted to delete non-existent server: {server_name}")
                self._send_response(404, {"error": "Server not found."})

        except (Exception, psycopg2.Error) as error:
            logging.error(f"Error deleting server: {error}")
            _reset_connection()
            self._send_response(500, {"error": "Failed to delete server."})
