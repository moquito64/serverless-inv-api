# api/index.py
# This is a serverless function for Vercel that acts as an API endpoint
# for a server inventory, using a Neon Postgres database for persistence.
#
# This updated version includes more robust error handling and logging
# to help diagnose FUNCTION_INVOCATION_FAILED errors.

import os
import json
from datetime import datetime
import logging
import psycopg2
import psycopg2.extras
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

# Configure logging to be more detailed
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Get the database connection string from environment variables
DATABASE_URL = os.environ.get("DATABASE_URL")

# --- Helper functions for database operations ---

def get_db_connection():
    """Establishes and returns a database connection with better error handling."""
    if not DATABASE_URL:
        logging.error("DATABASE_URL environment variable is not set.")
        raise ValueError("DATABASE_URL environment variable is not set.")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        logging.info("Successfully connected to the database.")
        return conn
    except psycopg2.OperationalError as e:
        logging.error(f"Operational error while connecting to PostgreSQL: {e}")
        raise
    except Exception as e:
        logging.error(f"An unexpected error occurred during database connection: {e}")
        raise

def create_servers_table():
    """
    Creates the 'servers' table if it does not already exist.
    This ensures our database schema is ready.
    """
    conn = None
    try:
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
        logging.info("Servers table ensured to exist.")
    except (Exception, psycopg2.Error) as error:
        logging.error(f"Error while connecting to or creating table in PostgreSQL: {error}")
    finally:
        if conn:
            conn.close()

# Ensure the table exists on startup
create_servers_table()

# --- Serverless Function Handler Class ---
class handler(BaseHTTPRequestHandler):
    """
    Vercel expects a class named 'handler' that inherits from
    BaseHTTPRequestHandler. We will override the do_GET, do_POST,
    and do_DELETE methods to handle our API endpoints.
    """

    def do_GET(self):
        """Handles GET requests to the /api/inventory endpoint."""
        path = urlparse(self.path).path
        if path == "/api/inventory":
            self._handle_get_inventory()
        else:
            self._send_404()

    def do_POST(self):
        """Handles POST requests to the /api/report endpoint."""
        path = urlparse(self.path).path
        if path == "/api/report":
            self._handle_report()
        else:
            self._send_404()

    def do_DELETE(self):
        """Handles DELETE requests to the /api/delete/<server_name> endpoint."""
        path = urlparse(self.path).path
        if path.startswith("/api/delete/"):
            server_name = path.split("/api/delete/")[1]
            self._handle_delete_server(server_name)
        else:
            self._send_404()
            
    # --- Private Handler Methods ---
    def _send_response(self, status_code, data):
        """Helper to send a JSON response."""
        self.send_response(status_code)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_404(self):
        """Helper to send a 404 Not Found response."""
        self._send_response(404, {"error": "Endpoint not found."})
    
    def _handle_report(self):
        """Handles the /api/report POST request logic."""
        conn = None
        try:
            # Read the body of the request
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            server_data = json.loads(post_data)

            if "name" not in server_data or "ip" not in server_data:
                self._send_response(400, {"error": "Missing required fields (name, ip)"})
                return

            conn = get_db_connection()
            with conn.cursor() as cur:
                sql = """
                    INSERT INTO servers (name, ip, location, status, last_report)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET
                        ip = EXCLUDED.ip,
                        location = EXCLUDED.location,
                        status = EXCLUDED.status,
                        last_report = EXCLUDED.last_report;
                """
                cur.execute(sql, (
                    server_data["name"],
                    server_data["ip"],
                    server_data.get("location", "Unknown"),
                    server_data.get("status", "Online"),
                    datetime.now()
                ))
            conn.commit()
            logging.info(f"Received and updated data for server: {server_data['name']}")
            self._send_response(200, {"message": f"Server {server_data['name']} data updated successfully."})
        
        except (json.JSONDecodeError, KeyError) as e:
            logging.error(f"Invalid JSON payload or missing key: {e}")
            self._send_response(400, {"error": "Invalid JSON payload or missing fields."})
        except (Exception, psycopg2.Error) as error:
            logging.error(f"Error handling report: {error}")
            self._send_response(500, {"error": "Failed to update server data due to a database error."})
        finally:
            if conn:
                conn.close()

    def _handle_get_inventory(self):
        """Handles the /api/inventory GET request logic."""
        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM servers;")
                servers = cur.fetchall()
                servers_list = [dict(row) for row in servers]

                for server in servers_list:
                    if 'last_report' in server and server['last_report'] is not None:
                        server['last_report'] = server['last_report'].isoformat()
                
                self._send_response(200, servers_list)

        except (Exception, psycopg2.Error) as error:
            logging.error(f"Error getting inventory: {error}")
            self._send_response(500, {"error": "Failed to retrieve inventory."})
        finally:
            if conn:
                conn.close()

    def _handle_delete_server(self, server_name):
        """Handles the /api/delete/<server_name> DELETE request logic."""
        conn = None
        try:
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
            self._send_response(500, {"error": "Failed to delete server."})
        finally:
            if conn:
                conn.close()

