from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from softx import domain, store
from softx.extract import Extraction, number
from softx.ocr import prefer_supported_units
from softx.pdf import Line, Span


def line(y, *cells):
    return Line(1, [Span(text, [[x+i, y, x+i+1, y+8] for i in range(len(text))])
                    for x, text in cells])


class RussianArticles(TestCase):
    def test_russian_corpus_boundaries(self):
        self.assertEqual(domain.classify(
            "Изготовлены однопереходные перовскитные солнечные элементы. КПД составил 21,5%.",
            "Перовскиты",
        )[0], "included")
        self.assertEqual(domain.classify(
            "Тандемные перовскитные солнечные элементы: КПД составил 28%.", "Перовскиты",
        )[0], "excluded_tandem")
        self.assertEqual(domain.classify(
            "Солнечные элементы. Чисто теоретический расчёт без изготовления устройства.",
            "Перовскиты",
        )[0], "excluded_theoretical")
        self.assertFalse(domain.has_photovoltaic_context("КПД блока питания составил 92,6%.", None))

    def test_cyrillic_units_and_decimal_commas(self):
        value, unit = domain.normalize("Voc", 945, "мВ")
        self.assertAlmostEqual(value, .945)
        self.assertEqual(unit, "V")
        self.assertEqual(domain.normalize("Jsc", 23.4, "мА см⁻²"), (23.4, "mA/cm²"))
        self.assertEqual(domain.normalize("Pin", 100, "мВт/см²"), (100, "mW/cm²"))
        self.assertEqual(domain.normalize("Eg", 1.57, "эВ"), (1.57, "eV"))
        self.assertEqual(number("0,945"), .945)
        self.assertEqual(number("20,500", decimal_comma=True), 20.5)
        self.assertEqual(number("20,500"), 20500)

    def test_ocr_keeps_the_pass_with_a_recognized_unit_at_each_position(self):
        observed_voltage = line(120, (10, "Напряжение холостого хода: 945 мВ"))
        refined_voltage = line(120, (10, "Напряжение холостого хода: 945 MB?"))
        observed_current = line(100, (10, "Плотность тока короткого замыкания: 23,4 MA/cM?"))
        refined_current = line(100, (10, "Плотность тока короткого замыкания: 23,4 мА/см2"))
        unrelated = line(70, (10, "КПД: 25%"))
        self.assertEqual(
            list(prefer_supported_units([observed_voltage, observed_current],
                                        [refined_voltage, refined_current, unrelated])),
            [observed_voltage, refined_current],
        )

    def test_russian_table_headings_keep_samples_and_printed_units(self):
        self.addCleanup(store.workspace.reset, store.workspace.set(Path(self.enterContext(TemporaryDirectory()))))
        store.initialize()
        lines = [
            line(140, (10, "Таблица 1. Сравнение образцов")),
            line(120, (10, "Параметр"), (100, "Образец А"), (200, "Образец Б"), (300, "Ед. изм.")),
            line(100, (10, "КПД"), (100, "20,500"), (200, "21,6"), (300, "%")),
            line(80, (10, "Напряжение холостого хода"), (100, "945"), (200, "960"), (300, "мВ")),
        ]
        with store.db() as connection:
            store.insert(connection,"publication",id="paper",title="Тест")
            store.insert(connection,"pdf_version",id="pdf",publication_id="paper",sha256="pdf",
                         filename="test.pdf",processing_status="completed")
            Extraction(connection,"paper","pdf",lines).parse()
            values = [tuple(row) for row in connection.execute("""SELECT d.device_label,r.metric_name,
                r.raw_value,r.raw_unit,r.normalized_unit FROM measurement_result r
                JOIN device d ON r.device_id=d.id ORDER BY 1,2""")]
            self.assertEqual(values,[
                ("Образец А","PCE","20,500","%","%"),
                ("Образец А","Voc","945","мВ","V"),
                ("Образец Б","PCE","21,6","%","%"),
                ("Образец Б","Voc","960","мВ","V"),
            ])
            self.assertEqual(connection.execute("""SELECT normalized_value FROM measurement_result
                WHERE raw_value='20,500'""").fetchone()[0],20.5)
