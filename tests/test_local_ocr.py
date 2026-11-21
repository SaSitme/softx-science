from types import SimpleNamespace
from unittest import TestCase, mock

from PIL import Image

from softx.ocr import figure_annotations, recognize
from softx.pdf import Line, Span
from softx.vision import vision_suggestions


class LocalOCR(TestCase):
    def test_word_height_preserves_table_row_and_pdf_coordinates(self):
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        words = [
            "5\t1\t1\t1\t1\t1\t11\t20\t64\t15\t98\tJsc",
            "5\t1\t2\t1\t1\t1\t85\t16\t100\t22\t98\t(mA/cm2)",
            "5\t1\t2\t1\t2\t1\t85\t65\t100\t16\t98\t23.8",
        ]
        output = SimpleNamespace(stdout=(header+"\n".join(words)).encode())
        with mock.patch("softx.ocr.subprocess.run",return_value=output) as engine:
            lines = list(recognize(Image.new("RGB",(200,100),"white"),3,(40,50,140,100)))

        self.assertEqual([line.text for line in lines],["Jsc (mA/cm2)","23.8"])
        self.assertEqual(lines[1].span.bbox,[82.5,59.5,132.5,67.5])
        self.assertTrue(lines[1].ocr)
        self.assertIn("--oem 1", " ".join(engine.call_args.args[0]))

    def test_chart_annotations_do_not_turn_axis_ticks_into_results(self):
        def cell(text):
            return Span(text,[[0,0,10,10]]*len(text))

        lines = [Line(1,[cell("PCE (%) 20")],ocr=True),
                 Line(1,[cell("PCE = 17.2%"),cell("FF (%) 50")],ocr=True)]
        result = list(figure_annotations(lines))
        self.assertEqual([line.text for line in result],["PCE = 17.2%"])
        self.assertTrue(result[0].image_annotation)

    def test_vision_accepts_only_locally_anchored_values_and_units(self):
        def cell(text,box):
            return Span(text,[box]*len(text))
        heading=Line(1,[cell("Photovoltaic efficiency (%)",[100,90,112,96])],ocr=True)
        row=Line(1,[cell("17.2",[102,70,115,76])],ocr=True)
        evidence=[(None,{},(10,10,200,200),False,[heading,row])]
        with (mock.patch("softx.vision.image_url",return_value="data:image/jpeg;base64,sample"),
              mock.patch("softx.vision.deepseek_response",return_value={"0":{"metric":"PCE","unit":0},
                                                                       "1":{"metric":"PCE","unit":0}})):
            results=vision_suggestions(evidence,covered=lambda line,span:False)
        self.assertEqual([(metric,span.text,unit.text) for metric,_,span,_,_,unit in results],[("PCE","17.2","%")])
