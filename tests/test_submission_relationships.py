from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from softx import store
from softx.extract import Extraction
from softx.export import relational_snapshot
from softx.submission import export_rows
from softx.pdf import Line, Span


def text_row(y, *cells):
    return Line(1, [Span(value, [[x+i, y, x+i+1, y+8] for i in range(len(value))])
                    for x, value in cells])


class SubmissionRelationships(TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.addCleanup(store.workspace.reset, store.workspace.set(Path(temp)))
        store.initialize()

    def test_labelled_table_export_links_cohort_and_preserves_numeric_evidence(self):
        lines = [
            text_row(340, (20, "Experimental single-junction perovskite cells were fabricated.")),
            text_row(302, (20, "Table 1. Photovoltaic results.")),
            text_row(282, (20, "SnO2 (from)"), (100, "Voc (mV)"), (160, "PCE (%)")),
            text_row(260, (20, "Method A"), (100, "840"), (160, "10.3 ± 0.4 (10.9)")),
            text_row(242, (20, "Method B"), (100, "850"), (160, "11.0 ± 0.2 (11.4)")),
        ]
        with store.db() as connection:
            store.insert(connection, "publication", id="paper", title="Test")
            store.insert(connection, "pdf_version", id="version", publication_id="paper", sha256="version",
                         filename="sample.pdf", processing_status="completed")
            extraction = Extraction(connection, "paper", "version", lines)
            self.assertTrue(extraction.discover({"/Title": "Experimental perovskite devices"}))
            extraction.parse()
            self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")), [])
        data, _, report = relational_snapshot()
        export = export_rows(data, report)
        self.assertEqual({r["device_label"] for r in export["devices"]}, {"SnO2 from Method A", "SnO2 from Method B"})
        measurement, = export["measurements"]
        self.assertEqual(measurement["device_id"], sorted(r["device_id"] for r in export["devices"]))
        self.assertEqual(measurement["measurement_type"], "J-V")
        self.assertEqual({(r["raw_value"].strip(), r["normalized_value"]) for r in export["results"]
                          if r["metric_name"] == "Voc"}, {("840", .84), ("850", .85)})
        self.assertEqual({(r["normalized_value"], r["aggregation"], r["spread_value"]) for r in export["results"]
                          if r["metric_name"] == "PCE"},
                         {(10.3, "mean", .4), (10.9, "champion", None), (11, "mean", .2), (11.4, "champion", None)})
        self.assertTrue(all(r["source_table"] == "1" and r["source_page"] == 1 for r in export["results"]))

    def test_migration_backfills_existing_measurement_owner(self):
        with store.db() as connection:
            store.insert(connection, "publication", id="old", title="Old")
            store.insert(connection, "device", id="device", publication_id="old", device_label="Old device")
            store.insert(connection, "measurement", id="measurement", device_id="device", measurement_type="JV")
            connection.execute("DROP TABLE measurement_device")
        store.initialize()
        store.initialize()
        with store.db() as connection:
            self.assertEqual(store.rows(connection, "SELECT * FROM measurement_device"),
                             [{"measurement_id": "measurement", "device_id": "device"}])
            self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")), [])

    def test_refresh_projects_reparsed_entities_and_preserves_manual_history(self):
        from softx.app import finish_processing

        lines = [
            text_row(340, (20, "Experimental single-junction perovskite cells were fabricated.")),
            text_row(302, (20, "Table 1. Photovoltaic results.")),
            text_row(282, (20, "Sample"), (100, "Voc (V)"), (160, "PCE (%)")),
            text_row(260, (20, "A"), (100, "0.84"), (160, "-33")),
            text_row(200, (20, "Eg = -1.6 eV.")),
        ]
        with store.db() as connection:
            store.insert(connection, "publication", id="paper", title="Test")
            store.insert(connection, "pdf_version", id="version", publication_id="paper", sha256="version",
                         filename="sample.pdf", processing_status="processing")
            for stale in ("fabrication", "reviewed"):
                store.insert(connection, "device", id=stale, publication_id="paper", device_label=stale)
                store.insert(connection, "measurement_result", id=stale, publication_id="paper",
                             pdf_version_id="version", metric_name="Voc", raw_value="0.9",
                             raw_unit="V", raw_record={"original": "0.9 V"}, normalized_value=.9,
                             normalized_unit="V", value_state="numeric", device_id=stale,
                             extraction_status="Подтверждено пользователем" if stale=="reviewed" else "Извлечено автоматически",
                             manual=int(stale=="reviewed"))
            extraction = Extraction(connection, "paper", "version", lines)
            self.assertTrue(extraction.discover({"/Title": "Experimental perovskite devices"}))
            extraction.parse()
            finish_processing(connection, extraction, [], [])
            stored = store.rows(connection, """SELECT id,raw_value,superseded FROM measurement_result
                WHERE id IN ('fabrication','reviewed') ORDER BY id""")
            self.assertEqual(stored,[{"id":"fabrication","raw_value":"0.9","superseded":1},
                                     {"id":"reviewed","raw_value":"0.9","superseded":0}])
            invalid = store.rows(connection, """SELECT raw_value,extraction_status FROM active_pv_result
                WHERE normalized_value < 0 ORDER BY metric_name""")
            self.assertEqual({row["raw_value"].strip() for row in invalid}, {"-1.6","-33"})
            self.assertTrue(all(row["extraction_status"]=="Требует проверки" for row in invalid))
        data, _, report = relational_snapshot()
        exported = export_rows(data, report)
        self.assertEqual({row["device_label"] for row in exported["devices"]}, {"A", "reviewed"})
        self.assertEqual(len([row for row in exported["issues"] if row["check_name"] == "range"]), 2)
        self.assertEqual({row["normalized_value"] for row in exported["results"] if row["normalized_value"] < 0},
                         {-33,-1.6})
