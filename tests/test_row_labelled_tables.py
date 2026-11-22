from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from softx import store
from softx.extract import Extraction
from softx.pdf import Line, Span


def line(y, *cells):
    return Line(1,[Span(text,[[x+i,y,x+i+1,y+8] for i in range(len(text))]) for x,text in cells])


class RowLabelledTables(TestCase):
    def test_rows_split_by_prose_gutter_and_missing_cells_keep_their_model(self):
        self.addCleanup(store.workspace.reset,store.workspace.set(Path(self.enterContext(TemporaryDirectory()))))
        store.initialize()
        rows = [
            line(100,(10,"Parameter"),(160,"a-Si:H model")),
            line(100,(310,"CdTe model"),(460,"Unit")),
            line(82,(10,"FF"),(160,"68.0")),
            line(82,(310,"75.0"),(460,"%")),
            line(64,(10,"Voc")),
            line(64,(310,"0.82"),(460,"V")),
            line(46,(10,"τ"),(160,"0.8 µs")),
            line(46,(310,"12 ns"),(460,"s")),
        ]
        with store.db() as connection:
            store.insert(connection,"publication",id="paper",title="Synthetic test")
            store.insert(connection,"pdf_version",id="pdf",publication_id="paper",sha256="pdf",
                         filename="test.pdf",processing_status="completed")
            extraction = Extraction(connection,"paper","pdf",rows)
            extraction.parse()
            extraction.parse()
            records = store.rows(connection,"""SELECT d.device_label,r.metric_name,r.normalized_value,
                                    r.normalized_unit,r.measurement_id FROM measurement_result r
                                    JOIN device d ON r.device_id=d.id ORDER BY 1,2""")
            self.assertEqual([(r["device_label"],r["metric_name"],r["normalized_unit"]) for r in records],[
                ("CdTe model","FF","%"),("CdTe model","Voc","V"),("CdTe model","tau","s"),
                ("a-Si:H model","FF","%"),("a-Si:H model","tau","s"),
            ])
            for row,value in zip(records,[75,.82,12e-9,68,.8e-6]):
                self.assertAlmostEqual(row["normalized_value"],value,places=15)
                self.assertIsNone(row["measurement_id"])
            self.assertEqual(connection.execute("""SELECT count(*) FROM result_source l
              JOIN source_reference s ON s.id=l.source_reference_id WHERE s.fragment_role='unit'""").fetchone()[0],5)
