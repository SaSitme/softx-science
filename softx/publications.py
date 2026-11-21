"""Remove a publication and its evidence from one workspace."""
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from pathlib import Path

from flask import abort

from .store import data_dir, db, rows


def delete_publication(publication_id):
    with TemporaryDirectory(prefix=".removed-", dir=data_dir()) as quarantine, ExitStack() as restore:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            versions = rows(connection, "SELECT id,sha256,processing_status FROM pdf_version WHERE publication_id=?", publication_id)
            if not versions:
                abort(404, "Статья не найдена")
            if any(version["processing_status"] == "processing" for version in versions):
                abort(409, "Дождитесь окончания обработки всех PDF этой статьи.")
            remove_records(connection, publication_id)
            for version in versions:
                # The storage directory is always the SHA-256 of the PDF.
                name = version["id"]
                if name != version["sha256"] or len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
                    raise ValueError("Invalid PDF storage directory")
                original = data_dir() / name
                if original.is_dir():
                    moved = Path(quarantine) / name
                    original.rename(moved)
                    restore.callback(moved.rename, original)
        restore.pop_all()


def remove_records(connection, publication_id):
    params = {"pub": publication_id}
    for table in ("publication", "pdf_version", "measurement_result", "plot_dataset", "validation_issue"):
        field = "id" if table == "publication" else "publication_id"
        connection.execute(f"""DELETE FROM audit_log WHERE entity_type=? AND entity_id IN
            (SELECT id FROM {table} WHERE {field}=?)""", (table, publication_id))

    for statement in (
        """DELETE FROM issue_source WHERE issue_id IN (SELECT id FROM validation_issue WHERE publication_id=:pub)
               OR source_reference_id IN (SELECT id FROM source_reference WHERE publication_id=:pub)""",
        """DELETE FROM issue_result WHERE issue_id IN (SELECT id FROM validation_issue WHERE publication_id=:pub)
               OR result_id IN (SELECT id FROM measurement_result WHERE publication_id=:pub)""",
        """DELETE FROM result_plot WHERE plot_id IN (SELECT id FROM plot_dataset WHERE publication_id=:pub)
               OR result_id IN (SELECT id FROM measurement_result WHERE publication_id=:pub)""",
        """DELETE FROM plot_source WHERE plot_id IN (SELECT id FROM plot_dataset WHERE publication_id=:pub)
               OR source_reference_id IN (SELECT id FROM source_reference WHERE publication_id=:pub)""",
        """DELETE FROM result_source WHERE result_id IN (SELECT id FROM measurement_result WHERE publication_id=:pub)
               OR source_reference_id IN (SELECT id FROM source_reference WHERE publication_id=:pub)""",
        """UPDATE measurement_result SET duplicate_of=NULL WHERE publication_id<>:pub AND duplicate_of
               IN (SELECT id FROM measurement_result WHERE publication_id=:pub)""",
        "DELETE FROM validation_issue WHERE publication_id=:pub",
        "DELETE FROM measurement_result WHERE publication_id=:pub",
        "DELETE FROM plot_dataset WHERE publication_id=:pub",
        "DELETE FROM device_layer WHERE device_id IN (SELECT id FROM device WHERE publication_id=:pub)",
        "DELETE FROM measurement WHERE device_id IN (SELECT id FROM device WHERE publication_id=:pub)",
        "DELETE FROM device WHERE publication_id=:pub",
        "DELETE FROM material WHERE publication_id=:pub",
        "DELETE FROM source_reference WHERE publication_id=:pub",
        "DELETE FROM pdf_version WHERE publication_id=:pub",
        """DELETE FROM publication WHERE id=:pub AND NOT EXISTS
               (SELECT 1 FROM measurement_result WHERE cited_publication_id=:pub)""",
    ):
        connection.execute(statement, params)
