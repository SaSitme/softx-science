"""Deterministic extraction from text geometry, with local evidence for every result."""
import json
import re

from . import domain as d
from .store import insert, key, one


PATTERN = "|".join(f"(?P<{name}>{pattern})" for name, pattern in d.METRICS.items())
METRIC = re.compile(rf"(?<![\w])(?:{PATTERN})(?![\w])", re.I)
UNIT = re.compile(d.UNIT_PATTERN)
VALUE = re.compile(rf"(?P<op><=|>=|[<>≤≥])?\s*(?P<n>{d.NUM})(?:\s*(?:±|\+/-)\s*(?P<spread>{d.NUM})|\s*(?:–|—|\bto\b|-)\s*(?P<end>{d.NUM}))?")
DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)
SCAN = re.compile(r"\b(forward|reverse)\b", re.I)
DEVICE = re.compile(r"\b(?:device|sample|cell)\s+([A-Z0-9][\w.-]{0,20})(?=\s*[:(,]|\s+(?:reverse|forward)\b)", re.I)
TABLE = re.compile(r"^Table\s+(\w+)\b", re.I)
FOOTNOTE = re.compile(r"^(?:[*†‡]|Note\s*:|\[[a-z]\])")

CURRENT_DENSITY = re.compile(r"\bcurrent\s+density\s*(?:of\s+|[:=]\s*)", re.I)
SHORT_CIRCUIT_SUFFIX = re.compile(r"[^.!?;]{0,65}?\b(?:at|under)\s+(?:the\s+)?short[- ]circuit\b", re.I)


def prose_metrics(text):
    value_offsets = set()
    for metric_match in METRIC.finditer(text):
        connector = re.match(
            r"\s*(?:\([^)]{1,20}\)\s*)?(?:(?:[=:~≈]|of\b|was\b|is\b|reached\b|at\b)\s*)?",
            text[metric_match.end():], re.I,
        )
        value_start = metric_match.end() + connector.end()
        value = VALUE.match(text, value_start)
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


def bibliographic_doi(metadata,lines):
    literal=metadata.get("/DOI")
    if literal:
        candidate=DOI.search(str(literal))
        if candidate:
            return candidate.group().rstrip(".,;").lower()
    for line in lines[:12]:
        if line.page!=1:
            return None
        match=re.search(r"\b(?:doi\s*[:=]|doi\.org/)\s*("+DOI.pattern+")",line.text,re.I)
        if match:
            return match[1].rstrip(".,;").lower()
    return None


def number(raw):
    raw=raw.replace("−","-")
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?",raw):
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


class Extraction:
    def __init__(self, connection, pub, version, lines):
        self.db, self.pub, self.version, self.lines = connection, pub, version, lines
        self.material_id = None
        self.devices = {}
        self.measurements = {}
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
        title = metadata.get("/Title") or next((item.text for item in self.lines if "SYNTHETIC TEST" not in item.text), None)
        doi = bibliographic_doi(metadata,self.lines)
        author = metadata.get("/Author")
        year = re.search(r"(?:©|copyright|published(?: online)?\s+(?:in\s+)?)\s*(20\d{2}|19\d{2})", text, re.I)
        journal = re.search(r"^Journal\s*:\s*(.+)$", text, re.I | re.M)
        fixture = any("SYNTHETIC TEST DOCUMENT" in line.text for line in self.lines[:5])
        self.db.execute("""UPDATE publication SET title=COALESCE(?,title), doi=COALESCE(?,doi), authors=COALESCE(?,authors),
            year=COALESCE(?,year), journal=COALESCE(?,journal), is_fixture=MAX(is_fixture,?),
            corpus_status=CASE WHEN corpus_manual=0 THEN ? ELSE corpus_status END,
            corpus_reason=CASE WHEN corpus_manual=0 THEN ? ELSE corpus_reason END WHERE id=?""",
                        (title, doi, None if not author else json.dumps([x.strip() for x in re.split(r";|\band\b", author) if x.strip()]),
                         int(year[1]) if year else None, journal[1] if journal else None, fixture, corpus, reason, self.pub))
        effective=one(self.db,"SELECT corpus_status FROM publication WHERE id=?",self.pub)
        if effective["corpus_status"].startswith("excluded"):
            return False
        corpus=effective["corpus_status"]
        if material_class:
            self.material_id = key(self.pub, "material", material_class)
            raw = re.search(d.CLASSES[material_class], text, re.I).group()
            bandgap = re.search(rf"(?:E[ _]?g|band\s?gap)\s*(?:[=:]|of)\s*({d.NUM})\s*(m?eV)", text, re.I)
            method = re.search(r"band\s?gap method\s*:\s*([^.;\n]+)", text, re.I)
            dimension = re.search(r"\b([0123]D)\s+(?:perovskite|material)", text, re.I)
            formula = re.search(r"\b(?:MAPbI3|FAPbI3|CsPbI3|Sb2Se3|CdTe|Cu2ZnSnS4)\b", text)
            additives = re.search(r"\badditives?\s*:\s*([^.;\n]+)", text, re.I)
            eg, eg_unit = d.normalize("Eg", number(bandgap[1]), bandgap[2]) if bandgap else (None, None)
            insert(self.db, "material", id=self.material_id, publication_id=self.pub, material_name_raw=raw,
                   material_class=material_class, formula_normalized=formula[0] if formula else None,
                   dimensionality=dimension[1] if dimension else None, additives=[v.strip() for v in additives[1].split(",")] if additives else None,
                   bandgap_value=eg, bandgap_unit=eg_unit, bandgap_method=method[1] if method else None)
        for line in self.lines:
            match = DEVICE.search(line.text)
            if match:
                self.device(match[1], line.text if "(" in line.text or "active area" in line.text.lower() else "")
        self.discover_table_devices()
        if not self.devices and corpus == "included":
            self.device("Образец без обозначения", device_type="single-junction thin-film"
                        if re.search(r"single[- ]junction", text, re.I) else None)
        for line in self.lines:
            layer = re.search(r"^Layer\s+(\d+)\s*:\s*([^/]+)\s*/\s*([^/]+)\s*/\s*([^/]+)\s*/\s*(.+)$", line.text, re.I)
            if layer and layer[2].strip() in self.devices:
                thickness, _, fabrication = layer[5].partition("/")
                val = re.search(rf"({d.NUM})\s*(nm|[µμu]m|Å)", thickness)
                amount, unit = d.normalize("thickness", number(val[1]), val[2]) if val else (None, None)
                insert(self.db, "device_layer", id=key(self.pub, "layer", layer[2].strip(), layer[1]),
                       device_id=self.devices[layer[2].strip()], layer_order=int(layer[1]), layer_function=layer[3].strip(),
                       layer_material=layer[4].strip(), layer_thickness=amount, thickness_unit=unit,
                       deposition_method=fabrication.split(";")[0].strip(" .") or None,
                       **self.named_conditions(fabrication, ("annealing_temperature", "annealing_time", "atmosphere")))
            layer_prose = re.search(rf"\b([\w₀-₉]+)\s+(ETL|HTL|absorber|contact)\s+layer\s*\(\s*({d.NUM})\s*(nm|[µμu]m|Å)\s*\)", line.text, re.I)
            label = DEVICE.search(line.text)
            if layer_prose and label and label[1] in self.devices:
                amount, unit = d.normalize("thickness", number(layer_prose[3]), layer_prose[4])
                insert(self.db, "device_layer", id=key(self.pub, "layer", line.page, line.span.bbox),
                       device_id=self.devices[label[1]], layer_function=layer_prose[2], layer_material=layer_prose[1],
                       layer_thickness=amount, thickness_unit=unit)
            if re.search(r"\b(?:all|each)\b.*\b(?:JV|J.V|scans?|measurements?)\b", line.text, re.I):
                self.conditions.update(self.conditions_for(line))
        return True

    def discover_table_devices(self):
        header=False
        cited=False
        for line in self.lines:
            if TABLE.match(line.text):
                cited=bool(re.search(r"literature|reported|previous|reference",line.text,re.I))
                header=False
            label=line.cells[0].text
            if len(line.cells)>=3 and re.fullmatch(r"(?:sample|device|cell)(?:\s+(?:name|id|label))?",label,re.I) and any(METRIC.search(c.text) for c in line.cells[1:]):
                header=True
                continue
            scalar_cells=sum(bool(VALUE.match(cell.text)) for cell in line.cells[1:])
            if not header or cited or scalar_cells<2:
                header=False
                continue
            if 0<len(label)<=46 and not re.match(r"^(?:mean|median|average|references?|\[|10\.)",label,re.I):
                self.device(label,label)

    def cited_publication(self,literal):
        doi=DOI.search(literal)
        if not doi:
            ref=re.search(r"\[\s*\d+\s*\]",literal)
            if ref:
                doi=next((DOI.search(line.text) for line in self.lines if line.text.lstrip().startswith(ref[0]) and DOI.search(line.text)),None)
        if not doi:
            return None
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
        spectrum = re.search(r"AM\s*1\.5\s*G", text, re.I)
        if spectrum:
            result["spectrum"] = "AM1.5G"
            result["spectrum_source_id"] = self.ref(line, line.span.part(*spectrum.span()), "condition")
        intensity = re.search(rf"({d.NUM})\s*(mW\s*/\s*cm(?:\^?2|²)|W\s*/\s*m(?:\^?2|²))", text)
        if intensity:
            result["light_intensity"], result["light_intensity_unit"] = d.normalize("Pin", number(intensity[1]), intensity[2])
            result["intensity_source_id"] = self.ref(line, line.span.part(*intensity.span()), "condition")
        if re.search(r"\b(?:independently certified|certified by)\b", text, re.I):
            result["certified"] = 1
        if re.search(r"\b(?:aperture mask|shadow mask|mask was used)\b", text, re.I):
            result["mask_used"] = 1
        return result

    def device(self, label, text="", device_type=None):
        if label in self.devices:
            return self.devices[label]
        ident = key(self.pub, "device", label)
        self.devices[label] = ident
        role = re.search(r"\b(control|target|record)\b", text, re.I)
        architecture = re.search(r"(?<!\w)(n-i-p|p-i-n|superstrate|substrate)(?!\w)", text, re.I)
        area = re.search(rf"(active|aperture|total)\s+area\s*[:=]?\s*({d.NUM})\s*([cm]m[²2])", text, re.I)
        v, u = d.normalize("area", number(area[2]), area[3]) if area else (None, None)
        encapsulation = re.search(r"\b(unencapsulated|encapsulated)\b", text, re.I)
        insert(self.db, "device", id=ident, publication_id=self.pub, material_id=self.material_id, device_label=label,
               device_role=role[1].lower() if role else None, device_type=device_type or ("single-junction thin-film" if architecture else None),
               architecture=architecture[1].lower() if architecture else None, active_area=v, area_unit=u,
               area_definition=area[1].lower() if area else None, encapsulation=encapsulation[1] if encapsulation else None,
               **self.named_conditions(text, ("substrate_type",)))
        return ident

    def measurement(self, device, scan, local=None, kind="JV"):
        if device is None:
            return None
        evidence = dict(self.conditions) if kind == "JV" else {}
        evidence.update(local or {})
        spectrum_source = evidence.pop("spectrum_source_id", None)
        signature = {k: v for k, v in evidence.items() if k != "intensity_source_id"}
        ident = key(self.version, device, scan, kind, sorted(signature.items()))
        self.measurements[ident] = device
        if spectrum_source:
            self.measurement_spectra.setdefault(ident, spectrum_source)
        insert(self.db, "measurement", id=ident, device_id=device, scan_direction=scan,
               measurement_type=kind, **evidence)
        return ident

    def result(self, metric, match, line, span, unit, refs, device, scan, origin, cited=None, extra="", conditions=None):
        value = number(match["n"]) if match else None
        op = match["op"] if match else None
        spread = number(match["spread"]) if match and match["spread"] else None
        end = number(match["end"]) if match and match["end"] else None
        low, high = sorted((value,end)) if end is not None else (None,None)
        local = self.conditions_for(line)
        local.update(conditions or {})
        measurement = self.measurement(device, scan, local, "stability" if metric == "T80" else "JV") if origin == "primary" and metric != "Eg" else None
        agg = aggregation(extra + " " + line.text)
        norm, norm_unit = d.normalize(metric, value, unit) if op is None and end is None else (None, None)
        ambiguous = metric != "Eg" and origin == "primary" and not device
        uncertain = ambiguous or (value is not None and norm is None) or (origin == "cited" and cited is None)
        state = "missing" if value is None else "ambiguous" if uncertain else "numeric_zero" if value == 0 else "numeric"
        raw_record = dict(value=span.text, unit=unit, spread_value=spread, range_min=low, range_max=high,
                          inequality_op=op, aggregation=agg, scan_direction=scan)
        count = re.search(r"\bn\s*=\s*(\d+)", extra + " " + line.text, re.I)
        point = re.search(rf"\bafter\s+({d.NUM})\s*(h|hours?|days?|min)\b", line.text, re.I)
        if count:
            raw_record["n_devices"] = int(count[1])
        if point:
            raw_record["time_point"], raw_record["time_unit"] = point[1], point[2]
        time, time_unit = d.normalize("T80", number(point[1]), point[2]) if point else (None, None)
        result_id = key(self.version, line.page, metric, span.bbox)
        insert(self.db, "measurement_result", id=result_id, publication_id=self.pub, pdf_version_id=self.version,
               material_id=self.material_id if origin == "primary" else None, device_id=device, measurement_id=measurement,
               metric_name=metric, metric_value=value, metric_unit=unit, raw_value=span.text, raw_unit=unit,
               raw_record=raw_record, normalized_value=norm, normalized_unit=norm_unit, aggregation=agg,
               value_state=state, spread_value=spread, range_min=low, range_max=high, inequality_op=op,
               n_devices=int(count[1]) if count else None, time_point=time, time_unit=time_unit,
               result_origin=origin, cited_publication_id=cited, extraction_status=d.REVIEW if uncertain else d.AUTO,
               extraction_method="geometry_table" if any(kind == "table_cell" for _, kind in refs) else "pdf_text")
        for ref, _ in refs:
            insert(self.db, "result_source", result_id=result_id, source_reference_id=ref)
        if measurement:
            conditions = one(self.db, "SELECT intensity_source_id FROM measurement WHERE id=?", measurement)
            for ref in (conditions["intensity_source_id"], self.measurement_spectra.get(measurement)):
                if ref:
                    insert(self.db, "result_source", result_id=result_id, source_reference_id=ref)
        if ambiguous:
            self.db.execute("UPDATE measurement_result SET candidate_device_ids=?,candidate_measurement_ids=? WHERE id=? AND manual=0",
                            (json.dumps(list(self.devices.values())), "[]", result_id))
        return result_id

    def parse(self):
        table, heading, headers, active_page = None, "", {}, None
        subheaders = {}
        ambiguous = []
        for line in self.lines:
            if active_page != line.page:
                table, heading, headers, subheaders = None, "", {}, {}
                active_page = line.page
            cap = TABLE.match(line.text)
            if cap:
                table, heading, headers, subheaders = cap[1], line.text, {}, {}
                continue
            metrics = [(cell, METRIC.search(cell.text)) for cell in line.cells]
            if table and not headers and not any(match for _,match in metrics) and len(line.cells)==1:
                heading+=" "+line.text
                continue
            if len(line.cells) >= 2 and any(match for _, match in metrics):
                candidate = {cell.bbox[0]: (line, cell, match.lastgroup) for cell, match in metrics if match}
                if len(candidate) >= 2 or table:
                    headers.update(candidate)
                    subheaders={}
                    continue
            if headers and len(line.cells)>=2:
                # A lower header row can hold unit cells or explicit scan-direction subcolumns.
                qualifiers=[cell for cell in line.cells
                            if SCAN.fullmatch(cell.text) or UNIT.fullmatch(cell.text.strip(" ()[]"))]
                if len(qualifiers)>=2 and not any(VALUE.match(cell.text) for cell in line.cells):
                    for cell in qualifiers:
                        center=(cell.bbox[0]+cell.bbox[2])/2
                        (header_line,header_cell,metric)=min(headers.values(),
                            key=lambda item:abs(sum(item[1].bbox[::2])/2-center))
                        subheaders[cell.bbox[0]]=(header_line,header_cell,metric,line,cell)
                    continue
            if headers and len(line.cells)<=2 and all(len(cell.text)<25 for cell in line.cells) and not any(VALUE.match(cell.text) for cell in line.cells) and not re.search(r"[.!?]",line.text):
                continue
            if headers and len(line.cells) >= 2:
                scan = SCAN.search(line.text)
                scan = scan[1].lower() if scan else None
                label = line.cells[0]
                origin = "cited" if re.search(r"literature|reported|previous|reference", heading, re.I) or DOI.search(label.text) else "primary"
                cited = None
                if origin=="cited":
                    cited=self.cited_publication(label.text)
                device = self.devices.get(label.text) if origin == "primary" else None
                if not device and origin=="primary" and table and len(label.text)<46 and not VALUE.fullmatch(label.text) and not SCAN.fullmatch(label.text) and not re.search(r"[.!?]",label.text):
                    device=self.device(label.text,label.text)
                used = False
                columns=subheaders or {x:(header_line,header_cell,metric,None,None)
                                       for x,(header_line,header_cell,metric) in headers.items()}
                for x, (header_line, header_cell, metric, qualifier_line, qualifier) in columns.items():
                    cell = min(line.cells, key=lambda item: abs(item.bbox[0]-x))
                    if abs(cell.bbox[0]-x) > 32:
                        continue
                    match = VALUE.match(cell.text)
                    if not match and cell.text.strip() not in ("—", "–", "-", "N/A", "NA"):
                        continue
                    used = True
                    unit_match = None
                    refs = [(self.ref(line, cell, "value", "table_cell", table), "table_cell")]
                    if label is not cell:
                        refs.append((self.ref(line, label, "sample_label", "table_cell", table), "table_cell"))
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
                    inherited_scan=SCAN.search(qualifier.text) if qualifier else None
                    metric_scan=scan or (inherited_scan[1].lower() if inherited_scan else None)
                    if inherited_scan:
                        refs.append((self.ref(qualifier_line,qualifier,"condition","table_header",table),"table_header"))
                    local_conditions={}
                    for scan_cell in line.cells[1:]:
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
                    result_id = self.result(metric, match, line, cell, unit, refs, device, metric_scan, origin, cited, heading+" "+header_cell.text,local_conditions)
                    if origin == "primary" and device is None:
                        ambiguous.append(result_id)
                if used:
                    continue
            if FOOTNOTE.match(line.text):
                continue
            table, heading, headers, subheaders = None, "", {}, {}
            span = line.span
            device_match = DEVICE.search(line.text)
            explicit_device = self.devices.get(device_match[1]) if device_match else None
            if not explicit_device and len(self.devices) == 1 and "Образец без обозначения" in self.devices:
                explicit_device = next(iter(self.devices.values()))
            for metric, match, descriptor_end in prose_metrics(line.text):
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
                                        scan_match[1].lower() if scan_match else None, origin,
                                        self.cited_publication(line.text) if origin=="cited" else None)
                if metric != "Eg" and not explicit_device and origin == "primary":
                    ambiguous.append(result_id)
        for result_id in ambiguous:
            self.db.execute("UPDATE measurement_result SET candidate_measurement_ids=? WHERE id=? AND manual=0",
                            (json.dumps(list(self.measurements)), result_id))
