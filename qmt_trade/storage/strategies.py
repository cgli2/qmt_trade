"""Structured instances and immutable published parameter versions."""
import json
import time


class StrategyRepository:
    def __init__(self, db):
        self.db = db
        db.executescript("""
        CREATE TABLE IF NOT EXISTS strategy_instances (
          id VARCHAR PRIMARY KEY, strategy_id VARCHAR NOT NULL, name VARCHAR,
          enabled BOOLEAN NOT NULL DEFAULT false, active_version VARCHAR,
          running_version VARCHAR, created_at DOUBLE, draft_json VARCHAR);
        CREATE TABLE IF NOT EXISTS strategy_versions (
          instance_id VARCHAR, id VARCHAR, params_json VARCHAR NOT NULL,
          note VARCHAR, published_at DOUBLE, PRIMARY KEY(instance_id,id));
        """)

    def list(self):
        result = self.db.query("SELECT * FROM strategy_instances ORDER BY created_at,id")
        for item in result:
            item["draft"] = json.loads(item.pop("draft_json") or "{}")
            versions = self.db.query("SELECT * FROM strategy_versions WHERE instance_id=? ORDER BY published_at,id", [item["id"]])
            for version in versions:
                version["params"] = json.loads(version.pop("params_json"))
            item["versions"] = versions
        return result

    def save(self, items):
        with self.db.transaction():
            for item in items:
                if item.get("enabled") and not item.get("active_version"):
                    raise ValueError("启用前必须发布配置版本")
                if item.get("enabled"):
                    other = self.db.query_one(
                        "SELECT id, active_version FROM strategy_instances WHERE strategy_id=? AND enabled AND id<>?",
                        [item["strategy_id"], item["id"]],
                    )
                    if other:
                        same_version = other.get("active_version") == item.get("active_version")
                        hint = "（同一策略同一版本）" if same_version else ""
                        raise ValueError(f"同一策略只允许启用一个实例{hint}，已存在启用实例 {other['id']}，请先停用或删除")
                for version in item.get("versions", []):
                    data = {"instance_id": item["id"], "id": version["id"], "params_json": json.dumps(version["params"], sort_keys=True, ensure_ascii=False),
                            "note": version.get("note", ""), "published_at": version["published_at"]}
                    old = self.db.query_one("SELECT params_json FROM strategy_versions WHERE instance_id=? AND id=?", [item["id"], version["id"]])
                    if old and old["params_json"] != data["params_json"]:
                        raise ValueError("已发布版本不可修改")
                    if not old:
                        self.db.insert("strategy_versions", data)
                row = {k: item.get(k) for k in ("id", "strategy_id", "name", "active_version", "running_version", "created_at")}
                row.update(enabled=bool(item.get("enabled")), draft_json=json.dumps(item.get("draft", {}), ensure_ascii=False))
                self.db.insert("strategy_instances", row, replace=True)

    def delete(self, instance_id):
        with self.db.transaction():
            self.db.delete("strategy_versions", "instance_id=?", [instance_id])
            self.db.delete("strategy_instances", "id=?", [instance_id])

    def apply_at_boundary(self, context):
        from ..core.config import _deep_merge
        with self.db.transaction():
            items = self.list()
            overlays = {}
            for item in items:
                version = next((v for v in item["versions"] if v["id"] == item["active_version"]), None)
                if version:
                    params = _deep_merge(version["params"], {"enabled": item["enabled"]})
                    if item["strategy_id"] not in overlays or item["enabled"]:
                        overlays[item["strategy_id"]] = params
            if overlays:
                # Only strategy sections are replaced. Hard risk/execution
                # components remain attached to the same trading context.
                context.settings = context.settings.merged({"strategies": overlays})
            for item in items:
                if item["active_version"]:
                    self.db.update("strategy_instances", {"running_version": item["active_version"]}, "id=?", [item["id"]])
