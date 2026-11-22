from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from softx import domain, store
from softx.extract import Extraction, key, number, prose_metrics
from softx.pdf import Line, Span


def span(text, left=0, bottom=0):
    return Span(text, [[left+i, bottom, left+i+1, bottom+8] for i in range(len(text))])


class PdfNotation(TestCase):
    def test_requires_a_reported_value_and_distinguishes_retention(self):
        cases = {
            "Efficiency retention is expressed as η(t)/η(0).": [],
            "PCE = 0": [],
            "PCE = 0%": [("PCE", "0")],
            "η = 21,5%; efficiency retention = 91%": [
                ("PCE", "21,5"), ("retained_PCE", "91")
            ],
            "Nₜ = 8 × 10¹⁴ cm⁻³; τ = 0.8 μs; EQE = 83%": [
                ("EQE", "83"), ("Nt", "8 × 10¹⁴"), ("tau", "0.8")
            ],
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual([(metric, match["n"]) for metric, match, _ in prose_metrics(source)], expected)
        self.assertEqual(number("8 × 10¹⁴"), 8e14)
        self.assertAlmostEqual(number("1.602 × 10^−19"), 1.602e-19)

    def test_retry_retires_only_machine_read_formula_arguments(self):
        self.addCleanup(store.workspace.reset, store.workspace.set(Path(self.enterContext(TemporaryDirectory()))))
        store.initialize()
        formula = "Efficiency retention is η(t)/η(0)."
        lines = [Line(1, [span(formula, bottom=y)]) for y in (100, 80)]
        lines.append(Line(1, [span("PCE = 0%", bottom=60)]))
        self.assertEqual(domain.has_photovoltaic_context(
            "Jₛ� = 23.4 mA cm⁻²; Vₒ� = 1.15 V; η = 21.5%.", None), True)
        with store.db() as db:
            store.insert(db, "publication", id="paper", title="Test")
            store.insert(db, "pdf_version", id="pdf", publication_id="paper", sha256="pdf",
                         filename="test.pdf", processing_status="completed")
            for line, manual in zip(lines, (0, 1)):
                offset = formula.index("0")
                ident = key("pdf", 1, "PCE", line.span.part(offset, offset+1).bbox)
                store.insert(db, "measurement_result", id=ident, publication_id="paper", pdf_version_id="pdf",
                             metric_name="PCE", metric_value=0, raw_value="0", raw_record={"value": "0"}, manual=manual,
                             extraction_method="pdf_text", extraction_status=domain.REVIEW,
                             value_state="ambiguous")
            parser = Extraction(db, "paper", "pdf", lines)
            parser.parse()
            parser.parse()
            self.assertEqual(
                [tuple(row) for row in db.execute("""SELECT deleted,manual,raw_value,raw_unit,normalized_value
                     FROM measurement_result ORDER BY rowid""")],
                [(1, 0, "0", None, None), (0, 1, "0", None, None), (0, 0, "0", "%", 0)],
            )
            self.assertEqual(db.execute("SELECT count(*) FROM audit_log WHERE field_name='deleted'").fetchone()[0], 1)

    def test_numeric_prose_in_two_columns_is_not_a_table_header(self):
        self.addCleanup(store.workspace.reset, store.workspace.set(Path(self.enterContext(TemporaryDirectory()))))
        store.initialize()
        source = Line(1, [span("FF (%), a photovoltaic metric", 15, 100), span("η = 21.5%.", 300, 100)])
        with store.db() as db:
            store.insert(db, "publication", id="paper", title="Test")
            store.insert(db, "pdf_version", id="pdf", publication_id="paper", sha256="pdf",
                         filename="test.pdf", processing_status="completed")
            Extraction(db, "paper", "pdf", [source]).parse()
            records = store.rows(db, """SELECT r.metric_name,r.normalized_value,s.source_fragment,s.bbox
                FROM measurement_result r JOIN result_source l ON l.result_id=r.id
                JOIN source_reference s ON s.id=l.source_reference_id WHERE s.fragment_role='value'""")
            self.assertEqual(records,[dict(metric_name="PCE", normalized_value=21.5,
                                           source_fragment="21.5", bbox=[304,100,308,108])])
