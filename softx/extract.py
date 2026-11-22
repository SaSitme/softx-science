"""Deterministic extraction from text geometry, with local evidence for every result."""
import json
import re

from . import domain as d, fabrication
from .publication_metadata import DOI, bibliographic_doi, front_matter
from .store import edit, insert, key, one, rows


PATTERN = "|".join(f"(?P<{name}>{pattern})" for name, pattern in d.METRICS.items())
METRIC = re.compile(rf"(?<![\w])(?:{PATTERN})(?![\w])", re.I)
UNIT = re.compile(r"cm\s*(?:\^?\s*[-−]\s*3|⁻³)|1\s*/\s*cm(?:\^?3|³)|(?:[µμu]s|ns|ms|ps)\b|"+d.UNIT_PATTERN)
VALUE = re.compile(rf"(?P<op><=|>=|[<>≤≥])?\s*(?P<n>{d.NUM})(?:\s*(?:±|\+/-)\s*(?P<spread>{d.NUM})|\s*(?:–|—|\bto\b|-)\s*(?P<end>{d.NUM}))?")
SCAN = re.compile(r"\b(forward|reverse|FB\s*[-–—−]\s*SC|SC\s*[-–—−]\s*FB|прям(?:ой|ого|ом)|обратн(?:ый|ого|ом))\b", re.I)
RUSSIAN_SAMPLE = r"образ(?:ец|ца)|прибор(?:а)?|устройств[оа]"
DEVICE = re.compile(rf"\b(?:device|sample|cell|{RUSSIAN_SAMPLE})\s+([A-ZА-ЯЁ0-9][\w.-]{{0,20}})(?=\s*[:(,]|\s+{SCAN.pattern})", re.I)
TABLE = re.compile(r"^(?:Table|Таблица)\s+(?:№\s*)?(\w+)\b", re.I)
FOOTNOTE = re.compile(r"^(?:[*†‡]|Note\s*:|Примечани[ея]\s*[.:]|\[[a-z]\])")
REFERENCE_HEADER = re.compile(r"(?:refs?\.?|references?|sources?|источник(?:и)?|ссылк[аи]|литература)", re.I)
OWN_WORK = re.compile(r"(?:this|present|our)\s+(?:work|study|paper)|(?:данная|настоящая|эта)\s+работа", re.I)
SAMPLE_HEADER = re.compile(rf"(?:sample|device|cell|{RUSSIAN_SAMPLE})(?:\s+(?:name|id|label))?", re.I)
FROM_HEADER = re.compile(r"(" + fabrication.CHEM + r")\s*\(\s*from\s*\)", re.I)
HEADER_WORDS = re.compile(r"(?:direction|scan|fabrication|structure|architecture|configuration|reference|material|parameters?|performance|conditions?|mean|median|average)", re.I)

NUMBERED_REFERENCE = re.compile(r"\[\s*(\d+)\s*\]")

CURRENT_DENSITY = re.compile(r"\bcurrent\s+density\s*(?:of\s+|[:=]\s*)", re.I)
SHORT_CIRCUIT_SUFFIX = re.compile(r"[^.!?;]{0,65}?\b(?:at|under)\s+(?:the\s+)?short[- ]circuit\b", re.I)
SHARED_PERCENT = re.compile(
    rf"(?<![\w.,+-])(?P<values>{d.NUM}(?:\s+(?:and|or)\s+{d.NUM}|"
    rf",\s+(?:and\s+)?{d.NUM}){{1,5}})\s*(?P<unit>%)", re.I
)
PERCENT_SERIES_CUE = re.compile(r"\b(PCEs?|power conversion efficienc(?:y|ies)|fill factors?)\b", re.I)
PERCENT_DELTA = re.compile(r"\b(?:increases?|decreases?|improvements?|reductions?|changes?|relative|deltas?)\b",re.I)


PROSE_INTERVAL = re.compile(
    rf"\s*\)?\s*(?:(?:increased|decreased|improved|rose|fell|ranged|varied)\s+)?"
    rf"(?P<prefix>from|between)\s+(?P<low>{d.NUM})\s*(?P<first_unit>{d.UNIT_PATTERN})?\s*"
    rf"(?P<join>to|and)\s+(?P<high>{d.NUM})(?![\d.,](?=\d))",
    re.I,
)


EXPLICIT_SCIENTIFIC_METRICS = (
    ("EQE", re.compile(r"(?<![\w-])(?:EQE|external quantum efficiency|ВКЭ|внешняя квантовая эффективность)\s*[:=]\s*", re.I),
     re.compile("%")),
    ("Nt", re.compile(r"(?<!\w)(?:N[ _]?[tₜ]|(?:defect|trap) density)\s*[:=]\s*"),
     re.compile(r"cm\s*(?:\^?\s*[-−]\s*3|⁻³)|1\s*/\s*cm(?:\^?3|³)")),
    ("tau", re.compile(r"(?<!\w)(?:τ|carrier lifetime)\s*[:=]\s*"),
     re.compile(r"(?:[µμu]s|ns|ms|ps|s)")),
)


def prose_metrics(text):
    value_offsets = set()
    for metric_match in METRIC.finditer(text):
        connector = re.match(
            r"\s*\)?\s*(?:\((?!\s*[+−-]?(?:\d|[.,]\d))[^)]{1,20}\)\s*)?(?:(?:[=:~≈]|of\b|was\b|is\b|(?:have\s+)?reached\b|at\b|(?:increased|decreased|improved|rose|fell|reduced)\s+to\b|составил[аио]?\b|рав(?:ен|на|но|ны)\b|достиг(?:ло|ла|ли)?\b)\s*)?\(?\s*",
            text[metric_match.end():], re.I,
        )
        value_start = metric_match.end() + connector.end()
        value = VALUE.match(text, value_start)
        if value and metric_match.lastgroup == "PCE":
            suffix = text[value.end():].lstrip()
            printed_unit = UNIT.match(suffix)
            symbol = metric_match.group().casefold() in ("η","ɳ")
            formula_arg = bool(re.fullmatch(r"\s*\(\s*",text[metric_match.end():value.start("n")]))
            if formula_arg and suffix.startswith(")"):
                continue
            if (symbol or number(value["n"])==0) and (
                    not printed_unit or d.normalize("PCE",1,printed_unit.group())[0] is None):
                continue
        if value:
            value_offsets.add(value.start("n"))
            yield metric_match.lastgroup, value, metric_match.end()

    for descriptor in CURRENT_DENSITY.finditer(text):
        value = VALUE.match(text, descriptor.end())
        if not value or value.start("n") in value_offsets:
            continue
        tail = text[value.end():]
        unit_start = value.end() + len(tail) - len(tail.lstrip())
        unit = UNIT.match(text, unit_start)
        if (unit and d.normalize("Jsc", 1, unit.group())[0] is not None
                and SHORT_CIRCUIT_SUFFIX.match(text, unit.end())):
            yield "Jsc", value, descriptor.end()


    for metric, label, unit_pattern in EXPLICIT_SCIENTIFIC_METRICS:
        for descriptor in label.finditer(text):
            value = VALUE.match(text, descriptor.end())
            if not value:
                continue
            unit = UNIT.match(text, value.end()+len(text[value.end():])-len(text[value.end():].lstrip()))
            if unit and unit_pattern.fullmatch(unit.group()):
                yield metric, value, descriptor.end()


def scan_direction(match):
    if not match:
        return None
    scan = match[1].lower()
    flags = re.sub(r"[^a-z]", "", scan)
    if flags in ("fbsc", "scfb"):
        return "reverse" if flags == "fbsc" else "forward"
    return "forward" if scan.startswith("прям") else "reverse" if scan.startswith("обратн") else scan


def number(raw, *, decimal_comma=False):
    power = re.fullmatch(rf"({d.BASE_NUM})\s*[×x·]\s*10\s*(?:\^\s*([+−-]?\d+)|([⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+))",raw)
    if power:
        exponent = (power[2] or power[3].translate(SUPERSCRIPTS)).replace("−","-")
        return number(power[1], decimal_comma=decimal_comma)*10**int(exponent)
    raw=raw.replace("−","-")
    if re.fullmatch(r"[+-]?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?",raw):
        if decimal_comma and raw.count(",") == 1 and "." not in raw:
            return float(raw.replace(",", "."))
        return float(raw.replace(",",""))
    return float(raw.replace(",", "."))


def aggregation(text):
    for token in ("champion", "mean", "median", "single"):
        if re.search(rf"\b{token}\b", text, re.I):
            return token
    return "mean" if re.search(r"\baverage\b", text, re.I) else "unknown"


def metric_unit(metric, text):
    return next((match for match in UNIT.finditer(text)
                 if d.normalize(metric, 1, match.group())[0] is not None), None)


def same_prose_column(line, label_offset, value_offset):
    start = 0
    label = None
    for cell in line.cells:
        end = start+len(cell.text)
        if start<=label_offset<end:
            label = cell
        if start<=value_offset<end:
            return label is cell or (label is not None and cell.bbox[0]-label.bbox[2]<=24)
        start = end+1
    return False


def table_headers(line):
    headers = {}
    for cell in line.cells:
        matches = list(METRIC.finditer(cell.text))
        for index, match in enumerate(matches):
            next_start = matches[index+1].start() if index+1<len(matches) else len(cell.text)
            end = match.start()+len(cell.text[match.start():next_start].rstrip())
            span = cell.part(match.start(),end)
            headers[span.bbox[0]] = (line, span, match.lastgroup)
        if not matches and re.fullmatch(r"Eff?\.?\s*(?:[\[(]\s*%\s*[\])])?", cell.text, re.I):
            headers[cell.bbox[0]] = (line, cell, "PCE")
    return headers


def table_qualifiers(cells):
    for cell in cells:
        if SCAN.fullmatch(cell.text) or UNIT.fullmatch(cell.text.strip(" ()[]")):
            yield cell
        else:
            for match in re.finditer(r"[\[(]([^\]\)]+)[\])]", cell.text):
                if UNIT.fullmatch(match[1].strip()):
                    yield cell.part(*match.span())


def column_distance(cell, header):
    return min(abs(cell.bbox[0]-header.bbox[0]),abs(sum(cell.bbox[::2])-sum(header.bbox[::2]))/2)


def wrapped_table_line(lines, index, headers, left, right):
    """Keep column anchors across wrapped labels and structures between numeric rows."""
    line = lines[index]
    if any(cell.bbox[0] < left - 8 or cell.bbox[2] > right + 12 for cell in line.cells):
        return False
    if len(line.cells) == 1 and line.cells[0].bbox[2] - line.cells[0].bbox[0] > (right-left)*.74:
        return False
    for following in lines[index+1:index+5]:
        if following.page != line.page or not 0 < line.span.bbox[1]-following.span.bbox[1] <= 48:
            break
        if len(following.cells) >= 2 and any(
            column_distance(cell, header) <= 18 and VALUE.match(cell.text)
            for cell in following.cells for _, header, _ in headers.values()
        ):
            return True
    return False


def table_values(cells, columns):
    anchors = [qualifier or header for _,header,_,_,qualifier in columns.values()]
    for cell in cells:
        words = list(re.finditer(r"\S+", cell.text))
        if len(words) > 1 and all(re.fullmatch(d.NUM, word.group()) for word in words):
            yield from (cell.part(*word.span()) for word in words)
        elif len(words)>1 and re.fullmatch(d.NUM,words[-1].group()):
            tail = cell.part(*words[-1].span())
            if (all(abs(cell.bbox[0]-anchor.bbox[0])>12 for anchor in anchors)
                    and any(column_distance(tail,anchor)<=11 for anchor in anchors)):
                yield cell.part(0,len(cell.text[:words[-1].start()].rstrip()))
                yield tail
            else:
                yield cell
        else:
            yield cell


def prose_windows(lines, max_lines=3):
    """Short connected stretches printed in the same column."""
    for index, first in enumerate(lines):
        if len(first.cells) != 1 or len(first.text) < 12:
            continue
        group = [(0, first)]
        text = first.text
        last = first
        for line in lines[index+1:index+max_lines]:
            if len(line.cells) != 1 or line.page != first.page or first.rotated or line.rotated:
                break
            left, bottom, _, _ = last.span.bbox
            x, y, _, _ = line.span.bbox
            if abs(x-left) > 16 or not 0 < bottom-y < 25:
                break
            group.append((len(text)+1, line))
            text += " " + line.text
            last = line
        yield text, group


def ordered_prose_values(text):
    for group in re.finditer(r"(?P<labels>[^.!?;]{4,150})\b(?:were|are)\s+(?P<values>[^!?;]{1,185}?)\brespectively\b", text, re.I):
        labels = []
        for label in METRIC.finditer(group["labels"]):
            if labels and labels[-1].lastgroup==label.lastgroup and re.fullmatch(
                    r"\s*\(\s*", group["labels"][labels[-1].end():label.start()]):
                labels[-1]=label
            else:
                labels.append(label)
        if not 2 <= len(labels) <= 8:
            continue
        if "," not in group["labels"] and not re.search(r"\band\b",group["labels"],re.I):
            continue
        cursor = group.start("values")
        records = []
        for label in labels:
            separator = re.match(r"[\s,]*(?:and\s+)?", text[cursor:], re.I)
            value = VALUE.match(text, cursor+separator.end())
            unit = UNIT.match(text, value.end()+len(text[value.end():])-len(text[value.end():].lstrip())) if value else None
            if not unit or d.normalize(label.lastgroup, 1, unit.group())[0] is None:
                break
            records.append((label.lastgroup, value, unit))
            cursor = unit.end()
        if len(records) == len(labels) and not text[cursor:group.end("values")].strip(" ,"):
            yield from records


def locate_prose_span(group, start, end):
    for offset,line in group:
        if offset<=start and end<=offset+len(line.text):
            return line,line.span.part(start-offset,end-offset)
    return None


def shared_percentage_values(text):
    for series in SHARED_PERCENT.finditer(text):
        prefix = re.split(r"[;!?]|\.\s+(?=[A-Z])", text[max(0,series.start()-130):series.start()])[-1]
        cues = list(PERCENT_SERIES_CUE.finditer(prefix))
        if not cues:
            continue
        tail = prefix[cues[-1].end():]
        if not re.search(r"\b(?:are|were|of|reached)\s*(?:approximately\s*)?$|[=:]\s*$", tail, re.I):
            continue
        if METRIC.search(tail) or PERCENT_DELTA.search(tail):
            continue
        metric = "FF" if cues[-1][0].lower().startswith("fill") else "PCE"
        unit = UNIT.match(text,series.start("unit"))
        for item in re.finditer(d.NUM,series["values"]):
            start = series.start("values")+item.start()
            value = VALUE.fullmatch(text,start,start+len(item[0]))
            if value:
                yield metric,value,unit


def scientific_cell_value(cell):
    """Read a flattened power of ten only when the exponent is superscript in the PDF."""
    match = re.fullmatch(rf"({d.NUM})\s*×\s*(10)([+−-]?\d{{1,3}})",cell.text)
    if not match:
        return None
    base_bottom = min(box[1] for box in cell.part(*match.span(2)).boxes)
    exponent_bottom = min(box[1] for box in cell.part(*match.span(3)).boxes)
    exponent = int(match[3].replace("−","-"))
    if exponent_bottom <= base_bottom+1.2 or not -100<=exponent<=100:
        return None
    return number(match[1])*10**exponent


def split_header_unit(header, metric, following):
    centers = sum(header.bbox[::2])/2
    for previous, line in zip(following,following[1:]):
        if previous.page != line.page:
            break
        for first in previous.cells:
            prefix = re.search(r"\(\s*(mA/|[ΩΩ])\s*$",first.text)
            if not prefix or abs(sum(first.bbox[::2])/2-centers)>20:
                continue
            for last in line.cells:
                suffix = re.fullmatch(r"(cm[²2])\)",last.text)
                if (not suffix or abs(sum(last.bbox[::2])/2-centers)>20
                        or not 0<first.bbox[1]-last.bbox[1]<18):
                    continue
                unit = prefix[1]+(" " if prefix[1] in "ΩΩ" else "")+suffix[1]
                if d.normalize(metric,1,unit)[0] is not None:
                    return unit,((previous,first.part(*prefix.span(1))),(line,last.part(*suffix.span(1))))
    return None



ROW_PARAMETER_HEADER = re.compile(r"(?:parameter|metric|property|characteristic|indicator)s?|параметр[ы]?|показател[ьи]|характеристик[аи]", re.I)
ROW_UNIT_HEADER = re.compile(r"units?(?:\s+of\s+measure(?:ment)?)?|ед(?:иниц[аы])?\.?\s*(?:изм(?:ерения)?\.?)?", re.I)
ROW_TABLE_METRICS = {"nt":"Nt","τ":"tau","tau":"tau"}
ROW_TABLE_EXTRA_UNITS = {
    "Nt":re.compile(r"cm\s*(?:\^?\s*[-−]\s*3|⁻³)|1\s*/\s*cm(?:\^?3|³)"),
    "tau":re.compile(r"(?<!\w)(?:[µμu]s|ns|ms|ps|s)\b"),
}
SCIENTIFIC_ROW_VALUE = re.compile(rf"\s*({d.NUM})\s*[×x]\s*10\s*(?:\^\s*([+−-]?\d+)|([⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+))\s*")
SUPERSCRIPTS = str.maketrans("⁺⁻⁰¹²³⁴⁵⁶⁷⁸⁹","+-0123456789")


def table_scientific_value(cell):
    if match := SCIENTIFIC_ROW_VALUE.fullmatch(cell.text):
        exponent = int((match[2] or match[3].translate(SUPERSCRIPTS)).replace("−","-"))
        if -100<=exponent<=100:
            return number(match[1])*10**exponent
    return scientific_cell_value(cell)


class Extraction:
    def __init__(self, connection, pub, version, lines):
        self.db, self.pub, self.version, self.lines = connection, pub, version, lines
        self.material_id = None
        self.device_materials = {}
        self.material_info = {}
        self.device_aliases = {}
        self.table_cohorts = set()
        self.table_rows = {}
        self.table_conditions = {}
        self.devices = {}
        self.measurements = set()
        self.result_ids = set()
        self.measurement_spectra = {}
        self.conditions = {}
        self.sections = {}
        current=None
        for line in self.lines:
            if re.fullmatch(r"(?:[0-9.]+\s+)?(?:abstract|introduction|methods?|materials and methods|experimental methods?|experimental section|results(?: and discussion)?|discussion|conclusions?|references|supporting information)",line.text,re.I):
                current=line.text
            self.sections[id(line)] = current

    def ref(self, line, span, role, kind="text", table=None, figure=None):
        ident = key(self.version, line.page, span.bbox, role, kind)
        insert(self.db, "source_reference", id=ident, publication_id=self.pub, pdf_version_id=self.version,
               source_page=line.page, bbox=span.bbox, source_fragment=span.text, source_type=kind,
               fragment_role=role, source_table=table, source_figure=figure,source_section=self.sections.get(id(line)))
        return ident

    def discover(self, metadata):
        text = "\n".join(line.text for line in self.lines)
        material_class = next((name for name, rx in d.CLASSES.items() if re.search(rx, text, re.I)), None)
        corpus, reason = d.classify(text, material_class)
        title, authors, year, journal = front_matter(metadata, self.lines)
        doi = bibliographic_doi(metadata, self.lines)
        fixture = any(re.match(r"SYNTHETIC TEST DOCUMENT|ТЕСТОВЫЙ ДОКУМЕНТ",line.text,re.I) for line in self.lines[:5])
        self.db.execute("""UPDATE publication SET title=COALESCE(?,title), doi=COALESCE(?,doi), authors=COALESCE(?,authors),
            year=COALESCE(?,year), journal=COALESCE(?,journal), is_fixture=MAX(is_fixture,?),
            corpus_status=CASE WHEN corpus_manual=0 THEN ? ELSE corpus_status END,
            corpus_reason=CASE WHEN corpus_manual=0 THEN ? ELSE corpus_reason END WHERE id=?""",
                        (title, doi, json.dumps(authors) if authors else None,
                         year, journal, fixture, corpus, reason, self.pub))
        effective=one(self.db,"SELECT corpus_status, corpus_manual FROM publication WHERE id=?",self.pub)
        corpus=effective["corpus_status"]
        eligible = not corpus.startswith("excluded") and (
            d.has_photovoltaic_context(text, material_class) or (corpus == "included" and effective["corpus_manual"])
        )
        self.db.execute("UPDATE pdf_version SET pv_extraction_enabled=? WHERE id=?", (int(eligible), self.version))
        if not eligible:
            return False
        passage = fabrication.fabrication_passage(self.lines)
        if material_class:
            self.material_id = key(self.pub, "material", material_class)
            raw = re.search(d.CLASSES[material_class], text, re.I).group()
            recipe, formula, precursors = fabrication.absorber_recipe(title, material_class, passage)
            bandgap = re.search(rf"(?:{d.METRICS['Eg']})\s*(?:[=:]|of|составила)\s*({d.NUM})\s*({d.BANDGAP_UNIT})", passage.text or text, re.I)
            method = re.search(r"band\s?gap method\s*:\s*([^.;\n]+)", passage.text or text, re.I)
            dimension = re.search(r"\b([0123]D)\s+(?:perovskite|material)", passage.text or (title or ""), re.I)
            additives = re.search(r"\badditives?\s*:\s*([^.;\n]+)", passage.text or text, re.I)
            eg, eg_unit = d.normalize("Eg", number(bandgap[1], decimal_comma=bool(d.CYRILLIC.search(bandgap[0]))), bandgap[2]) if bandgap else (None, None)
            self.material_info = dict(publication_id=self.pub, material_name_raw=recipe or raw, material_class=material_class,
                formula_normalized=formula, dimensionality=dimension[1] if dimension else None,
                additives=precursors or ([v.strip() for v in additives[1].split(",")] if additives else None),
                bandgap_value=eg, bandgap_unit=eg_unit, bandgap_method=method[1] if method else None)
            insert(self.db, "material", id=self.material_id, **self.material_info)
            if recipe:
                self.db.execute("""UPDATE material SET material_name_raw=?,formula_normalized=?,additives=?
                    WHERE id=?""", (recipe, formula, json.dumps(self.material_info["additives"]) if self.material_info["additives"] else None, self.material_id))
        for line in self.lines:
            match = DEVICE.search(line.text)
            if match and self.is_device_label(match[1]):
                self.device(match[1], line.text if "(" in line.text or "active area" in line.text.lower() else "")
        self.discover_table_devices()
        if not self.devices and corpus == "included":
            self.device("Образец без обозначения", device_type="single-junction thin-film"
                        if re.search(r"single[- ]junction|однопереходн\w*", text, re.I) else None)
        for line in self.lines:
            layer = re.search(r"^Layer\s+(\d+)\s*:\s*([^/]+)\s*/\s*([^/]+)\s*/\s*([^/]+)\s*/\s*(.+)$", line.text, re.I)
            if layer and layer[2].strip() in self.devices:
                thickness, _, process_text = layer[5].partition("/")
                val = re.search(rf"({d.NUM})\s*({d.THICKNESS_UNIT})", thickness)
                amount, unit = d.normalize("thickness", number(val[1]), val[2]) if val else (None, None)
                insert(self.db, "device_layer", id=key(self.pub, "layer", layer[2].strip(), layer[1]),
                       device_id=self.devices[layer[2].strip()], layer_order=int(layer[1]), layer_function=layer[3].strip(),
                       layer_material=layer[4].strip(), layer_thickness=amount, thickness_unit=unit,
                       deposition_method=process_text.split(";")[0].strip(" .") or None,
                       **self.named_conditions(process_text, ("annealing_temperature", "annealing_time", "atmosphere")))
            layer_prose = re.search(rf"\b([\w₀-₉]+)\s+(ETL|HTL|absorber|contact)\s+layer\s*\(\s*({d.NUM})\s*({d.THICKNESS_UNIT})\s*\)", line.text, re.I)
            label = DEVICE.search(line.text)
            if layer_prose and label and label[1] in self.devices:
                amount, unit = d.normalize("thickness", number(layer_prose[3]), layer_prose[4])
                insert(self.db, "device_layer", id=key(self.pub, "layer", line.page, line.span.bbox),
                       device_id=self.devices[label[1]], layer_function=layer_prose[2], layer_material=layer_prose[1],
                       layer_thickness=amount, thickness_unit=unit)
            if re.search(r"\b(?:all|each)\b.*\b(?:JV|J.V|scans?|measurements?)\b|\bвсе\s+(?:измерения|разв[её]ртки)\b", line.text, re.I):
                self.conditions.update(self.conditions_for(line))
        self.discover_layers(passage)
        return True

    @staticmethod
    def is_device_label(label):
        return (0 < len(label) <= 80 and not HEADER_WORDS.fullmatch(label) and not SCAN.fullmatch(label)
                and not METRIC.fullmatch(label) and not VALUE.fullmatch(label)
                and not re.match(r"^(?:references?|Table|Figure|\[|10\.)", label, re.I))

    def discover_table_devices(self):
        table = scope = page = None
        cited = False
        prefix = None
        caption_active = False
        for line in self.lines:
            if line.page != page:
                table = scope = None
                page = line.page
                cited = False
                caption_active = False
            if (caption := TABLE.match(line.text)):
                table = caption[1]
                cited = bool(re.search(r"literature|reported|previous|reference", line.text, re.I))
                scope = None
                caption_active = not cited
            if caption_active and len(line.cells) == 1:
                self.table_conditions.setdefault((page, table), {}).update(self.conditions_for(line))
            else:
                caption_active = False
            label = line.cells[0].text
            from_header = FROM_HEADER.fullmatch(label)
            if len(line.cells) >= 3 and (SAMPLE_HEADER.fullmatch(label) or from_header) and sum(
                    bool(METRIC.search(cell.text)) for cell in line.cells[1:]) >= 2:
                scope = (line.page, table) if table and not cited else None
                prefix = from_header[1] + " from " if from_header else None
                continue
            scalar_cells = sum(bool(VALUE.match(cell.text)) for cell in line.cells[1:])
            if not scope or scalar_cells < 2 or not self.is_device_label(label):
                scope = None
                continue
            if prefix:
                self.device_aliases[label] = prefix + label
            self.device(label, label, "cell")
            self.table_rows[id(line)] = self.known_device(label)
            if not any(SCAN.fullmatch(cell.text) for cell in line.cells[1:]):
                self.table_cohorts.add(scope)
        # Only promote concentration labels found in J–V figure captions when
        # the paper already uses this sample-naming convention in its tables.
        if any(fabrication.DOPANT.fullmatch(label) for label in self.devices):
            for index, line in enumerate(self.lines):
                if re.match(r"^Figure\s+\d+", line.text, re.I):
                    section = " ".join(part.text for part in self.lines[index:index + 5] if part.page == line.page)
                    if re.search(r"\bJ[–—−-]V\s+curves?\b", section, re.I):
                        for match in fabrication.DOPANT.finditer(section):
                            self.device(match[0], match[0], "cell")

    def discover_layers(self, passage):
        candidate = next(((match, line) for line in passage.lines + self.lines
                          if (match := fabrication.STACK.search(line.text)) and not re.search(
                              r"reported|literature|previous", line.text, re.I)), None)
        stack = fabrication.identify_layers(passage, candidate, self.material_info.get("formula_normalized"))
        for label, device in self.devices.items():
            roles = [role for role, _, _ in stack]
            if roles == ["front_contact", "ETL", "absorber", "HTL", "back_contact"]:
                self.db.execute("UPDATE device SET architecture=COALESCE(architecture,'n-i-p') WHERE id=?", (device,))
            if stack and re.search(r"(?:FTO|ITO|AZO)\)?\s+glass", passage.text, re.I):
                self.db.execute("UPDATE device SET substrate_type=COALESCE(substrate_type,'rigid') WHERE id=?", (device,))
            if one(self.db, "SELECT id FROM device_layer WHERE device_id=?", device):
                continue
            material_id = self.device_materials.get(device)
            material = one(self.db, "SELECT material_name_raw FROM material WHERE id=?", material_id) if material_id else None
            for order, (role, layer, bounds) in enumerate(stack, 1):
                if role == "absorber" and material:
                    layer = material["material_name_raw"]
                if role == "ETL" and " from " in label and label.split(" from ", 1)[0].lower() == layer.lower():
                    layer = label
                props = fabrication.layer_properties(passage.text[slice(*bounds)]) if bounds else {}
                if props.get("layer_thickness") is not None:
                    props["layer_thickness"], props["thickness_unit"] = d.normalize(
                        "thickness", props["layer_thickness"], props["thickness_unit"])
                layer_id = key(self.pub, "derived-layer", device, order)
                insert(self.db, "device_layer", id=layer_id, device_id=device, layer_order=order, layer_function=role,
                       layer_material=layer, material_id=material_id if role == "absorber" else None, **props)
                evidence = passage.sources(*bounds) if bounds else [(candidate[1], candidate[1].span.part(*candidate[0].span()))]
                for line, span in evidence:
                    insert(self.db, "layer_source", layer_id=layer_id, source_reference_id=self.ref(line, span, "condition"))

    def known_device(self, label):
        return self.devices.get(self.device_aliases.get(label, label))

    def cited_publication(self,literal, numbered=False):
        doi=DOI.search(literal)
        bibliography = None
        if not doi:
            ref=NUMBERED_REFERENCE.search(literal)
            if ref:
                doi=next((DOI.search(line.text) for line in self.lines if line.text.lstrip().startswith(ref[0]) and DOI.search(line.text)),None)
            if numbered and not doi and ref:
                entry = re.compile(rf"^(?:{ref[1]}[.)]|\[\s*{ref[1]}\s*\])\s+(.+)")
                for line in self.lines:
                    if (self.sections.get(id(line)) or "").lower() == "references" and (match := entry.match(line.text)):
                        bibliography = match[1]
                        doi = DOI.search(bibliography)
                        break
        if not doi:
            if not bibliography:
                return None
            cited=key(self.pub, "bibliography", ref[1])
            insert(self.db, "publication", id=cited, title=bibliography,
                   corpus_reason=f"Позиция [{ref[1]}] в библиографии")
            return cited
        normalized=doi.group().rstrip(".,;").lower()
        existing=one(self.db,"SELECT id FROM publication WHERE lower(doi)=?",normalized)
        if existing:
            return existing["id"]
        cited=key("doi",normalized)
        insert(self.db,"publication",id=cited,title=normalized,doi=normalized,corpus_reason="Библиографическая ссылка")
        return cited

    @staticmethod
    def named_conditions(text, fields):
        values = {}
        for field in fields:
            match = re.search(rf"\b{field.replace('_',' ')}\s*[:=]\s*([^;]+)", text, re.I)
            if match:
                values[field] = match[1].strip(" .")
        return values

    def conditions_for(self, line):
        text = line.text
        result = self.named_conditions(text, ("scan_rate", "temperature", "stability_protocol", "stress_conditions",
                                               "batch_label", "condition_label"))
        spectrum = re.search(r"AM\s*1[.,]5\s*G", text, re.I)
        if spectrum:
            result["spectrum"] = "AM1.5G"
            result["spectrum_source_id"] = self.ref(line, line.span.part(*spectrum.span()), "condition")
        intensity = re.search(rf"({d.NUM})\s*({d.INTENSITY_UNIT})", text)
        if intensity:
            result["light_intensity"], result["light_intensity_unit"] = d.normalize("Pin", number(intensity[1],decimal_comma=bool(d.CYRILLIC.search(text))), intensity[2])
            result["intensity_source_id"] = self.ref(line, line.span.part(*intensity.span()), "condition")
        if re.search(r"\b(?:independently certified|certified by)\b", text, re.I):
            result["certified"] = 1
        mask = re.search(r"\b(?:aperture mask|shadow mask|mask was used|masked with a metal aperture)\b", text, re.I)
        if mask:
            result["mask_used"] = 1
            result["condition_source_ids"] = [self.ref(line, line.span.part(*mask.span()), "condition")]
        speed = re.search(rf"\bscan rate\s+(?:of\s+)?({d.NUM}\s*V\s*(?:/s|s[−-]1|s⁻¹))", text, re.I)
        if speed:
            result["scan_rate"] = speed[1]
            result.setdefault("condition_source_ids", []).append(self.ref(line, line.span.part(*speed.span(1)), "condition"))
        return result

    def device(self, label, text="", device_type=None):
        source_label = label
        label = self.device_aliases.get(label, label)
        if not self.is_device_label(source_label):
            return None
        if label in self.devices:
            return self.devices[label]
        ident = key(self.pub, "device", source_label)
        self.devices[label] = ident
        material_id = self.material_id
        if self.material_info.get("formula_normalized") and (dopant := fabrication.DOPANT.fullmatch(label)):
            material_id = key(self.pub, "absorber", label)
            raw = f"{self.material_info['formula_normalized']} ({label})"
            insert(self.db, "material", id=material_id, **dict(self.material_info, material_name_raw=raw,
                   additives=[f"{dopant[2]} {dopant[1]} M"]))
        self.device_materials[ident] = material_id
        self.db.execute("UPDATE device SET device_label=?,material_id=COALESCE(?,material_id) WHERE id=?",
                        (label, material_id, ident))
        role = re.search(r"\b(control|target|record)\b", text, re.I)
        role_value = role[1].lower() if role else "control" if label.lower() == "pristine" else None
        architecture = re.search(r"(?<!\w)(n-i-p|p-i-n|superstrate|substrate)(?!\w)", text, re.I)
        area = re.search(rf"(active|aperture|total)\s+area\s*[:=]?\s*({d.NUM})\s*({d.AREA_UNIT})", text, re.I)
        v, u = d.normalize("area", number(area[2]), area[3]) if area else (None, None)
        encapsulation = re.search(r"\b(unencapsulated|encapsulated)\b", text, re.I)
        insert(self.db, "device", id=ident, publication_id=self.pub, material_id=material_id, device_label=label,
               device_role=role_value, device_type=device_type or ("single-junction thin-film" if architecture else None),
               architecture=architecture[1].lower() if architecture else None, active_area=v, area_unit=u,
               area_definition=area[1].lower() if area else None, encapsulation=encapsulation[1] if encapsulation else None,
               **self.named_conditions(text, ("substrate_type",)))
        return ident

    def measurement(self, device, scan, local=None, kind="JV", cohort=None):
        if device is None:
            return None
        evidence = dict(self.conditions) if kind == "JV" else {}
        evidence.update(local or {})
        spectrum_source = evidence.pop("spectrum_source_id", None)
        sources = evidence.pop("condition_source_ids", ())
        signature = {k: v for k, v in evidence.items() if k != "intensity_source_id"}
        ident = key(self.version, cohort or device, scan, kind, sorted(signature.items()))
        self.measurements.add(ident)
        if spectrum_source:
            self.measurement_spectra.setdefault(ident, spectrum_source)
        insert(self.db, "measurement", id=ident, device_id=device, scan_direction=scan,
               measurement_type=kind, **evidence)
        insert(self.db, "measurement_device", measurement_id=ident, device_id=device)
        for source in (spectrum_source, evidence.get("intensity_source_id"), *sources):
            if source:
                insert(self.db, "measurement_source", measurement_id=ident, source_reference_id=source)
        return ident

    def result(self, metric, match, line, span, unit, refs, device, scan, origin, cited=None, extra="", conditions=None, row_aggregation=None, extraction_method=None, numeric_override=None, raw_literal=None, row_labelled=False, row_model=False, cohort=None, linked_measurement=None):
        decimal_comma = bool(d.CYRILLIC.search(f"{line.text} {unit or ''} {extra}"))
        # Table readers can also pass a parsed scalar; prefer the original localized literal.
        if decimal_comma and match and "," in match["n"] and (
                numeric_override is None or re.fullmatch(d.NUM,span.text)):
            numeric_override = number(match["n"],decimal_comma=True)
        value = numeric_override if numeric_override is not None else number(match["n"]) if match else None
        op = match["op"] if match else None
        spread = number(match["spread"],decimal_comma=decimal_comma) if match and match["spread"] else None
        end = number(match["end"],decimal_comma=decimal_comma) if match and match["end"] else None
        low, high = sorted((value,end)) if end is not None else (None,None)
        local = self.conditions_for(line)
        local.update(conditions or {})
        measurement_kind = {"T80": "stability", "retained_PCE": "stability", "Jsc_EQE": "EQE",
                            "stabilized_PCE": "MPP tracking", "Jmpp": "MPP tracking", "Vmpp": "MPP tracking"}.get(metric, "JV")
        if metric == "Jsc_EQE" and scan:
            # A published EQE comparison may explicitly name the corresponding J–V scan.
            measurement_kind = "JV"
        measurement = linked_measurement
        if measurement is None and origin == "primary" and metric != "Eg" and not row_model:
            measurement = self.measurement(device, scan if measurement_kind == "JV" else None,
                                           local, measurement_kind, cohort=cohort)
        agg = row_aggregation or aggregation(extra + " " + line.text)
        fraction = (metric == "FF" and unit is None and match and op is None and end is None
                    and re.fullmatch(r"(?:0[.,]|[.,])\d+",match["n"]) and 0 < value < 1)
        effective_unit = "fraction" if fraction else unit
        norm, norm_unit = d.normalize(metric, value, effective_unit) if op is None and end is None else (None, None)
        ambiguous = metric != "Eg" and origin == "primary" and not device
        uncertain = (fraction or line.ocr or row_model or ambiguous or (value is not None and norm is None) or (origin == "cited" and cited is None)
                     or extraction_method in d.AI_EXTRACTION_METHODS.values())
        state = "missing" if value is None else "ambiguous" if uncertain else "numeric_zero" if value == 0 else "numeric"
        original_value = span.text if raw_literal is None else raw_literal
        raw_record = dict(value=original_value, unit=unit, spread_value=spread, range_min=low, range_max=high,
                          inequality_op=op, aggregation=agg, scan_direction=scan)
        count = re.search(r"\bn\s*=\s*(\d+)", extra + " " + line.text, re.I)
        point = re.search(rf"\bafter\s+({d.NUM})\s*(h|hours?|days?|min)\b", line.text, re.I)
        if count:
            raw_record["n_devices"] = int(count[1])
        if point:
            raw_record["time_point"], raw_record["time_unit"] = point[1], point[2]
        time, time_unit = d.normalize("T80", number(point[1]), point[2]) if point else (None, None)
        result_id = key(self.version, line.page, metric, span.bbox)
        if metric == "retained_PCE":
            previous = one(self.db,"""SELECT * FROM measurement_result WHERE id=? AND metric_name='PCE'
                AND raw_value=? AND raw_unit IS ? AND duplicate_of IS NULL""",
                key(self.version,line.page,"PCE",span.bbox),original_value,unit)
            if previous and (previous["manual"] or previous["deleted"]):
                return previous["id"]
            if previous and not one(self.db,"SELECT id FROM measurement_result WHERE id=?",result_id):
                edit(self.db,"measurement_result",previous,{"metric_name":metric,"measurement_id":measurement},
                     author="softx-parser")
                result_id = previous["id"]
        from_table = any(kind == "table_cell" for _, kind in refs)
        status = d.REVIEW if uncertain or d.outside_physical_bounds(metric, norm) else d.AUTO
        insert(self.db, "measurement_result", id=result_id, publication_id=self.pub, pdf_version_id=self.version,
               material_id=self.device_materials.get(device, self.material_id) if origin == "primary" else None, device_id=device, measurement_id=measurement,
               metric_name=metric, metric_value=value, metric_unit=effective_unit, raw_value=original_value, raw_unit=unit,
               raw_record=raw_record, normalized_value=norm, normalized_unit=norm_unit, aggregation=agg,
               value_state=state, spread_value=spread, range_min=low, range_max=high, inequality_op=op,
               n_devices=int(count[1]) if count else None, time_point=time, time_unit=time_unit,
               result_origin=origin, cited_publication_id=cited, extraction_status=status,
               extraction_method=("local_ocr" if line.ocr and extraction_method in (None,"pdf_prose_context")
                                  else extraction_method or ("geometry_table" if from_table else "pdf_text")),
               note="Столбец содержит значения модели." if row_model else
               "FF без указанной единицы интерпретирован как доля; требуется проверка." if fraction else None)
        if row_labelled:
            self.db.execute("""UPDATE measurement_result SET device_id=?,measurement_id=?,metric_unit=?,
                normalized_value=?,normalized_unit=?,extraction_status=?,value_state=?,extraction_method='geometry_table',
                candidate_device_ids='[]',candidate_measurement_ids='[]'
                WHERE id=? AND manual=0 AND deleted=0 AND raw_value=? AND raw_unit IS NULL
                  AND normalized_value IS NULL AND ? IS NOT NULL""",
                (device,measurement,effective_unit,norm,norm_unit,status,state,result_id,original_value,norm))
        if fraction:
            self.db.execute("""UPDATE measurement_result SET metric_unit=?, normalized_value=?, normalized_unit=?,
                value_state=?, extraction_status=?, note=?
                WHERE id=? AND manual=0 AND raw_unit IS NULL AND normalized_value IS NULL""",
                (effective_unit,norm,norm_unit,state,status,"FF без указанной единицы интерпретирован как доля; требуется проверка.",result_id))
        self.result_ids.add(result_id)
        if from_table and measurement:
            # Re-reading a printed table can repair the current links, including
            # old rows where a scan label was incorrectly treated as the device.
            existing = one(self.db, """SELECT * FROM measurement_result WHERE id=? AND manual=0 AND deleted=0
                AND result_origin='primary' AND acquisition_method='published_numeric'
                AND extraction_method IN ('geometry_table','pdf_text','local_ocr')
                AND raw_value=? AND raw_unit IS ?""", result_id, original_value, unit)
            if existing:
                edit(self.db, "measurement_result", existing,
                     dict(device_id=device, measurement_id=measurement, material_id=self.device_materials.get(device),
                          aggregation=agg, value_state=state, extraction_status=status,
                          extraction_method="geometry_table" if not line.ocr else "local_ocr",
                          candidate_device_ids=[], candidate_measurement_ids=[]), author="softx-parser")
        for ref, _ in refs:
            insert(self.db, "result_source", result_id=result_id, source_reference_id=ref)
        if measurement:
            conditions = one(self.db, "SELECT intensity_source_id FROM measurement WHERE id=?", measurement)
            measurement_sources = rows(self.db, "SELECT source_reference_id FROM measurement_source WHERE measurement_id=?", measurement)
            for ref in {conditions["intensity_source_id"], self.measurement_spectra.get(measurement),
                        *(source["source_reference_id"] for source in measurement_sources)}:
                if ref:
                    insert(self.db, "result_source", result_id=result_id, source_reference_id=ref)
        if ambiguous:
            self.db.execute("UPDATE measurement_result SET candidate_device_ids=?,candidate_measurement_ids=? WHERE id=? AND manual=0",
                            (json.dumps(list(self.devices.values())), "[]", result_id))
        return result_id


    def add_reported_intervals(self, text, group):
        for cue in METRIC.finditer(text):
            interval = PROSE_INTERVAL.match(text, cue.end())
            if not interval or (interval["prefix"].lower(), interval["join"].lower()) not in (
                    ("from", "to"), ("between", "and")):
                continue
            if locate_prose_span(group,*cue.span()) is None:
                continue
            start = locate_prose_span(group,*interval.span("low"))
            end = locate_prose_span(group,*interval.span("high"))
            if not start or not end:
                continue
            following = text[interval.end():]
            unit = UNIT.match(text,interval.end()+len(following)-len(following.lstrip()))
            if unit and d.normalize(cue.lastgroup,1,unit.group())[0] is None:
                continue
            first_unit = interval["first_unit"]
            if first_unit and (d.normalize(cue.lastgroup,1,first_unit)[0] is None
                               or (unit and first_unit!=unit.group())):
                continue
            line,start_span = start
            anchor = locate_prose_span(group,interval.start("low"),interval.end("high"))
            span = anchor[1] if anchor else start_span
            refs = [(self.ref(*observed,"value"),"text") for observed in (start,end)]
            if unit and (unit_span:=locate_prose_span(group,*unit.span())):
                refs.append((self.ref(*unit_span,"unit"),"text"))
            if first_unit and (unit_span:=locate_prose_span(group,*interval.span("first_unit"))):
                refs.append((self.ref(*unit_span,"unit"),"text"))
            metric = cue.lastgroup
            match = VALUE.fullmatch(interval["low"]+" to "+interval["high"])
            cited = bool(re.search(r"\bpreviously reported\b|\bet al\.", line.text, re.I))
            self.result(metric,match,line,span,unit.group() if unit else first_unit,refs,
                        None,None,"cited" if cited else "primary",
                        self.cited_publication(line.text) if cited else None,
                        extraction_method="pdf_prose_context",
                        raw_literal=text[interval.start("low"):interval.end("high")])

    def extra_prose(self):
        for text, group in prose_windows(self.lines):
            self.add_reported_intervals(text, group)
            candidates = [*ordered_prose_values(text), *shared_percentage_values(text)]
            if len(group)>1:
                boundary = group[1][0]
                first_line = group[0][1]
                local_offsets = {(metric, value.start("n")) for metric,value,_ in prose_metrics(first_line.text)}
                for metric,value,descriptor_end in prose_metrics(text):
                    if descriptor_end >= boundary or (metric,value.start("n")) in local_offsets:
                        continue
                    unit_start = value.end()+len(text[value.end():])-len(text[value.end():].lstrip())
                    unit = UNIT.match(text,unit_start)
                    if unit and d.normalize(metric,1,unit.group())[0] is not None:
                        candidates.append((metric,value,unit))
            for metric,value,unit in candidates:
                self.add_prose_candidate(group, metric, value, unit, "pdf_prose_context")

    def add_prose_candidate(self, group, metric, value, unit, method):
        source = locate_prose_span(group,value.start("n") if not value["op"] else value.start(),value.end())
        unit_source = locate_prose_span(group,*unit.span())
        if not source or not unit_source:
            return
        line, span = source
        if one(self.db, """SELECT 1 FROM source_reference s JOIN result_source l ON l.source_reference_id=s.id
                          JOIN measurement_result r ON r.id=l.result_id
                          WHERE s.pdf_version_id=? AND s.source_page=? AND s.fragment_role='value'
                            AND (r.manual=1 OR r.deleted=1 OR l.result_id IN (SELECT value FROM json_each(?))) AND
                            json_extract(s.bbox,'$[0]')<=? AND json_extract(s.bbox,'$[1]')<=?
                            AND json_extract(s.bbox,'$[2]')>=? AND json_extract(s.bbox,'$[3]')>=? LIMIT 1""",
               self.version,line.page,json.dumps(sorted(self.result_ids)),(span.bbox[0]+span.bbox[2])/2,(span.bbox[1]+span.bbox[3])/2,
               (span.bbox[0]+span.bbox[2])/2,(span.bbox[1]+span.bbox[3])/2):
            return
        context = " ".join(part.text for _,part in group)
        origin = "cited" if re.search(r"\bpreviously reported\b|\bet al\.",context,re.I) else "primary"
        refs = [(self.ref(line,span,"value"),"text"),(self.ref(*unit_source,"unit"),"text")]
        self.result(metric,value,line,span,unit.group(),refs,None,None,origin,
                    self.cited_publication(context) if origin=="cited" else None,extraction_method=method)

    def contextual_experiments(self):
        eqe = re.compile(r"\b(?:integration of (?:the )?EQE spectrum yields? (?:the )?photocurrent of|"
                         r"EQE[- ]integrated current density (?:of|was))\s+"
                         rf"(?P<j>{d.NUM})\s*(?P<ju>{d.CURRENT_UNIT})\s+for\s+(?:the\s+)?(?P<sample>.{{1,110}}?)\s+"
                         rf"and\s+(?P<j2>{d.NUM})\s*(?P<ju2>{d.CURRENT_UNIT})\s+for\s+(?:the\s+)?(?P<sample2>.{{1,110}}?)(?=,(?!\d)|\.(?!\d)|;|$)", re.I)
        mpp = re.compile(r"\bcurrent density at (?:the )?MPP was\s+"
                         rf"(?P<j>{d.NUM})\s*(?P<ju>{d.CURRENT_UNIT})\s+at a bias voltage of\s+"
                         rf"(?P<v>{d.NUM})\s*(?P<vu>mV|V),?\s+corresponding to an?\s+(?:efficiency|effci iency) of\s+(?P<p>{d.NUM})\s*(?P<pu>%)", re.I)
        seen = set()
        for text, group in prose_windows(self.lines, max_lines=6):
            for match in eqe.finditer(text):
                association = []
                for value, unit, sample in (("j", "ju", "sample"), ("j2", "ju2", "sample2")):
                    label = match[sample]
                    candidates = [(name, ident) for name, ident in self.devices.items() if re.search(
                        r"(?<![\w.])" + re.escape(name) + r"(?!\w)", label, re.I)]
                    if len(candidates) == 1:
                        association.append((value, unit, candidates[0][1], sample))
                cue = locate_prose_span(group, match.start(), min(match.start()+12, match.end()))
                cohort = ("eqe", cue[0].page, tuple(cue[1].bbox)) if cue and len(association) > 1 else None
                for value, unit, device, sample in association:
                    label_ref = locate_prose_span(group, *match.span(sample))
                    self.contextual_value(group, match, value, unit, "Jsc_EQE", device, seen, cohort, label_ref)
            for match in mpp.finditer(text):
                prefix = text[:match.start()]
                candidates = {ident for name, ident in self.devices.items() if re.search(
                    r"(?<![\w.])" + re.escape(name) + r"(?!\w)", prefix, re.I)}
                device = next(iter(candidates)) if len(candidates) == 1 else None
                for value, unit, metric in (("j","ju","Jmpp"),("v","vu","Vmpp"),("p","pu","stabilized_PCE")):
                    self.contextual_value(group, match, value, unit, metric, device, seen)

    def contextual_value(self, group, match, value, unit, metric, device, seen, cohort=None, label_ref=None):
        observed = locate_prose_span(group, *match.span(value))
        observed_unit = locate_prose_span(group, *match.span(unit))
        if not observed or not observed_unit:
            return
        line, span = observed
        ident = (metric, line.page, tuple(span.bbox))
        if ident in seen:
            return
        seen.add(ident)
        refs = [(self.ref(*observed, "value"), "text"), (self.ref(*observed_unit, "unit"), "text")]
        if label_ref:
            refs.append((self.ref(*label_ref, "sample_label"), "text"))
        self.result(metric, VALUE.fullmatch(match[value]), line, span, match[unit], refs, device, None,
                    "primary", extraction_method="pdf_prose_context", cohort=cohort)

    def grouped_table_rows(self):
        """Associate vertically centered sample labels with paired mean and record rows."""
        paired_tables = set()
        grouped = {}
        table = None
        paired = False
        page = None
        for index, line in enumerate(self.lines):
            if line.page != page:
                table, paired, page = None, False, line.page
            caption = TABLE.match(line.text)
            if caption:
                table = caption[1]
                declared = re.search(r"\b(?:average|mean)\b.*\b(?:record|champion|best)\b.*\bparentheses\b", line.text, re.I)
                if declared:
                    paired_tables.add(table)
                paired = bool(declared or (table in paired_tables and re.search(r"\bcont(?:inued)?\b", line.text, re.I)))
                continue
            if re.match(r"^(?:figure|fig\.)\s*\d", line.text, re.I):
                paired = False
            if not paired or not 0 < index < len(self.lines)-1:
                continue
            mean, champion = self.lines[index-1], self.lines[index+1]
            label = line.cells[0]
            if mean.page != page or champion.page != page or not re.fullmatch(r"[A-Za-z][\w-]{0,20}", label.text):
                continue
            if len(mean.cells) < 2 or len(mean.cells) != len(champion.cells):
                continue
            if not (0 < mean.cells[0].bbox[1]-label.bbox[1] < 35 and 0 < label.bbox[1]-champion.cells[0].bbox[1] < 35):
                continue
            if label.bbox[2] >= min(mean.cells[0].bbox[0], champion.cells[0].bbox[0]):
                continue
            if not all(VALUE.fullmatch(cell.text) for cell in mean.cells):
                continue
            if not all(cell.text.startswith("(") and cell.text.endswith(")") and VALUE.fullmatch(cell.text[1:-1])
                       for cell in champion.cells):
                continue
            if not all(abs(sum(a.bbox[::2])-sum(b.bbox[::2]))<32 for a,b in zip(mean.cells,champion.cells)):
                continue
            grouped[id(mean)] = (line, label, "mean")
            grouped[id(line)] = (line, label, "unknown")
            grouped[id(champion)] = (line, label, "champion")
        return grouped

    def parse_row_labelled_tables(self):
        # Rejoin cells split by the page's prose gutter without changing PDF coordinates.
        for_page = {}
        for line in self.lines:
            if not line.image_annotation and not line.rotated:
                for_page.setdefault(line.page, []).append(line)
        consumed = set()
        for lines in for_page.values():
            bands = []
            for line in sorted(lines, key=lambda row: -row.cells[0].bbox[1]):
                y = line.cells[0].bbox[1]
                if not bands or bands[-1][0] - y > 3:
                    bands.append((y, []))
                bands[-1][1].append(line)
            header = None
            table = None
            caption_y = None
            previous_y = None
            for y, parts in bands:
                cells = sorted((cell for part in parts for cell in part.cells), key=lambda cell: cell.bbox[0])
                if caption := TABLE.match(cells[0].text):
                    table = caption[1]
                    caption_y = y
                    header = None
                    continue
                if len(cells) >= 3 and ROW_PARAMETER_HEADER.fullmatch(cells[0].text):
                    columns = [i for i, cell in enumerate(cells) if ROW_UNIT_HEADER.fullmatch(cell.text)]
                    if len(columns)<=1 and all(not VALUE.fullmatch(cell.text) for cell in cells[1:]):
                        header = (parts[0], cells, columns[0] if columns else None)
                        if caption_y is None or caption_y-y>75:
                            table = None
                        previous_y = y
                    continue
                if not header:
                    continue
                head_line, headings, unit_index = header
                label = cells[0]
                if (previous_y-y>37 or abs(label.bbox[0]-headings[0].bbox[0])>28
                        or len(label.text)>60 or len(cells)<2):
                    header = None
                    continue
                previous_y = y
                found = METRIC.match(label.text)
                extra = ROW_TABLE_METRICS.get(label.text.casefold())
                metric = found.lastgroup if found else extra
                if not metric:
                    continue
                if found and re.search(r"\b(?:deficit|loss|retention|change|ratio)\b",label.text[found.end():],re.I):
                    continue
                if len(cells)==len(headings):
                    mapped = dict(enumerate(cells))
                else:
                    mapped = {0:label}
                    for cell in cells[1:]:
                        distances = sorted((column_distance(cell,head),index)
                                           for index,head in enumerate(headings) if index)
                        distance,index = distances[0]
                        if distance<=34 and (len(distances)==1 or distances[1][0]-distance>=8):
                            if index in mapped:
                                mapped[index] = None
                            else:
                                mapped[index] = cell
                unit_cell = mapped.get(unit_index)
                row_unit = self.row_table_unit(metric,unit_cell.text) if unit_cell else None
                label_unit = self.row_table_unit(metric,label.text)
                made_record = False
                for index,heading in enumerate(headings[1:],1):
                    cell = mapped.get(index)
                    if index==unit_index or not cell:
                        continue
                    match = VALUE.match(cell.text)
                    if not match:
                        continue
                    scalar = table_scientific_value(cell)
                    tail = cell.text[match.end():].strip()
                    inline_unit = self.row_table_unit(metric,tail)
                    if tail and scalar is None and (inline_unit is None or inline_unit.span()!=(0,len(tail))):
                        continue
                    unit_source = cell if inline_unit else unit_cell if row_unit else label
                    unit_match = inline_unit or row_unit or label_unit
                    unit = unit_match.group() if unit_match else None
                    if unit_match and inline_unit:
                        start = len(cell.text)-len(tail)
                        unit_span = cell.part(start+unit_match.start(),start+unit_match.end())
                        value = cell.part(0,match.end())
                    else:
                        unit_span = unit_source.part(*unit_match.span()) if unit_match else None
                        value = cell
                    line = next(part for part in parts if cell in part.cells)
                    refs = [(self.ref(line,value,"value","table_cell",table),"table_cell"),
                            (self.ref(head_line,heading,"sample_label","table_header",table),"table_header"),
                            (self.ref(line,label,"condition","table_header",table),"table_header")]
                    if unit_span is not None:
                        refs.append((self.ref(line,unit_span,"unit","table_cell",table),"table_cell"))
                    model = re.search(r"\b(?:model|simulated|simulation)\b",heading.text,re.I)
                    device = self.device(heading.text,device_type="model" if model else None)
                    self.result(metric,match,line,value,unit,refs,device,None,"primary",
                                numeric_override=scalar,row_labelled=True,row_model=bool(model))
                    made_record = True
                if made_record:
                    consumed.update(map(id,parts))
        return consumed

    @staticmethod
    def row_table_unit(metric,text):
        pattern = ROW_TABLE_EXTRA_UNITS.get(metric)
        if pattern:
            return pattern.search(text)
        return metric_unit(metric,text)


    def retire_formula_arguments(self):
        # Retire old automatic records only when their original anchor is the formula argument.
        for line in self.lines:
            for formula in re.finditer(r"(?<![\w])[ηɳ]\s*\(\s*(?P<zero>0)\s*\)", line.text):
                span = line.span.part(*formula.span("zero"))
                ident = key(self.version,line.page,"PCE",span.bbox)
                result = one(self.db, """SELECT * FROM measurement_result
                    WHERE id=? AND manual=0 AND deleted=0 AND metric_name='PCE' AND raw_value='0'
                      AND raw_unit IS NULL AND extraction_method IN ('pdf_text','pdf_prose_context','local_ocr')""",ident)
                if result:
                    edit(self.db,"measurement_result",result,{"deleted":1,
                         "note":"Исходный ноль — аргумент функции η(0)."},author="softx-parser")

    def comparison_measurement(self, metric, match, unit, label):
        if match is None or match["end"] or match["op"]:
            return None
        value, _ = d.normalize(metric, number(match["n"]), unit)
        if value is None:
            return None
        variants = []
        for result in rows(self.db, """SELECT DISTINCT r.device_id,r.measurement_id,d.device_label
             FROM measurement_result r JOIN device d ON d.id=r.device_id
             WHERE r.pdf_version_id=? AND r.metric_name=? AND r.normalized_value=?
               AND r.result_origin='primary' AND r.extraction_method='geometry_table'
               AND r.measurement_id IS NOT NULL AND r.deleted=0""", self.version, metric, value):
            alias = result["device_label"].rsplit(" from ", 1)[-1]
            if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", label, re.I):
                variants.append(result)
                continue
            stem = re.sub(r"[-–]\d+$", "", alias)
            if stem != alias and re.search(r"(?<!\w)" + re.escape(stem) + r"(?!\w)", label, re.I):
                variants.append(result)
        return variants[0] if len(variants) == 1 else None

    def parse(self):
        self.retire_formula_arguments()
        row_labels = self.parse_row_labelled_tables()
        table, heading, headers, active_page = None, "", {}, None
        subheaders = {}
        grouped = self.grouped_table_rows()
        transposed_labels, table_left = [], None
        ambiguous = []
        header_units = {}
        table_right, reference_header = None, None
        for index,line in enumerate(self.lines):
            if id(line) in row_labels:
                continue
            if active_page != line.page or line.image_annotation or line.rotated:
                table, heading, headers, subheaders = None, "", {}, {}
                transposed_labels, table_left = [], None
                header_units = {}
                table_right, reference_header = None, None
                active_page = line.page
            cap = TABLE.match(line.text)
            if cap:
                table, heading, headers, subheaders = cap[1], line.text, {}, {}
                transposed_labels, table_left = [], line.cells[0].bbox[0]
                header_units = {}
                table_right, reference_header = None, None
                continue
            if table_left is not None and headers:
                right = table_right or max(span.bbox[2] for _,span,_ in headers.values())
                if all(cell.bbox[0]>right+9 or cell.bbox[2]<table_left-9 for cell in line.cells):
                    continue
            if table and not headers and len(line.cells)>=3:
                label = line.cells[0]
                metric = METRIC.match(label.text)
                if (transposed_labels and len(line.cells)-1==len(transposed_labels)
                        and all(VALUE.fullmatch(cell.text) for cell in line.cells[1:])
                        and all(abs(head.bbox[0]-cell.bbox[0])<=12 for (_,head),cell in zip(transposed_labels,line.cells[1:]))):
                    unit = metric_unit(metric.lastgroup,label.text) if metric else None
                    if unit:
                        source = self.ref(line,label.part(*unit.span()),"unit","table_header",table)
                        for (head_line,head), cell in zip(transposed_labels,line.cells[1:]):
                            origin = "cited" if re.search(r"\bliterature\b",heading,re.I) else "primary"
                            device = self.device(head.text) if origin=="primary" else None
                            refs=[(self.ref(line,cell,"value","table_cell",table),"table_cell"),
                                  (source,"table_header"),
                                  (self.ref(head_line,head,"sample_label","table_header",table),"table_header")]
                            self.result(metric.lastgroup,VALUE.fullmatch(cell.text),line,cell,unit.group(),
                                        refs,device,None,origin,extra=heading)
                    continue
                if not table_headers(line) and all(len(cell.text)<30 and not VALUE.fullmatch(cell.text) for cell in line.cells):
                    # The corner cell describes the sample column in some layouts.
                    stub = re.fullmatch(rf"(?:sample|device|cell|{RUSSIAN_SAMPLE})(?:\s+(?:name|id|label))?", label.text, re.I)
                    transposed_labels = [(line,cell) for cell in line.cells[1 if stub else 0:]]
                    continue
            candidate = table_headers(line) if len(line.cells) >= 2 and not line.image_annotation else {}
            if table and not headers:
                first = line.cells[0]
                if table_left is not None and first.bbox[0]>table_left+155 and not candidate:
                    continue
                if not candidate and (len(line.cells)==1 or (
                        abs(first.bbox[0]-table_left)<12 and len(first.text)>33)):
                    heading += " "+first.text
                    continue
            if (candidate and (len(candidate)>=2 or table)
                    and not any(VALUE.match(cell.text) or next(prose_metrics(cell.text),None) for cell in line.cells)):
                if index>0 and self.lines[index-1].page==line.page:
                    for cell in line.cells:
                        if cell.text.lower()=="factor" and any(
                                prefix.text.lower()=="fill" and abs(sum(prefix.bbox[::2])-sum(cell.bbox[::2]))<20
                                and 0<prefix.bbox[1]-cell.bbox[1]<18 for prefix in self.lines[index-1].cells):
                            candidate[cell.bbox[0]]=(line,cell,"FF")
                headers.update(candidate)
                if table:
                    table_left = min(table_left, line.cells[0].bbox[0])
                    table_right = max(table_right or 0, line.cells[-1].bbox[2])
                    reference_header = next((cell for cell in line.cells if REFERENCE_HEADER.fullmatch(cell.text)), reference_header)
                for x,(_,cell,metric) in candidate.items():
                    split=split_header_unit(cell,metric,self.lines[index:index+3])
                    if split:
                        header_units[x]=split
                subheaders={}
                continue
            if headers and not any(VALUE.match(cell.text) for cell in line.cells):
                qualifiers=list(table_qualifiers(line.cells))
                if len(qualifiers)>=2:
                    for cell in qualifiers:
                        center=(cell.bbox[0]+cell.bbox[2])/2
                        header_line,header_cell,metric=min(headers.values(),
                            key=lambda item:abs(sum(item[1].bbox[::2])/2-center))
                        if SCAN.fullmatch(cell.text) or metric_unit(metric,cell.text):
                            subheaders[cell.bbox[0]]=(header_line,header_cell,metric,line,cell)
                    continue
            if headers and len(line.cells)<=2 and all(len(cell.text)<25 for cell in line.cells) and not any(VALUE.match(cell.text) for cell in line.cells) and not re.search(r"[.!?]",line.text):
                continue
            if headers and len(line.cells) >= 2:
                scan = SCAN.search(line.text)
                scan = scan_direction(scan)
                label_line, label, row_aggregation = grouped.get(id(line), (line, line.cells[0], None))
                origin = "cited" if re.search(r"literature|reported|previous|reference", heading, re.I) or DOI.search(label.text) else "primary"
                cited = None
                reference = None
                if reference_header:
                    reference = next((cell for cell in line.cells if column_distance(cell, reference_header) <= 24 and
                                      (NUMBERED_REFERENCE.fullmatch(cell.text) or DOI.search(cell.text) or OWN_WORK.fullmatch(cell.text))), None)
                    if reference:
                        origin = "primary" if OWN_WORK.fullmatch(reference.text) else "cited"
                        if label_line is line and re.fullmatch(r"\d+[.)]?", label.text):
                            label = next((cell for cell in line.cells[1:] if cell.bbox[2] < min(headers)-4), reference)
                if origin=="cited":
                    cited=self.cited_publication(reference.text, numbered=True) if reference else self.cited_publication(label.text)
                device = self.known_device(label.text) if origin == "primary" else None
                if origin == "primary" and SCAN.fullmatch(label.text):
                    matching = {self.known_device(dopant[0]) for dopant in fabrication.DOPANT.finditer(heading)} - {None}
                    if len(matching) == 1:
                        device = matching.pop()
                if (not device and origin == "primary" and table and reference is None
                        and self.is_device_label(label.text) and label.bbox[2] < min(headers)
                        and not re.search(r"[!?]", label.text)):
                    device = self.device(label.text, label.text, "cell")
                used = False
                columns=dict(subheaders)
                for x,(header_line,header_cell,metric) in headers.items():
                    if not subheaders or (x in header_units and not any(head is header_cell for _,head,*_ in subheaders.values())):
                        columns[x]=(header_line,header_cell,metric,None,None)
                cells=list(table_values(line.cells,columns)) if len(headers)>=2 else line.cells
                mapped_cells = {}
                for x,(_,header_cell,_,_,qualifier) in columns.items():
                    anchor = qualifier or header_cell
                    cell = min(cells,key=lambda span:column_distance(span,anchor))
                    distance = column_distance(cell,anchor)
                    previous = mapped_cells.get(id(cell))
                    if distance<=32 and (previous is None or distance<previous[1]):
                        mapped_cells[id(cell)]=(x,distance)
                for cell in cells:
                    mapping = mapped_cells.get(id(cell))
                    if mapping is None:
                        continue
                    header_line, header_cell, metric, qualifier_line, qualifier = columns[mapping[0]]
                    match = VALUE.match(cell.text, 1 if row_aggregation == "champion" and cell.text.startswith("(") else 0)
                    if not match and cell.text.strip() not in ("—", "–", "-", "N/A", "NA"):
                        continue
                    used = True
                    unit_match = None
                    alternate = re.fullmatch(rf"\s*\(\s*({d.NUM})\s*\)\s*[*†‡]?\s*",cell.text[match.end():]) if match and match["spread"] else None
                    value_span = cell.part(match.start(),match.end()) if alternate else cell
                    refs = [(self.ref(line, value_span, "value", "table_cell", table), "table_cell")]
                    if label is not cell:
                        refs.append((self.ref(label_line, label, "sample_label", "table_cell", table), "table_cell"))
                    if reference is not None and reference is not label:
                        refs.append((self.ref(line,reference,"sample_label","table_cell",table),"table_cell"))
                    for unit_line, unit_span, kind in ((line, cell, "table_cell"),
                            (qualifier_line, qualifier, "table_header"), (header_line, header_cell, "table_header")):
                        if unit_span is None:
                            continue
                        unit_match = metric_unit(metric, unit_span.text)
                        if unit_match:
                            fragment = unit_span.part(*unit_match.span())
                            refs.append((self.ref(unit_line, fragment, "unit", kind, table), kind))
                            break
                    unit = unit_match.group() if unit_match else None
                    split = header_units.get(header_cell.bbox[0])
                    if not unit and split:
                        unit,pieces=split
                        refs.extend((self.ref(piece_line,fragment,"unit","table_header",table),"table_header")
                                    for piece_line,fragment in pieces)
                    inherited_scan=SCAN.search(qualifier.text) if qualifier else None
                    metric_scan=scan or (scan_direction(inherited_scan))
                    if inherited_scan:
                        refs.append((self.ref(qualifier_line,qualifier,"condition","table_header",table),"table_header"))
                    local_conditions = dict(self.table_conditions.get((line.page, table), {}))
                    for scan_cell in line.cells:
                        if SCAN.fullmatch(scan_cell.text):
                            refs.append((self.ref(line, scan_cell, "condition", "table_cell", table), "table_cell"))
                    for foot in self.lines:
                        gap = line.span.bbox[1] - foot.span.bbox[3]
                        if foot.page == line.page and 0 < gap < 110 and FOOTNOTE.match(foot.text):
                            foot_unit = metric_unit(metric, foot.text)
                            if (METRIC.search(foot.text) or any(mark in cell.text for mark in "*†‡")):
                                refs.append((self.ref(foot, foot.span, "unit" if foot_unit else "condition", "footnote", table), "footnote"))
                                applies=any(mark in cell.text and foot.text.startswith(mark) for mark in "*†‡") or bool(re.search(r"\ball (?:devices?|measurements?|scans?)\b",foot.text,re.I))
                                if applies:
                                    local_conditions.update(self.conditions_for(foot))
                                    if not unit and foot_unit:
                                        unit=foot_unit.group()
                    cohort = (line.page, table) if (line.page, table) in self.table_cohorts else None
                    linked = self.comparison_measurement(metric, match, unit, label.text) if (
                        device is None and reference and OWN_WORK.fullmatch(reference.text)) else None
                    measured_device = linked["device_id"] if linked else device
                    pair_is_summary = bool(alternate and id(line) in self.table_rows
                                           and number(alternate[1]) >= number(match["n"]))
                    row_kind = row_aggregation or ("mean" if pair_is_summary else None)
                    result_id = self.result(metric, match, line, cell, unit, refs, measured_device, metric_scan, origin,
                                            cited, heading+" "+header_cell.text, local_conditions, row_kind,
                                            numeric_override=scientific_cell_value(cell), cohort=cohort,
                                            linked_measurement=linked["measurement_id"] if linked else None)
                    if origin == "primary" and measured_device is None:
                        ambiguous.append(result_id)
                    if alternate:
                        other=cell.part(match.end()+alternate.start(1),match.end()+alternate.end(1))
                        other_refs=[(self.ref(line,other,"value","table_cell",table),"table_cell"),*refs[1:]]
                        value_aggregation = "champion" if (
                            re.search(r"\b(?:champion|best|record)\b",heading,re.I)
                            and re.search(r"\bparenthes(?:es|is)\b",heading,re.I)) or pair_is_summary else "unknown"
                        other_id=self.result(metric,VALUE.fullmatch(other.text),line,other,unit,other_refs,device,
                                             metric_scan,origin,cited,heading,local_conditions,value_aggregation,cohort=cohort)
                        if origin == "primary" and device is None:
                            ambiguous.append(other_id)
                if used:
                    continue
            if table_right is not None and headers and wrapped_table_line(self.lines,index,headers,table_left,table_right):
                continue
            if FOOTNOTE.match(line.text):
                continue
            table, heading, headers, subheaders = None, "", {}, {}
            transposed_labels, table_left = [], None
            table_right, reference_header = None, None
            span = line.span
            device_match = DEVICE.search(line.text)
            explicit_device = self.known_device(device_match[1]) if device_match else None
            if not explicit_device and len(self.devices) == 1 and "Образец без обозначения" in self.devices:
                explicit_device = next(iter(self.devices.values()))
            for metric, match, descriptor_end in prose_metrics(line.text):
                if metric in ("PCE","retained_PCE") and not same_prose_column(line,descriptor_end-1,match.start("n")):
                    continue
                value_fragment = span.part(match.start(), match.end())
                unit_match = UNIT.match(line.text, match.end() + len(line.text[match.end():]) - len(line.text[match.end():].lstrip()))
                unit = unit_match.group() if unit_match else None
                unit_offset=0
                if not unit:
                    # Units printed alongside the metric name still belong to the value.
                    unit_match=metric_unit(metric,line.text[descriptor_end:match.start()])
                    if unit_match:
                        unit=unit_match.group()
                        unit_offset=descriptor_end
                refs = [(self.ref(line, value_fragment, "value"), "text")]
                if unit:
                    refs.append((self.ref(line, span.part(unit_match.start()+unit_offset,unit_match.end()+unit_offset), "unit"), "text"))
                if device_match:
                    refs.append((self.ref(line, span.part(*device_match.span()), "sample_label"), "text"))
                scan_match = SCAN.search(line.text)
                if scan_match:
                    refs.append((self.ref(line, span.part(*scan_match.span()), "condition"), "text"))
                origin = "cited" if re.search(r"\bpreviously reported\b|\bet al\.", line.text, re.I) else "primary"
                result_id = self.result(metric, match, line, value_fragment, unit, refs, explicit_device if metric != "Eg" and origin == "primary" else None,
                                        scan_direction(scan_match), origin,
                                        self.cited_publication(line.text) if origin=="cited" else None)
                if metric != "Eg" and not explicit_device and origin == "primary":
                    ambiguous.append(result_id)
        self.extra_prose()
        self.contextual_experiments()
        for result_id in ambiguous:
            self.db.execute("UPDATE measurement_result SET candidate_measurement_ids=? WHERE id=? AND manual=0",
                            (json.dumps(sorted(self.measurements)), result_id))
