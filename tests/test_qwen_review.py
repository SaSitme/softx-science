from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from softx import domain, store
from softx.extract import Extraction, UNIT, VALUE
from softx.pdf import Line, Span
from softx.qwen_review import approve_qwen_evidence


class QwenReview(TestCase):
    def test_requires_real_evidence_and_preserves_manual_status(self):
        self.addCleanup(store.workspace.reset,store.workspace.set(Path(self.enterContext(TemporaryDirectory()))))
        store.initialize()
        with store.db() as connection:
            store.insert(connection,"publication",id="paper",title="Synthetic article")
            store.insert(connection,"pdf_version",id="pdf",publication_id="paper",sha256="pdf",
                         filename="test.pdf",processing_status="completed")
            examples=[
                ("We measured 73.2%, which represents the filling factor.", "FF"),
                ("We measured 74.2%, which represents the filling factor.", "PCE"),
                ("The relative improvement observed was 15%, which represents the filling factor.", "FF"),
                ("The device yielded 75.2%, which represents the filling factor.", "FF"),
            ]
            for index,(sentence,metric) in enumerate(examples):
                line=Line(1,[Span(sentence,[[x,100-index*30,x+1,109-index*30] for x in range(len(sentence))])])
                extraction=Extraction(connection,"paper","pdf",[line])
                value=VALUE.search(sentence)
                unit=UNIT.match(sentence,value.end())
                extraction.add_prose_candidate([(0,line)],metric,value,unit,domain.AI_EXTRACTION_METHODS["local"])
                if index==3:
                    connection.execute("UPDATE measurement_result SET manual=1 WHERE raw_value='75.2'")
                approve_qwen_evidence(extraction)
            records=store.rows(connection,"""SELECT raw_value,metric_name,extraction_status,value_state
                FROM measurement_result ORDER BY CAST(raw_value AS REAL)""")
            self.assertEqual([r["extraction_status"] for r in records],
                             [domain.REVIEW,domain.AUTO,domain.REVIEW,domain.REVIEW])
            self.assertEqual(records[1]["value_state"],"numeric")
            self.assertEqual([r["raw_value"] for r in records],["15","73.2","74.2","75.2"])
