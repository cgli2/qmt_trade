"""Runtime storage uses DuckDB; legacy imports retain the Database interface."""
from .duckdb import Database

__all__ = ["Database"]
