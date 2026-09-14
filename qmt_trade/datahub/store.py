"""DuckDB dataset store; old files are migration inputs only."""
from pathlib import Path
from ..storage.db import Database
from ..storage.market import MarketRepository
from ..storage.runtime import runtime_path

class DuckDBStore(MarketRepository):
    def __init__(self, root):
        self.root = Path(root)
        super().__init__(Database(runtime_path(self.root.parent), schema="market"))

# Import compatibility for external clients; implementation never writes Parquet.
ParquetStore = DuckDBStore
