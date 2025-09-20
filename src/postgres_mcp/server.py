# ruff: noqa: B008
import argparse
import asyncio
import csv
import json
import logging
import os
import signal
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from typing import List
from typing import Literal
from typing import Union
from urllib.parse import urlparse

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from pydantic import validate_call

from postgres_mcp.index.dta_calc import DatabaseTuningAdvisor

from .artifacts import ErrorResult
from .artifacts import ExplainPlanArtifact
from .database_health import DatabaseHealthTool
from .database_health import HealthType
from .explain import ExplainPlanTool
from .index.index_opt_base import MAX_NUM_INDEX_TUNING_QUERIES
from .index.llm_opt import LLMOptimizerTool
from .index.presentation import TextPresentation
from .sql import DbConnPool
from .sql import SafeSqlDriver
from .sql import SqlDriver
from .sql import check_hypopg_installation_status
from .sql import obfuscate_password
from .top_queries import TopQueriesCalc

# Initialize FastMCP with default settings
mcp = FastMCP("postgres-mcp")

# Constants
PG_STAT_STATEMENTS = "pg_stat_statements"
HYPOPG_EXTENSION = "hypopg"

# Global variable for tool identifier
tool_identifier = ""

ResponseType = List[types.TextContent | types.ImageContent | types.EmbeddedResource]

logger = logging.getLogger(__name__)


class AccessMode(str, Enum):
    """SQL access modes for the server."""

    UNRESTRICTED = "unrestricted"  # Unrestricted access
    RESTRICTED = "restricted"  # Read-only with safety features


# Global variables
db_connection = DbConnPool()
current_access_mode = AccessMode.UNRESTRICTED
shutdown_in_progress = False
output_directory = None
host_output_directory = None  # Host path for reporting to user
result_row_limit = 100  # Default limit before writing to file


async def get_sql_driver() -> Union[SqlDriver, SafeSqlDriver]:
    """Get the appropriate SQL driver based on the current access mode."""
    base_driver = SqlDriver(conn=db_connection)

    if current_access_mode == AccessMode.RESTRICTED:
        logger.debug("Using SafeSqlDriver with restrictions (RESTRICTED mode)")
        return SafeSqlDriver(sql_driver=base_driver, timeout=30)  # 30 second timeout
    else:
        logger.debug("Using unrestricted SqlDriver (UNRESTRICTED mode)")
        return base_driver


def format_text_response(text: Any) -> ResponseType:
    """Format a text response."""
    return [types.TextContent(type="text", text=str(text))]


def format_error_response(error: str) -> ResponseType:
    """Format an error response."""
    return format_text_response(f"Error: {error}")


def extract_hostname_from_db_url(database_url: str) -> str:
    """Extract hostname from database URL and format it as a tool identifier."""
    try:
        parsed = urlparse(database_url)
        hostname = parsed.hostname
        if hostname:
            # Clean hostname for use as identifier: replace dots and dashes with underscores
            clean_hostname = hostname.replace(".", "_").replace("-", "_")
            return f"{clean_hostname}_"
        return ""
    except Exception:
        return ""


def get_tool_name(base_name: str) -> str:
    """Generate a tool name with the configured identifier prefix."""
    if tool_identifier:
        return f"{tool_identifier}{base_name}"
    return base_name


def generate_unique_filename(base_name: str = "query_results") -> str:
    """Generate a unique filename with timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # Include milliseconds
    return f"{base_name}_{timestamp}.csv"


async def write_results_to_file(results: list, filename: str) -> bool:
    """Write query results to a CSV file in the output directory."""
    if not output_directory or not results:
        return False
    
    try:
        output_path = Path(output_directory)
        output_path.mkdir(parents=True, exist_ok=True)
        
        file_path = output_path / filename
        
        # Get column names from the first row (assuming all rows have same structure)
        if isinstance(results[0], dict):
            column_names = list(results[0].keys())
        else:
            # If results are not dictionaries, create generic column names
            column_names = [f"column_{i}" for i in range(len(results[0]))]
        
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=column_names)
            writer.writeheader()
            
            for row in results:
                if isinstance(row, dict):
                    # Convert all values to strings to handle any data type
                    row_data = {k: str(v) if v is not None else '' for k, v in row.items()}
                    writer.writerow(row_data)
                else:
                    # Handle non-dict rows by creating a dict with generic column names
                    row_data = {f"column_{i}": str(v) if v is not None else '' for i, v in enumerate(row)}
                    writer.writerow(row_data)
        
        logger.info(f"Query results written to CSV: {file_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to write results to CSV file {filename}: {e}")
        return False


def register_tools():
    """Register all MCP tools with their dynamic names."""
    # Register all tools with their prefixed names
    mcp.add_tool(
        list_schemas,
        name=get_tool_name("list_schemas"),
        description="List all schemas in the database"
    )
    
    mcp.add_tool(
        list_objects,
        name=get_tool_name("list_objects"),
        description="List objects in a schema"
    )
    
    mcp.add_tool(
        get_object_details,
        name=get_tool_name("get_object_details"),
        description="Show detailed information about a database object"
    )
    
    mcp.add_tool(
        explain_query,
        name=get_tool_name("explain_query"),
        description="Explains the execution plan for a SQL query, showing how the database will execute it and provides detailed cost estimates."
    )
    
    mcp.add_tool(
        analyze_workload_indexes,
        name=get_tool_name("analyze_workload_indexes"),
        description="Analyze frequently executed queries in the database and recommend optimal indexes"
    )
    
    mcp.add_tool(
        analyze_query_indexes,
        name=get_tool_name("analyze_query_indexes"),
        description="Analyze a list of (up to 10) SQL queries and recommend optimal indexes"
    )
    
    mcp.add_tool(
        analyze_db_health,
        name=get_tool_name("analyze_db_health"),
        description="Analyzes database health. Here are the available health checks:\n"
        "- index - checks for invalid, duplicate, and bloated indexes\n"
        "- connection - checks the number of connection and their utilization\n"
        "- vacuum - checks vacuum health for transaction id wraparound\n"
        "- sequence - checks sequences at risk of exceeding their maximum value\n"
        "- replication - checks replication health including lag and slots\n"
        "- buffer - checks for buffer cache hit rates for indexes and tables\n"
        "- constraint - checks for invalid constraints\n"
        "- all - runs all checks\n"
        "You can optionally specify a single health check or a comma-separated list of health checks. The default is 'all' checks."
    )
    
    mcp.add_tool(
        get_top_queries,
        name=get_tool_name("get_top_queries"),
        description=f"Reports the slowest or most resource-intensive queries using data from the '{PG_STAT_STATEMENTS}' extension."
    )
    
    # Add the execute_sql tool with a description appropriate to the access mode
    if current_access_mode == AccessMode.UNRESTRICTED:
        mcp.add_tool(execute_sql, name=get_tool_name("execute_sql"), description="Execute any SQL query")
    else:
        mcp.add_tool(execute_sql, name=get_tool_name("execute_sql"), description="Execute a read-only SQL query")


# Remove decorator - will be added dynamically
async def list_schemas() -> ResponseType:
    """List all schemas in the database."""
    try:
        sql_driver = await get_sql_driver()
        rows = await sql_driver.execute_query(
            """
            SELECT
                schema_name,
                schema_owner,
                CASE
                    WHEN schema_name LIKE 'pg_%' THEN 'System Schema'
                    WHEN schema_name = 'information_schema' THEN 'System Information Schema'
                    ELSE 'User Schema'
                END as schema_type
            FROM information_schema.schemata
            ORDER BY schema_type, schema_name
            """
        )
        schemas = [row.cells for row in rows] if rows else []
        return format_text_response(schemas)
    except Exception as e:
        logger.error(f"Error listing schemas: {e}")
        return format_error_response(str(e))


# Remove decorator - will be added dynamically
async def list_objects(
    schema_name: str = Field(description="Schema name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
) -> ResponseType:
    """List objects of a given type in a schema."""
    try:
        sql_driver = await get_sql_driver()

        if object_type in ("table", "view"):
            table_type = "BASE TABLE" if object_type == "table" else "VIEW"
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = {} AND table_type = {}
                ORDER BY table_name
                """,
                [schema_name, table_type],
            )
            objects = (
                [{"schema": row.cells["table_schema"], "name": row.cells["table_name"], "type": row.cells["table_type"]} for row in rows]
                if rows
                else []
            )

        elif object_type == "sequence":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type
                FROM information_schema.sequences
                WHERE sequence_schema = {}
                ORDER BY sequence_name
                """,
                [schema_name],
            )
            objects = (
                [{"schema": row.cells["sequence_schema"], "name": row.cells["sequence_name"], "data_type": row.cells["data_type"]} for row in rows]
                if rows
                else []
            )

        elif object_type == "extension":
            # Extensions are not schema-specific
            rows = await sql_driver.execute_query(
                """
                SELECT extname, extversion, extrelocatable
                FROM pg_extension
                ORDER BY extname
                """
            )
            objects = (
                [{"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]} for row in rows]
                if rows
                else []
            )

        else:
            return format_error_response(f"Unsupported object type: {object_type}")

        return format_text_response(objects)
    except Exception as e:
        logger.error(f"Error listing objects: {e}")
        return format_error_response(str(e))


# Remove decorator - will be added dynamically
async def get_object_details(
    schema_name: str = Field(description="Schema name"),
    object_name: str = Field(description="Object name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
) -> ResponseType:
    """Get detailed information about a database object."""
    try:
        sql_driver = await get_sql_driver()

        if object_type in ("table", "view"):
            # Get columns
            col_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = {} AND table_name = {}
                ORDER BY ordinal_position
                """,
                [schema_name, object_name],
            )
            columns = (
                [
                    {
                        "column": r.cells["column_name"],
                        "data_type": r.cells["data_type"],
                        "is_nullable": r.cells["is_nullable"],
                        "default": r.cells["column_default"],
                    }
                    for r in col_rows
                ]
                if col_rows
                else []
            )

            # Get constraints
            con_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT tc.constraint_name, tc.constraint_type, kcu.column_name
                FROM information_schema.table_constraints AS tc
                LEFT JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = {} AND tc.table_name = {}
                """,
                [schema_name, object_name],
            )

            constraints = {}
            if con_rows:
                for row in con_rows:
                    cname = row.cells["constraint_name"]
                    ctype = row.cells["constraint_type"]
                    col = row.cells["column_name"]

                    if cname not in constraints:
                        constraints[cname] = {"type": ctype, "columns": []}
                    if col:
                        constraints[cname]["columns"].append(col)

            constraints_list = [{"name": name, **data} for name, data in constraints.items()]

            # Get indexes
            idx_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = {} AND tablename = {}
                """,
                [schema_name, object_name],
            )

            indexes = [{"name": r.cells["indexname"], "definition": r.cells["indexdef"]} for r in idx_rows] if idx_rows else []

            result = {
                "basic": {"schema": schema_name, "name": object_name, "type": object_type},
                "columns": columns,
                "constraints": constraints_list,
                "indexes": indexes,
            }

        elif object_type == "sequence":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type, start_value, increment
                FROM information_schema.sequences
                WHERE sequence_schema = {} AND sequence_name = {}
                """,
                [schema_name, object_name],
            )

            if rows and rows[0]:
                row = rows[0]
                result = {
                    "schema": row.cells["sequence_schema"],
                    "name": row.cells["sequence_name"],
                    "data_type": row.cells["data_type"],
                    "start_value": row.cells["start_value"],
                    "increment": row.cells["increment"],
                }
            else:
                result = {}

        elif object_type == "extension":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT extname, extversion, extrelocatable
                FROM pg_extension
                WHERE extname = {}
                """,
                [object_name],
            )

            if rows and rows[0]:
                row = rows[0]
                result = {"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]}
            else:
                result = {}

        else:
            return format_error_response(f"Unsupported object type: {object_type}")

        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error getting object details: {e}")
        return format_error_response(str(e))


# Remove decorator - will be added dynamically
async def explain_query(
    sql: str = Field(description="SQL query to explain"),
    analyze: bool = Field(
        description="When True, actually runs the query to show real execution statistics instead of estimates. "
        "Takes longer but provides more accurate information.",
        default=False,
    ),
    hypothetical_indexes: list[dict[str, Any]] = Field(
        description="""A list of hypothetical indexes to simulate. Each index must be a dictionary with these keys:
    - 'table': The table name to add the index to (e.g., 'users')
    - 'columns': List of column names to include in the index (e.g., ['email'] or ['last_name', 'first_name'])
    - 'using': Optional index method (default: 'btree', other options include 'hash', 'gist', etc.)

Examples: [
    {"table": "users", "columns": ["email"], "using": "btree"},
    {"table": "orders", "columns": ["user_id", "created_at"]}
]
If there is no hypothetical index, you can pass an empty list.""",
        default=[],
    ),
) -> ResponseType:
    """
    Explains the execution plan for a SQL query.

    Args:
        sql: The SQL query to explain
        analyze: When True, actually runs the query for real statistics
        hypothetical_indexes: Optional list of indexes to simulate
    """
    try:
        sql_driver = await get_sql_driver()
        explain_tool = ExplainPlanTool(sql_driver=sql_driver)
        result: ExplainPlanArtifact | ErrorResult | None = None

        # If hypothetical indexes are specified, check for HypoPG extension
        if hypothetical_indexes and len(hypothetical_indexes) > 0:
            if analyze:
                return format_error_response("Cannot use analyze and hypothetical indexes together")
            try:
                # Use the common utility function to check if hypopg is installed
                (
                    is_hypopg_installed,
                    hypopg_message,
                ) = await check_hypopg_installation_status(sql_driver)

                # If hypopg is not installed, return the message
                if not is_hypopg_installed:
                    return format_text_response(hypopg_message)

                # HypoPG is installed, proceed with explaining with hypothetical indexes
                result = await explain_tool.explain_with_hypothetical_indexes(sql, hypothetical_indexes)
            except Exception:
                raise  # Re-raise the original exception
        elif analyze:
            try:
                # Use EXPLAIN ANALYZE
                result = await explain_tool.explain_analyze(sql)
            except Exception:
                raise  # Re-raise the original exception
        else:
            try:
                # Use basic EXPLAIN
                result = await explain_tool.explain(sql)
            except Exception:
                raise  # Re-raise the original exception

        if result and isinstance(result, ExplainPlanArtifact):
            return format_text_response(result.to_text())
        else:
            error_message = "Error processing explain plan"
            if isinstance(result, ErrorResult):
                error_message = result.to_text()
            return format_error_response(error_message)
    except Exception as e:
        logger.error(f"Error explaining query: {e}")
        return format_error_response(str(e))


# Query function declaration without the decorator - we'll add it dynamically based on access mode
async def execute_sql(
    sql: str = Field(description="SQL to run", default="all"),
) -> ResponseType:
    """Executes a SQL query against the database."""
    try:
        logger.info(f"execute_sql called with SQL: {sql[:200]}{'...' if len(sql) > 200 else ''}")
        sql_driver = await get_sql_driver()
        rows = await sql_driver.execute_query(sql)  # type: ignore
        if rows is None:
            return format_text_response("No results")
        
        results = [r.cells for r in rows]
        
        # Check if results exceed the row limit
        if len(results) > result_row_limit:
            # Generate unique filename and write full results to file
            filename = generate_unique_filename("sql_query_results")
            write_success = await write_results_to_file(results, filename)
            
            if write_success and output_directory:
                # Return first N rows plus file info
                preview_results = results[:result_row_limit]
                # Use host path for user-facing response, fallback to container path
                if host_output_directory:
                    user_file_path = Path(host_output_directory) / filename
                else:
                    user_file_path = Path(output_directory) / filename
                response_message = {
                    "message": f"Query returned {len(results)} rows (limit: {result_row_limit}). Full results written to file.",
                    "preview_rows": len(preview_results),
                    "total_rows": len(results),
                    "file_path": str(user_file_path),
                    "preview_data": preview_results
                }
                return format_text_response(response_message)
            else:
                # Fallback: just return limited results if file writing failed
                limited_results = results[:result_row_limit]
                response_message = {
                    "message": f"Query returned {len(results)} rows. Showing first {len(limited_results)} rows (file output disabled or failed).",
                    "total_rows": len(results),
                    "shown_rows": len(limited_results),
                    "data": limited_results
                }
                return format_text_response(response_message)
        else:
            # Return all results normally
            return format_text_response(results)
            
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        return format_error_response(str(e))


# Remove decorator - will be added dynamically
@validate_call
async def analyze_workload_indexes(
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    method: Literal["dta", "llm"] = Field(description="Method to use for analysis", default="dta"),
) -> ResponseType:
    """Analyze frequently executed queries in the database and recommend optimal indexes."""
    try:
        sql_driver = await get_sql_driver()
        if method == "dta":
            index_tuning = DatabaseTuningAdvisor(sql_driver)
        else:
            index_tuning = LLMOptimizerTool(sql_driver)
        dta_tool = TextPresentation(sql_driver, index_tuning)
        result = await dta_tool.analyze_workload(max_index_size_mb=max_index_size_mb)
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing workload: {e}")
        return format_error_response(str(e))


# Remove decorator - will be added dynamically
@validate_call
async def analyze_query_indexes(
    queries: list[str] = Field(description="List of Query strings to analyze"),
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    method: Literal["dta", "llm"] = Field(description="Method to use for analysis", default="dta"),
) -> ResponseType:
    """Analyze a list of SQL queries and recommend optimal indexes."""
    if len(queries) == 0:
        return format_error_response("Please provide a non-empty list of queries to analyze.")
    if len(queries) > MAX_NUM_INDEX_TUNING_QUERIES:
        return format_error_response(f"Please provide a list of up to {MAX_NUM_INDEX_TUNING_QUERIES} queries to analyze.")

    try:
        sql_driver = await get_sql_driver()
        if method == "dta":
            index_tuning = DatabaseTuningAdvisor(sql_driver)
        else:
            index_tuning = LLMOptimizerTool(sql_driver)
        dta_tool = TextPresentation(sql_driver, index_tuning)
        result = await dta_tool.analyze_queries(queries=queries, max_index_size_mb=max_index_size_mb)
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing queries: {e}")
        return format_error_response(str(e))


# Remove decorator - will be added dynamically
async def analyze_db_health(
    health_type: str = Field(
        description=f"Optional. Valid values are: {', '.join(sorted([t.value for t in HealthType]))}.",
        default="all",
    ),
) -> ResponseType:
    """Analyze database health for specified components.

    Args:
        health_type: Comma-separated list of health check types to perform.
                    Valid values: index, connection, vacuum, sequence, replication, buffer, constraint, all
    """
    health_tool = DatabaseHealthTool(await get_sql_driver())
    result = await health_tool.health(health_type=health_type)
    return format_text_response(result)


# Remove decorator - will be added dynamically
async def get_top_queries(
    sort_by: str = Field(
        description="Ranking criteria: 'total_time' for total execution time or 'mean_time' for mean execution time per call, or 'resources' "
        "for resource-intensive queries",
        default="resources",
    ),
    limit: int = Field(description="Number of queries to return when ranking based on mean_time or total_time", default=10),
) -> ResponseType:
    try:
        sql_driver = await get_sql_driver()
        top_queries_tool = TopQueriesCalc(sql_driver=sql_driver)

        if sort_by == "resources":
            result = await top_queries_tool.get_top_resource_queries()
            return format_text_response(result)
        elif sort_by == "mean_time" or sort_by == "total_time":
            # Map the sort_by values to what get_top_queries_by_time expects
            result = await top_queries_tool.get_top_queries_by_time(limit=limit, sort_by="mean" if sort_by == "mean_time" else "total")
        else:
            return format_error_response("Invalid sort criteria. Please use 'resources' or 'mean_time' or 'total_time'.")
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error getting slow queries: {e}")
        return format_error_response(str(e))


async def main():
    # Configure logging to DEBUG level to help diagnose issues
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="PostgreSQL MCP Server")
    parser.add_argument("database_url", help="Database connection URL", nargs="?")
    parser.add_argument(
        "--access-mode",
        type=str,
        choices=[mode.value for mode in AccessMode],
        default=AccessMode.UNRESTRICTED.value,
        help="Set SQL access mode: unrestricted (unrestricted) or restricted (read-only with protections)",
    )
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "sse", "http"],
        default="stdio",
        help="Select MCP transport: stdio (default), sse, or http",
    )
    parser.add_argument(
        "--sse-host",
        type=str,
        default="localhost",
        help="Host to bind SSE server to (default: localhost)",
    )
    parser.add_argument(
        "--sse-port",
        type=int,
        default=8000,
        help="Port for SSE server (default: 8000)",
    )
    parser.add_argument(
        "--tool-identifier",
        type=str,
        default="",
        help="Unique identifier prefix for tool names (e.g., 'db1_' for database 1)",
    )
    parser.add_argument(
        "--output-directory",
        type=str,
        default="",
        help="Directory to write large query results to (e.g., '/app/output' for Docker volume)",
    )
    parser.add_argument(
        "--result-row-limit",
        type=int,
        default=100,
        help="Maximum number of rows to return directly (default: 100). Larger results are written to file.",
    )
    parser.add_argument(
        "--host-output-directory",
        type=str,
        default="",
        help="Host directory path to report to user (e.g., '/Users/user/Desktop/sqloutput' for the host path)",
    )

    args = parser.parse_args()

    # Store the access mode in global variable
    global current_access_mode, tool_identifier, output_directory, host_output_directory, result_row_limit
    current_access_mode = AccessMode(args.access_mode)
    output_directory = args.output_directory if args.output_directory else None
    host_output_directory = args.host_output_directory if args.host_output_directory else output_directory
    result_row_limit = args.result_row_limit

    # Get database URL from environment variable or command line
    database_url = os.environ.get("DATABASE_URI", args.database_url)

    # Set tool identifier: use provided value or extract from database URL hostname
    if args.tool_identifier:
        tool_identifier = args.tool_identifier
    elif database_url:
        tool_identifier = extract_hostname_from_db_url(database_url)
    else:
        tool_identifier = ""

    # Register all tools with their dynamic names
    register_tools()

    logger.info(f"Starting PostgreSQL MCP Server in {current_access_mode.upper()} mode")
    if tool_identifier:
        logger.info(f"Using tool identifier prefix: '{tool_identifier}'")
    else:
        logger.info("No tool identifier prefix configured")
    
    if output_directory:
        logger.info(f"Large query results will be written to: {output_directory}")
    else:
        logger.info("No output directory configured - large results will be truncated")
    
    logger.info(f"Query result row limit: {result_row_limit}")

    if not database_url:
        raise ValueError(
            "Error: No database URL provided. Please specify via 'DATABASE_URI' environment variable or command-line argument.",
        )

    # Initialize database connection pool
    try:
        await db_connection.pool_connect(database_url)
        logger.info("Successfully connected to database and initialized connection pool")
    except Exception as e:
        logger.warning(
            f"Could not connect to database: {obfuscate_password(str(e))}",
        )
        logger.warning(
            "The MCP server will start but database operations will fail until a valid connection is established.",
        )

    # Set up proper shutdown handling
    try:
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGINT)
        for s in signals:
            loop.add_signal_handler(s, lambda s=s: asyncio.create_task(shutdown(s)))
    except NotImplementedError:
        # Windows doesn't support signals properly
        logger.warning("Signal handling not supported on Windows")
        pass

    # Run the server with the selected transport (always async)
    if args.transport == "stdio":
        await mcp.run_stdio_async()
    elif args.transport == "sse":
        # Update FastMCP settings for SSE transport
        mcp.settings.host = args.sse_host
        mcp.settings.port = args.sse_port
        await mcp.run_sse_async()
    elif args.transport == "http":
        # Update FastMCP settings for HTTP transport
        mcp.settings.host = args.sse_host  # Use same host/port settings
        mcp.settings.port = args.sse_port
        logger.info(f"Starting streamable HTTP server on http://{args.sse_host}:{args.sse_port}/mcp")
        await mcp.run_streamable_http_async()



async def shutdown(sig=None):
    """Clean shutdown of the server."""
    global shutdown_in_progress

    if shutdown_in_progress:
        logger.warning("Forcing immediate exit")
        # Use sys.exit instead of os._exit to allow for proper cleanup
        sys.exit(1)

    shutdown_in_progress = True

    if sig:
        logger.info(f"Received exit signal {sig.name}")

    # Close database connections
    try:
        await db_connection.close()
        logger.info("Closed database connections")
    except Exception as e:
        logger.error(f"Error closing database connections: {e}")

    # Exit with appropriate status code
    sys.exit(128 + sig if sig is not None else 0)
