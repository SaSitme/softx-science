"""Generate clearly labeled synthetic inputs independently of the extractor."""
import json
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import (DecodedStreamObject, DictionaryObject, NameObject, RectangleObject)

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
ROOT.mkdir(exist_ok=True)


def line(x, y, text, size=11):
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return f"BT /F1 {size} Tf {x} {y} Td ({escaped}) Tj ET\n"


def make(name, title, content, rotate=False, graph=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=620, height=790)
    if rotate:
        page.cropbox = RectangleObject((18, 20, 602, 767))
        page.rotate(90)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    notebook = line(50, 738, "SYNTHETIC TEST DOCUMENT / SOFTX", 10) + line(50, 705, title, 16) + content
    stream.set_data(notebook.encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": title, "/Author": "SoftX synthetic corpus", "/CreationDate": "D:20260101"})
    with (ROOT / name).open("wb") as output:
        writer.write(output)


def row(y, cells):
    return "".join(line(x, y, value) for x, value in zip((50, 145, 227, 310, 398, 505), cells))


points = [[i / 10, 20 * (1 - (i / 10)**4)] for i in range(11)]
figure = line(50, 292, "Figure 1(a). JV curve for B, reverse scan.")
figure += "0.15 0.19 0.25 RG 1 w 115 83 m 115 251 l 495 251 l 495 83 l h S\n"
for i in range(6):
    x = 115 + i * 76
    figure += f"{x} 83 m {x} 79 l S\n" + line(x-6, 66, f"{i/5:.1f}", 9)
for i in range(5):
    y = 83 + i * 42
    figure += f"115 {y} m 110 {y} l S\n" + line(88, y-3, str(i*5), 9)
figure += line(265, 46, "Voltage (V)", 10) + line(50, 266, "Current density (mA/cm2)", 10)
figure += "0.04 0.58 0.48 RG 1.5 w " + " ".join(
    f"{115+380*x:.2f} {83+8.4*y:.2f} {'m' if i == 0 else 'l'}" for i, (x, y) in enumerate(points)) + " S\n"

text = "".join([
    line(50, 674, "Experimental single-junction perovskite devices were fabricated."),
    line(50, 655, "Device A (control), n-i-p, active area 0.10 cm2."),
    line(50, 636, "Device B (target), p-i-n, active area 0.10 cm2."),
    line(50, 617, "Layer 1: A / ETL / SnO2 / 35 nm / spin coating."),
    line(50, 598, "Layer 2: A / absorber / MAPbI3 / 480 nm / spin coating."),
    line(50, 579, "Layer 1: B / HTL / NiOx / 30 nm / sputtering."),
    line(50, 560, "Layer 2: B / absorber / MAPbI3 / 500 nm / spin coating."),
    line(50, 541, "Eg = 1.10 eV. Bandgap method: Tauc plot."),
    line(50, 522, "All JV scans measured under AM1.5G at 100 mW/cm2."),
    line(50, 497, "Table 1. Photovoltaic results for fabricated devices.", 12),
    row(476, ("Sample", "Scan", "PCE (%)", "Voc (mV)", "Jsc (mA/cm2)", "FF (%)")),
    row(455, ("A", "forward", "18.00", "1200", "20.00", "75.00")),
    row(434, ("A", "reverse", "17.00", "1200", "20.00", "75.00")),
    row(413, ("B", "reverse", "14.00", "1000", "20.00", "70.00")),
    line(50, 390, "Device A reverse scan: Jsc_EQE = 17.00 mA/cm2."),
    line(50, 371, "PCE = 16.00 %."),
    line(50, 349, "Table 2. Comparison with literature.", 12),
    row(330, ("Reference", "Scan", "PCE (%)", "Voc (mV)", "Jsc (mA/cm2)", "FF (%)")),
    row(312, ("10.5555/sx", "reverse", "10.00", "800", "20.00", "62.50")),
    figure,
])
make("01-fabricated.pdf", "Perovskite thin films under AM1.5G", text)
make("02-rotated.pdf", "Experimental CdTe thin-film cells",
     line(50, 668, "A single-junction CdTe solar cell was fabricated.") +
     line(50, 640, "Device C (target), superstrate, active area 0.2 cm2.") +
     line(50, 610, "Device C reverse scan: PCE = 12.00 %; Voc = 800 mV; Jsc = 20 mA/cm2; FF = 75 %."),
     rotate=True)
make("03-tandem.pdf", "Perovskite tandem solar cells", line(50, 657, "Experimental tandem devices were fabricated. PCE = 24 %."))
make("04-theoretical.pdf", "Theoretical study of CIGS", line(50, 657, "Simulation-only study. No devices were fabricated. PCE = 24 %."))
(ROOT / "05-corrupted.pdf").write_bytes(b"%PDF-1.7\nSYNTHETIC TEST: deliberately truncated input\n")

expected = []
for sample, scan, values, y in (("A","forward",[18,1.2,20,75],455), ("A","reverse",[17,1.2,20,75],434),
                                ("B","reverse",[14,1,20,70],413), (None,"reverse",[10,.8,20,62.5],312)):
    for metric, value, x, unit in zip(("PCE","Voc","Jsc","FF"), values, (227,310,398,505), ("%","V","mA/cm²","%")):
        expected.append(dict(file="01-fabricated.pdf", metric=metric, value=value, device=sample, scan=scan, unit=unit,
                             origin="primary" if sample else "cited", aggregation="unknown", page=1, bbox=[x,y-5,x+37,y+13]))
for metric, value, unit in [("PCE",12,"%"),("Voc",.8,"V"),("Jsc",20,"mA/cm²"),("FF",75,"%")]:
    expected.append(dict(file="02-rotated.pdf", metric=metric, value=value, device="C",scan="reverse",unit=unit,
                         origin="primary",aggregation="unknown",page=1,bbox=[170,604,545,625]))
expected.append(dict(file="01-fabricated.pdf",metric="PCE",value=16,device=None,scan=None,unit="%",origin="primary",
                     aggregation="unknown",page=1,bbox=[50,365,140,386]))
ground_truth = dict(
    synthetic=True,
    classifications={"01-fabricated.pdf":"included","02-rotated.pdf":"included",
                     "03-tandem.pdf":"excluded_tandem","04-theoretical.pdf":"excluded_theoretical"},
    expected_metrics=expected,
    extended=[dict(file="01-fabricated.pdf",metric="Jsc_EQE",value=17,unit="mA/cm²",device="A",scan="reverse",page=1,
                   bbox=[190,383,290,404],origin="primary",aggregation="unknown"),
              dict(file="01-fabricated.pdf",metric="Eg",value=1.1,unit="eV",device=None,scan=None,page=1,
                   bbox=[50,535,122,554],origin="primary",aggregation="unknown")],
    JV={"file":"01-fabricated.pdf","page":1,"figure":"1","panel":"a","bbox":[115,83,495,251],
        "x_unit":"V","y_unit":"mA/cm²","points":points})
# Per-field truth for populated expanded schema fields in the extraction subset.
ground_truth["entity_fields"] = {
    "material": [
        {"file":"01-fabricated.pdf","key":"Перовскиты","material_name_raw":"Perovskite","formula_normalized":"MAPbI3",
         "material_class":"Перовскиты","bandgap_value":1.1,"bandgap_unit":"eV","bandgap_method":"Tauc plot"},
        {"file":"02-rotated.pdf","key":"CdTe","material_name_raw":"CdTe","formula_normalized":"CdTe","material_class":"CdTe"},
    ],
    "device": [
        {"file":"01-fabricated.pdf","key":"A","device_role":"control","device_type":"single-junction thin-film",
         "architecture":"n-i-p","active_area":.1,"area_unit":"cm²","area_definition":"active"},
        {"file":"01-fabricated.pdf","key":"B","device_role":"target","device_type":"single-junction thin-film",
         "architecture":"p-i-n","active_area":.1,"area_unit":"cm²","area_definition":"active"},
        {"file":"02-rotated.pdf","key":"C","device_role":"target","device_type":"single-junction thin-film",
         "architecture":"superstrate","active_area":.2,"area_unit":"cm²","area_definition":"active"},
    ],
    "device_layer": [
        {"file":"01-fabricated.pdf","key":"A/1","layer_order":1,"layer_function":"ETL","layer_material":"SnO2",
         "layer_thickness":35,"thickness_unit":"nm","deposition_method":"spin coating"},
        {"file":"01-fabricated.pdf","key":"A/2","layer_order":2,"layer_function":"absorber","layer_material":"MAPbI3",
         "layer_thickness":480,"thickness_unit":"nm","deposition_method":"spin coating"},
        {"file":"01-fabricated.pdf","key":"B/1","layer_order":1,"layer_function":"HTL","layer_material":"NiOx",
         "layer_thickness":30,"thickness_unit":"nm","deposition_method":"sputtering"},
        {"file":"01-fabricated.pdf","key":"B/2","layer_order":2,"layer_function":"absorber","layer_material":"MAPbI3",
         "layer_thickness":500,"thickness_unit":"nm","deposition_method":"spin coating"},
    ],
    "measurement": [
        {"file":"01-fabricated.pdf","key":f"{sample}/{scan}","scan_direction":scan,"measurement_type":"JV","spectrum":"AM1.5G",
         "light_intensity":100,"light_intensity_unit":"mW/cm²"} for sample,scan in (("A","forward"),("A","reverse"),("B","reverse"))
    ] + [{"file":"02-rotated.pdf","key":"C/reverse","scan_direction":"reverse","measurement_type":"JV"}],
}

(ROOT / "ground_truth.json").write_text(json.dumps(ground_truth, ensure_ascii=False, indent=2))

# Separate graph fixtures keep the stage-one acceptance set stable.
(ROOT / "graphs").mkdir(exist_ok=True)


def additional(kind, caption, x_label, y_label, x_values, y_values, samples, markers=False):
    x0,x1,y0,y1=125,505,210,418
    graphic=line(50,476,caption)+line(50,447,y_label)
    graphic+="0.15 0.19 0.25 RG 1 w 125 210 m 125 418 l 505 418 l 505 210 l h S\n"
    for i,value in enumerate(x_values):
        x=x0+(x1-x0)*i/(len(x_values)-1)
        graphic+=f"{x} {y0} m {x} {y0-5} l S\n"+line(x-7,y0-23,str(value),9)
    for i,value in enumerate(y_values):
        y=y0+(y1-y0)*i/(len(y_values)-1)
        graphic+=f"{x0} {y} m {x0-5} {y} l S\n"+line(x0-32,y-2,str(value),9)
    graphic+=line(260,y0-54,x_label)
    coords=[(x0+(x1-x0)*x,y0+(y1-y0)*y) for x,y in samples]
    if markers:
        graphic+=".72 .19 .52 rg "+''.join(f"{x-2:.2f} {y-2:.2f} 4 4 re f\n" for x,y in coords)
    else:
        graphic+=".12 .34 .81 RG 1.5 w "+' '.join(
            f"{x:.2f} {y:.2f} {'m' if i==0 else 'l'}" for i,(x,y) in enumerate(coords))+" S\n"
    make("graphs/"+kind+".pdf", "Graph test "+kind, line(50,671,"Experimental single-junction CdTe devices were fabricated.")+
         line(50,647,"Device D (target), superstrate, active area 0.12 cm2.")+graphic)


additional("eqe","Figure 2(b). EQE spectrum for D.","Wavelength (nm)","EQE (%)",
           [300,450,600,750,900],[0,25,50,75,100],[(0,0),(.25,.8),(.5,.87),(.75,.7),(1,0)])
additional("stability","Figure 3. Stability of D.","Time (h)","Retention (%)",
           [0,25,50,75,100],[0,25,50,75,100],[(0,1),(.25,.95),(.5,.85),(.75,.78),(1,.7)])
additional("thermal","Figure 4. Annealing temperature for D.","Temperature (C)","PCE (%)",
           [50,100,150,200],[0,5,10,15,20],[(0,.3),(.33,.5),(.66,.72),(1,.43)])
additional("pce","Figure 5. PCE versus thickness for D.","Thickness (nm)","PCE (%)",
           [100,200,300,400],[0,5,10,15,20],[(0,.39),(.3,.53),(.6,.75),(1,.72)])
additional("scatter","Figure 6. Scatter data for D.","Voltage (V)","PCE (%)",
           [.6,.7,.8,.9,1.0],[0,5,10,15,20],[(.15,.35),(.31,.52),(.68,.69),(.91,.72)],markers=True)
