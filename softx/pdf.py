"""PDF I/O and coordinate transformations independent of the scientific schema."""
import math
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject, RectangleObject


def draw(path, fmt, page=1, width=None):
    command = ["mutool", "draw", "-q", "-F", fmt, "-o", "-"]
    if width:
        command += ["-w", str(width)]
    return subprocess.run(command + [str(path), str(page)], capture_output=True, check=True, timeout=45).stdout


def prepare(path, directory):
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ValueError("PDF защищён паролем")
    if not len(reader.pages) or len(reader.pages) > 200:
        raise ValueError("Поддерживается от 1 до 200 страниц")
    directory.mkdir(exist_ok=True)
    metadata = []
    for index, page in enumerate(reader.pages, 1):
        crop, media = list(map(float, page.cropbox)), list(map(float, page.mediabox))
        box = [max(crop[0], media[0]), max(crop[1], media[1]), min(crop[2], media[2]), min(crop[3], media[3])]
        unit = float(page.user_unit)
        rotation = page.rotation % 360
        if rotation not in (0, 90, 180, 270) or not math.isfinite(unit) or unit <= 0:
            raise ValueError("Недопустимые параметры PDF-страницы")
        x0, y0, x1, y1 = box
        if x1 <= x0 or y1 <= y0:
            raise ValueError("Недопустимый CropBox")
        metadata.append(dict(page=index, crop_box=box, media_box=media, rotation=rotation, user_unit=unit,
                             width=(x1-x0)*unit, height=(y1-y0)*unit))
        try:
            writer = PdfWriter()
            clone = writer.add_page(page, excluded_keys=("/Annots",))
            clone[NameObject("/Rotate")] = NumberObject(0)
            clone.cropbox = RectangleObject(box)
            temporary = directory / f"{index}.tmp"
            with temporary.open("wb") as output:
                writer.write(output)
            temporary.replace(directory / f"{index}.pdf")
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return metadata, dict(reader.metadata or {})


def union(boxes):
    return [min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes)]


@dataclass
class Span:
    text: str
    boxes: list

    @property
    def bbox(self):
        return union(self.boxes)

    def part(self, start, end):
        return Span(self.text[start:end], self.boxes[start:end])


@dataclass
class Line:
    page: int
    cells: list
    ocr: bool = False
    image_annotation: bool = False

    @property
    def span(self):
        text = " ".join(cell.text for cell in self.cells)
        boxes = []
        for cell in self.cells:
            if boxes:
                boxes.append(cell.boxes[0])
            boxes.extend(cell.boxes)
        return Span(text, boxes)

    @property
    def text(self):
        return self.span.text


def read_lines(path, meta):
    root = ET.fromstring(draw(path, "stext"))
    x0, _, _, y1 = meta["crop_box"]
    unit = meta["user_unit"]
    baselines = []
    for element in root.iter("line"):
        if not element.get("dir", "1 0").startswith("1 "):
            continue
        characters = []
        for char in element.iter("char"):
            coords = list(map(float, char.get("quad").split()))
            xs, ys = coords[::2], coords[1::2]
            box = [x0+min(xs)/unit, y1-max(ys)/unit, x0+max(xs)/unit, y1-min(ys)/unit]
            characters.extend((symbol, box) for symbol in char.get("c", ""))
        if characters:
            baseline = float(next(element.iter("char")).get("y")) / unit
            group = next((chars for y, chars in reversed(baselines) if abs(y-baseline) < 1.7), None)
            if group is None:
                baselines.append((baseline, characters))
            else:
                group.extend(characters)
    result = []
    for _, characters in sorted(baselines):
        cells, current = [], []
        for symbol, box in sorted(characters, key=lambda item: item[1][0]):
            if current and box[0] - current[-1][1][2] > 12:
                cells.append(current)
                current = []
            if current and box[0] - current[-1][1][2] > 1.8 and not current[-1][0].isspace() and not symbol.isspace():
                current.append((" ", current[-1][1]))
            current.append((symbol, box))
        if current:
            cells.append(current)
        spans = []
        for cell in cells:
            text = "".join(char for char, _ in cell)
            start, end = len(text)-len(text.lstrip()), len(text.rstrip())
            if end > start:
                spans.append(Span(text[start:end], [box for _, box in cell[start:end]]))
        if spans:
            result.append(Line(meta["page"], spans))
    # Detect a repeated gutter between substantial text spans, then read columns independently.
    gutters=[]
    for line in result:
        for left,right in zip(line.cells,line.cells[1:]):
            if len(left.text)>35 and len(right.text)>35 and right.bbox[0]-left.bbox[2]>8:
                gutters.append((left.bbox[2]+right.bbox[0])/2)
    if len(gutters)>=8:
        gutters.sort()
        gutter=gutters[len(gutters)//2]
        left_lines,right_lines=[],[]
        for line in result:
            left=[s for s in line.cells if s.bbox[0]<gutter]
            right=[s for s in line.cells if s.bbox[0]>=gutter]
            if left:
                left_lines.append(Line(line.page,left))
            if right:
                right_lines.append(Line(line.page,right))
        result=left_lines+right_lines
    return result


def page_file(directory, page):
    return Path(directory) / "pages" / f"{page}.pdf"
