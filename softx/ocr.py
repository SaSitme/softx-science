"""Local OCR for scans and image-only figures or tables."""
import csv
import io
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from contextlib import ExitStack

from PIL import Image

from .domain import CYRILLIC, INTENSITY_UNIT, normalize
from .extract import METRIC, UNIT, prose_metrics
from .graphs import raster_boxes
from .pdf import Line, Span, draw


CAPTION = re.compile(r"^(?:Table|Figure|Fig\.|Таблица|Рисунок|Рис\.)\s+(?:№\s*)?\d", re.I)
ANNOTATION = re.compile(r"\s*[=:≈]\s*[+−-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)\s*")


def figure_annotations(lines):
    for line in lines:
        cells = []
        for cell in line.cells:
            for metric in METRIC.finditer(cell.text):
                value = ANNOTATION.match(cell.text, metric.end())
                if value and UNIT.match(cell.text, value.end()):
                    cells.append(cell)
                    break
        if cells:
            yield Line(line.page, cells, ocr=True, image_annotation=True)


def recognize(image, page, box):
    pixels = io.BytesIO()
    image.save(pixels, format="PNG")
    command = [os.getenv("SOFTX_OCR_COMMAND", "tesseract"), "stdin", "stdout",
               "-l", os.getenv("SOFTX_OCR_LANG", "eng"), "--oem", "1", "--psm", "11", "tsv"]
    response = subprocess.run(command, input=pixels.getvalue(), capture_output=True, check=True,
                              timeout=35, env={**os.environ, "OMP_THREAD_LIMIT": "2"})
    left, bottom, right, top = box
    sx, sy = (right-left)/image.width, (top-bottom)/image.height
    words = []
    for row in csv.DictReader(io.StringIO(response.stdout.decode("utf-8")), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if not text or row["level"] != "5" or float(row["conf"]) < 45:
            continue
        x, y, w, h = (int(row[name]) for name in ("left", "top", "width", "height"))
        words.append((text, [left+x*sx, top-(y+h)*sy, left+(x+w)*sx, top-y*sy]))
    rows = []
    for word in sorted(words, key=lambda item: -(item[1][1]+item[1][3])):
        _, lower, _, upper = word[1]
        if not rows or all(
            min(upper, other[3])-max(lower, other[1]) < .5*min(upper-lower, other[3]-other[1])
            for _, other in rows[-1]
        ):
            rows.append([])
        rows[-1].append(word)
    for row in rows:
        cells, text, boxes = [], "", []
        for word, rect in sorted(row, key=lambda item: item[1][0]):
            if boxes and rect[0]-boxes[-1][2] > 12:
                cells.append(Span(text, boxes))
                text, boxes = "", []
            if boxes:
                text += " "
                boxes.append(boxes[-1])
            text += word
            boxes.extend([rect]*len(word))
        yield Line(page, [*cells, Span(text, boxes)], ocr=True)


def image_regions(path, meta, lines):
    captions = [(index, line.span.bbox, line.text.lower().startswith(("table","таблица")))
                for index, line in enumerate(lines) if CAPTION.match(line.text)]
    if not captions:
        return
    images = ET.fromstring(draw(path, "trace"))
    used = set()
    for index, (left, bottom, right, top), table in captions:
        regions = []
        for box in raster_boxes(images, meta):
            a,b,c,d = box
            below = 0 <= bottom-d <= 60
            above = 0 <= b-top <= 60
            if (below or above) and min(right,c)-max(left,a) >= .4*min(right-left,c-a):
                regions.append((bottom-d if below else b-top, tuple(box)))
        if not regions:
            continue
        _, box = min(regions)
        if box in used:
            continue
        used.add(box)
        if sum(box[0] <= (span[0]+span[2])/2 <= box[2] and box[1] <= (span[1]+span[3])/2 <= box[3]
               for span in (line.span.bbox for line in lines)) <= 1:
            yield index+1, box, not table


def read_region(image, page_box, box, page):
    left, bottom, right, top = page_box
    sx, sy = image.width/(right-left), image.height/(top-bottom)
    a, b, c, d = box
    x0, y0, x1, y1 = (round(value) for value in ((a-left)*sx, (top-d)*sy, (c-left)*sx, (top-b)*sy))
    if x0 >= x1 or y0 >= y1:
        return [], box
    crop_box = (left+x0/sx, top-y1/sy, left+x1/sx, top-y0/sy)
    return list(recognize(image.crop((x0,y0,x1,y1)), page, crop_box)), crop_box


def recognized_measurements(line):
    count = 0
    for metric, value, _ in prose_metrics(line.text):
        unit = UNIT.match(line.text[value.end():].lstrip())
        if unit and normalize(metric, 1, unit.group())[0] is not None:
            count += 1
    return count + int(bool(re.search(INTENSITY_UNIT, line.text)))


def prefer_supported_units(observed, refined):
    # OCR passes can disagree on superscripts or Cyrillic letters. Compare only
    # lines in the same position and prefer a recognized metric with its unit.
    for line in observed:
        a, b, c, d = line.span.bbox
        candidates = []
        for other in refined:
            e, f, g, h = other.span.bbox
            if (min(d,h)-max(b,f) >= .6*min(d-b,h-f)
                    and min(c,g)-max(a,e) >= .7*min(c-a,g-e)):
                candidates.append(other)
        if candidates:
            best = max(candidates, key=recognized_measurements)
            if recognized_measurements(best) > recognized_measurements(line):
                yield best
                continue
        yield line


def read_ocr(path, meta, native, *, vision_regions=None):
    page_box = meta["crop_box"]
    regions = list(image_regions(path, meta, native)) if native else [(0, page_box, False)]
    if not regions:
        return native
    languages = os.getenv("SOFTX_OCR_LANG", "eng").split("+")
    with ExitStack() as images:
        page_image = images.enter_context(Image.open(io.BytesIO(draw(path, "png", width=1900))))
        inserted = {}
        high_resolution = None
        for index, box, figure in regions:
            observed, crop_box = read_region(page_image, page_box, box, meta["page"])
            if ("rus" in languages and sum(len(CYRILLIC.findall(line.text)) for line in observed) >= 20):
                try:
                    if high_resolution is None:
                        high_resolution = images.enter_context(
                            Image.open(io.BytesIO(draw(path, "png", width=2300))))
                    refined, _ = read_region(high_resolution, page_box, box, meta["page"])
                except (OSError, ValueError, subprocess.SubprocessError):
                    refined = []
                if refined:
                    observed = list(prefer_supported_units(observed, refined))
            if vision_regions is not None and observed:
                vision_regions.append((path, meta, crop_box, figure, observed))
            inserted.setdefault(index, []).extend(figure_annotations(observed) if figure else observed)
    return [line for index in range(len(native)+1)
            for line in [*inserted.get(index, ()), *native[index:index+1]]]
