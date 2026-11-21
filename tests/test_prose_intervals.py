from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from softx import store
from softx.extract import Extraction
from softx.pdf import Line, Span


def printed_line(text, y):
    return Line(1,[Span(text,[[x,y,x+1,y+9] for x in range(len(text))])])


class ProseIntervals(TestCase):
    def test_published_intervals_preserve_both_sources_and_do_not_create_scalar_results(self):
        self.addCleanup(store.workspace.reset,store.workspace.set(Path(self.enterContext(TemporaryDirectory()))))
        store.initialize()
        with store.db() as connection:
            store.insert(connection,"publication",id="paper",title="Synthetic article")
            store.insert(connection,"pdf_version",id="pdf",publication_id="paper",sha256="pdf",
                         filename="test.pdf",processing_status="completed")
            lines = [
                printed_line("The fill factor (FF) improved from 56 to",100),
                printed_line("58%, and the voltage (Voc) from 0.94 to 1.05.",85),
                printed_line("The FF changed by 12%.",70),
                printed_line("The FF varied from 15 to 20 min.",62),
                printed_line("In another series the FF ranged between 15% and 20%.",55),
            ]
            extraction = Extraction(connection,"paper","pdf",lines)
            extraction.extra_prose()
            extraction.extra_prose()
            records = store.rows(connection,"""SELECT metric_name,metric_unit,range_min,range_max,normalized_value
                FROM measurement_result ORDER BY metric_name,range_min""")
            self.assertEqual(records,[
                dict(metric_name="FF",metric_unit="%",range_min=15,range_max=20,normalized_value=None),
                dict(metric_name="FF",metric_unit="%",range_min=56,range_max=58,normalized_value=None),
                dict(metric_name="Voc",metric_unit=None,range_min=.94,range_max=1.05,normalized_value=None),
            ])
            references = store.rows(connection,"""SELECT s.source_fragment FROM source_reference s
                JOIN result_source k ON k.source_reference_id=s.id JOIN measurement_result r ON r.id=k.result_id
                WHERE r.range_min=56 AND s.fragment_role='value' ORDER BY s.source_fragment""")
            self.assertEqual(references,[{"source_fragment":"56"},{"source_fragment":"58"}])
