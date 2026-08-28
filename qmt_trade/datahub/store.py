"""本地列存（Parquet）。日线/因子等历史数据只追加不修改，天然适合 PIT 快照语义。

DuckDB 为可选依赖：装了就用它做跨文件 SQL 查询，没装就退化为 pandas 过滤，
功能不受影响（ADR-5 的务实落地）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

from ..core.logging import get_logger

logger = get_logger("datahub.store")

try:  # pragma: no cover - 可选依赖
    import duckdb  # type: ignore

    HAS_DUCKDB = True
except Exception:  # pragma: no cover
    duckdb = None  # type: ignore
    HAS_DUCKDB = False


class ParquetStore:
    """按 ``dataset/key.parquet`` 组织的简单列存。

    - ``upsert`` 以主键去重后整体重写单个分片（单机日频场景足够快）
    - ``read`` 支持日期范围与列裁剪
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 路径
    def path_of(self, dataset: str, key: str = "_all") -> Path:
        d = self.root / dataset
        d.mkdir(parents=True, exist_ok=True)
        safe = str(key).replace("/", "_").replace("\\", "_")
        return d / f"{safe}.parquet"

    def exists(self, dataset: str, key: str = "_all") -> bool:
        return self.path_of(dataset, key).exists()

    def list_keys(self, dataset: str) -> list[str]:
        d = self.root / dataset
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.parquet"))

    # ------------------------------------------------------------ 读写
    def write(self, dataset: str, df: pd.DataFrame, key: str = "_all") -> Path:
        path = self.path_of(dataset, key)
        df.to_parquet(path, index=False)
        return path

    def upsert(
        self,
        dataset: str,
        df: pd.DataFrame,
        key: str = "_all",
        *,
        primary_keys: Sequence[str] = ("date",),
        sort_by: Sequence[str] | None = None,
    ) -> Path:
        """按主键去重合并。新数据覆盖旧数据。"""
        path = self.path_of(dataset, key)
        if df is None or df.empty:
            return path
        if path.exists():
            old = pd.read_parquet(path)
            merged = pd.concat([old, df], ignore_index=True)
        else:
            merged = df.copy()
        pks = [c for c in primary_keys if c in merged.columns]
        if pks:
            merged = merged.drop_duplicates(subset=pks, keep="last")
        order = list(sort_by) if sort_by else pks
        order = [c for c in order if c in merged.columns]
        if order:
            merged = merged.sort_values(order).reset_index(drop=True)
        merged.to_parquet(path, index=False)
        return path

    def read(
        self,
        dataset: str,
        key: str = "_all",
        *,
        start: str | None = None,
        end: str | None = None,
        date_col: str = "date",
        columns: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        path = self.path_of(dataset, key)
        if not path.exists():
            return pd.DataFrame()
        df = pd.read_parquet(path, columns=list(columns) if columns else None)
        if date_col in df.columns and (start or end):
            ts = pd.to_datetime(df[date_col])
            if start:
                df = df.loc[ts >= pd.Timestamp(start)]
            if end:
                df = df.loc[ts <= pd.Timestamp(end)]
            df = df.reset_index(drop=True)
        return df

    def read_many(
        self,
        dataset: str,
        keys: Iterable[str],
        *,
        start: str | None = None,
        end: str | None = None,
        date_col: str = "date",
    ) -> pd.DataFrame:
        frames = [self.read(dataset, k, start=start, end=end, date_col=date_col) for k in keys]
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def delete(self, dataset: str, key: str = "_all") -> None:
        path = self.path_of(dataset, key)
        if path.exists():
            path.unlink()

    # ------------------------------------------------------------ SQL（可选）
    def sql(self, query: str) -> pd.DataFrame:  # pragma: no cover - 依赖可选
        """对 Parquet 目录执行 SQL。需要 duckdb，未安装时抛出提示。"""
        if not HAS_DUCKDB:
            raise RuntimeError("未安装 duckdb，无法使用 SQL 查询；请改用 read()/read_many()")
        con = duckdb.connect()
        try:
            return con.execute(query).fetchdf()
        finally:
            con.close()
