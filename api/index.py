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
import psycopg2.extras # Import for DictCursor
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
        # Connect to the database and return the connection object
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

# --- Serverless Function Handler ---

def handler(request):
    """
    Main handler function for the Vercel serverless API.
    It routes requests based on the URL path and HTTP method.
    """
    path = urlparse(request.url).path
    method = request.method
    
    try:
        # Check for the /api path. The vercel.json routes all requests to this file,
        # so we need to handle the specific endpoints here.
        if path == "/api/report" and method == "POST":
            return handle_report(request)
        elif path == "/api/inventory" and method == "GET":
            return handle_get_inventory()
        elif path.startswith("/api/delete/") and method == "DELETE":
            server_name = path.split("/api/delete/")[1]
            return handle_delete_server(server_name)
        else:
            # This is the 404 handler for any path that doesn't match our API routes.
            return {
                "statusCode": 404,
                "body": json.dumps({"error": "Endpoint not found."}),
                "headers": {"Content-Type": "application/json"}
            }

    except Exception as e:
        # Catch any unexpected errors from the routing logic itself
        logging.error(f"An unexpected error occurred in handler: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal server error."}),
            "headers": {"Content-Type": "application/json"}
        }

def handle_report(request):
    """Handles the /api/report POST request to add or update server data."""
    conn = None
    try:
        # Parse the JSON body from the request
        server_data = json.loads(request.body)
        
        # Ensure the required fields are present
        if "name" not in server_data or "ip" not in server_data:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Missing required fields (name, ip)"}),
                "headers": {"Content-Type": "application/json"}
            }

        conn = get_db_connection()
        with conn.cursor() as cur:
            # SQL query to insert or update the server data
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
        return {
            "statusCode": 200,
            "body": json.dumps({"message": f"Server {server_data['name']} data updated successfully."}),
            "headers": {"Content-Type": "application/json"}
        }

    except (json.JSONDecodeError, KeyError) as e:
        logging.error(f"Invalid JSON payload or missing key: {e}")
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Invalid JSON payload or missing fields."}),
            "headers": {"Content-Type": "application/json"}
        }
    except (Exception, psycopg2.Error) as error:
        logging.error(f"Error handling report: {error}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to update server data due to a database error."}),
            "headers": {"Content-Type": "application/json"}
        }
    finally:
        if conn:
            conn.close()

def handle_get_inventory():
    """Handles the /api/inventory GET request to retrieve all servers."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM servers;")
            servers = cur.fetchall()
            
            # Convert the list of row objects to a list of dictionaries
            servers_list = [dict(row) for row in servers]

            # Fix last_report to be JSON serializable
            for server in servers_list:
                if 'last_report' in server and server['last_report'] is not None:
                    server['last_report'] = server['last_report'].isoformat()
            
            return {
                "statusCode": 200,
                "body": json.dumps(servers_list),
                "headers": {"Content-Type": "application/json"}
            }

    except (Exception, psycopg2.Error) as error:
        logging.error(f"Error getting inventory: {error}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to retrieve inventory."}),
            "headers": {"Content-Type": "application/json"}
        }
    finally:
        if conn:
            conn.close()

def handle_delete_server(server_name):
    """Handles the /api/delete/<server_name> DELETE request."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM servers WHERE name = %s;", (server_name,))
            rows_deleted = cur.rowcount
        conn.commit()
        
        if rows_deleted > 0:
            logging.info(f"Server {server_name} deleted successfully.")
            return {
                "statusCode": 200,
                "body": json.dumps({"message": f"Server {server_name} deleted successfully."}),
                "headers": {"Content-Type": "application/json"}
            }
        else:
            logging.warning(f"Attempted to delete non-existent server: {server_name}")
            return {
                "statusCode": 404,
                "body": json.dumps({"error": "Server not found."}),
                "headers": {"Content-Type": "application/json"}
            }

    except (Exception, psycopg2.Error) as error:
        logging.error(f"Error deleting server: {error}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to delete server."}),
            "headers": {"Content-Type": "application/json"}
        }
    finally:
        if conn:
            conn.close()

