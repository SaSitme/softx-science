import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("SOFTX_DATA", ROOT.parent / "instance")).resolve()
TABLES = ("publication", "pdf_version", "material", "device", "device_layer", "measurement", "measurement_result",
          "source_reference", "result_source", "plot_dataset", "plot_source", "validation_issue", "issue_result",
          "issue_source", "audit_log", "result_plot")


def key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


@contextmanager
def db():
    connection = sqlite3.connect(DATA / "softx.sqlite", timeout=30, detect_types=sqlite3.PARSE_DECLTYPES)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


sqlite3.register_converter("JSON", json.loads)


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript((ROOT / "schema.sql").read_text())
        for table,field,kind in (("measurement_result","calculation_inputs","JSON"),("plot_dataset","stroke_width_pdf","REAL"),("measurement_result","duplicate_of","TEXT REFERENCES measurement_result(id)"),("measurement_result","note","TEXT")):
            fields={row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if field not in fields:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {field} {kind}")
        if "pv_extraction_enabled" not in {row["name"] for row in connection.execute("PRAGMA table_info(pdf_version)")}:
            connection.execute("ALTER TABLE pdf_version ADD COLUMN pv_extraction_enabled INTEGER CHECK(pv_extraction_enabled IN (0,1))")
        connection.execute("""CREATE VIEW IF NOT EXISTS active_pv_result AS
            SELECT r.*,r.rowid AS record_order FROM measurement_result r JOIN pdf_version f ON f.id=r.pdf_version_id
            WHERE f.pv_extraction_enabled IS NOT 0""")


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value, (tuple, list, dict)) else value


def insert(connection, table, **data):
    columns = ", ".join(data)
    placeholders = ", ".join("?" for _ in data)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                       tuple(map(encode, data.values())))


def rows(connection, sql, *args):
    return [dict(row) for row in connection.execute(sql, args)]


def one(connection, sql, *args):
    return next(iter(rows(connection, sql, *args)), None)


def edit(connection, table, entity, changes, author="local-reviewer"):
    for field, value in changes.items():
        if entity[field] == value:
            continue
        connection.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (encode(value), entity["id"]))
        insert(connection, "audit_log", entity_type=table, entity_id=entity["id"], field_name=field,
               old_value=json.dumps(entity[field], ensure_ascii=False), new_value=json.dumps(value, ensure_ascii=False),
               author=author)
