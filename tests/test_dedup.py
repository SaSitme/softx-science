from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from softx import domain, store
from softx.dedup import consolidate, flag_reading_conflicts


class DuplicateResults(TestCase):
    def test_consolidates_sources_but_preserves_distinct_readings(self):
        directory = self.enterContext(TemporaryDirectory())
        self.addCleanup(store.workspace.reset,store.workspace.set(Path(directory)))
        store.initialize()
        with store.db() as connection:
            store.insert(connection,"publication",id="pub",title="PV")
            store.insert(connection,"pdf_version",id="pdf",publication_id="pub",sha256="pdf",
                         filename="test.pdf",processing_status="completed")
            for device in ("a","b"):
                store.insert(connection,"device",id=device,publication_id="pub",device_label=device)
            for name,device,scan in (("initial","a","forward"),("repeat","a","reverse"),("other","b","forward")):
                store.insert(connection,"measurement",id=name,device_id=device,scan_direction=scan)

            def result(id,method,device,measurement,box,extra=False,value=18):
                store.insert(connection,"measurement_result",id=id,publication_id="pub",pdf_version_id="pdf",
                             metric_name="PCE",metric_value=value,normalized_value=value,normalized_unit="%",metric_unit="%",
                             value_state="numeric",raw_record={},extraction_status=domain.AUTO,
                             extraction_method=method,device_id=device,measurement_id=measurement)
                for role in ("value","unit") if extra else ("value",):
                    source = id+"_"+role
                    store.insert(connection,"source_reference",id=source,publication_id="pub",pdf_version_id="pdf",
                                 source_page=1,bbox=box,source_type="table_cell",fragment_role=role)
                    store.insert(connection,"result_source",result_id=id,source_reference_id=source)

            result("table","geometry_table","a","initial",[30,70,55,80])
            result("ocr","local_ocr","a","initial",[31,71,53,81],extra=True)
            result("qwen","local_ai_pdf_quote","a","initial",[32,72,51,80])
            result("different_scan","geometry_table","a","repeat",[30,70,55,80])
            result("different_device","geometry_table","b","other",[30,70,55,80])
            result("different_cell","geometry_table","a","initial",[60,70,75,80])
            for _ in range(2):
                consolidate(connection,"pdf")
            visible={row["id"] for row in store.rows(connection,"SELECT id FROM active_pv_result")}
            self.assertEqual(visible,{"table","different_scan","different_device","different_cell"})
            links={row["source_reference_id"] for row in store.rows(connection,"SELECT * FROM result_source WHERE result_id='table'")}
            self.assertEqual(links,{"table_value","ocr_value","ocr_unit","qwen_value"})
            self.assertEqual(store.one(connection,"SELECT duplicate_of FROM measurement_result WHERE id='ocr'")["duplicate_of"],"table")

            result("disagreement","local_ocr","a","initial",[31,71,53,81],value=19)
            for _ in range(2):
                flag_reading_conflicts(connection,"pdf")
            active = {row["id"]:row for row in store.rows(connection,"SELECT * FROM active_pv_result")}
            self.assertEqual(active["table"]["extraction_status"],domain.REVIEW)
            self.assertEqual(active["disagreement"]["extraction_status"],domain.REVIEW)
            self.assertEqual(active["table"]["note"].count("19.0 %"),1)
            self.assertIn("18.0 %",active["disagreement"]["note"])
            for id in ("different_scan","different_device","different_cell"):
                self.assertEqual(active[id]["extraction_status"],domain.AUTO)
            self.assertEqual(active["table"]["normalized_value"],18)
