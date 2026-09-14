"""Versioned user configuration; YAML supplies installation defaults only."""
import json
import time
import uuid

from .db import Database
from .runtime import runtime_path


def read_active(name, data_dir=None):
    path = runtime_path(data_dir)
    if not path.exists():
        return None
    db = Database(path, schema="meta")
    try:
        if not db.table_exists("configuration_versions"):
            return None
        row = db.query_one("SELECT payload,id FROM configuration_versions WHERE name=? ORDER BY created DESC,id DESC LIMIT 1", [name])
        return json.loads(row["payload"]) if row else None
    finally:
        db.close()


def publish(name, payload, data_dir=None):
    db = Database(runtime_path(data_dir), schema="meta")
    try:
        db.execute("CREATE TABLE IF NOT EXISTS configuration_versions (id VARCHAR PRIMARY KEY, name VARCHAR, created DOUBLE, payload VARCHAR)")
        version = uuid.uuid4().hex
        db.insert("configuration_versions", {"id": version, "name": name, "created": time.time(), "payload": json.dumps(payload, ensure_ascii=False, allow_nan=False)})
        return version
    finally:
        db.close()
