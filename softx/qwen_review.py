"""Check the printed PDF evidence for local-model results."""
from collections import defaultdict
import re

from . import domain as d
from .extract import PATTERN, UNIT, VALUE, locate_prose_span, prose_windows
from .store import insert, rows

LABEL = re.compile(
    rf"(?<!\w)(?:{PATTERN}|(?P<FF_alias>filling factor)|"
    r"(?P<PCE_alias>(?:photovoltaic|device|conversion|champion|cell) efficienc(?:y|ies)))(?!\w)",
    re.I,
)
BEFORE = re.compile(
    r"[\s(),:=≈~]*(?:(?:of|was|is|were|are|at|to|be|reached|yielded|achieved|obtained|"
    r"exhibited|measured|evaluated|calculated|estimated|about|approximately|around|approx)"
    r"\b[\s(),.:=≈~]*)*",
    re.I,
)
AFTER = re.compile(
    r"[\s,(:]*(?:(?:which\s+)?(?:represents?|corresponds?\s+to|is|was|for|as)"
    r"\s+(?:(?:the|a)\s+)?)?(?:champion\s+|average\s+|mean\s+)?",
    re.I,
)
UNRELATED = re.compile(
    r"\b(?:relative|loss|deficit|retention|improvement|increase|decrease|reduction|"
    r"variation|uncertainty|tolerance|error|ratio|tick|axis)\b|[Δ∆]",
    re.I,
)
RELATIVE_PREFIX = re.compile(
    r"\b(?:relative (?:improvement|increase|decrease|reduction)|percent(?:age)? change)"
    r"\b[^%,;!?]{0,35}$",re.I)
MEASURED_LINK = re.compile(
    r"\b(?:to|equals?|is|was|are|were|reached|yielded|achieved|obtained)"
    r"\s*(?:(?:about|approximately|around)\s+)?$",re.I)
OTHER_QUANTITY = re.compile(rf"(?<![\w.]){d.NUM}(?![\w.])|,|;",re.I)
SAMPLE = re.compile(r"\b(?:sample|device|cell)\s+([\w.-]+)",re.I)
BOUNDARY = re.compile(r"[!?;]|\.(?=\s+[A-Z])")


def labels_for_value(text, value, unit):
    labels = {}
    for label in LABEL.finditer(text):
        name = label.lastgroup.removesuffix("_alias")
        if d.normalize(name,1,unit.group())[0] is None:
            continue
        if label.end() <= value.start("n"):
            bridge = text[label.end():value.start("n")]
            bridge = re.sub(r"\bi\.e\.,?\s*", "", bridge, flags=re.I)
            direct = len(bridge)<=42 and BEFORE.fullmatch(bridge)
            described = (len(bridge)<=75 and MEASURED_LINK.search(bridge) and not OTHER_QUANTITY.search(bridge)
                         and not UNRELATED.search(bridge) and not LABEL.search(bridge))
            if direct or described:
                labels[name]=label.span()
        elif unit.end() <= label.start():
            bridge = text[unit.end():label.start()]
            if len(bridge)<=45 and AFTER.fullmatch(bridge):
                labels[name]=label.span()
    return labels


def approve_qwen_evidence(extraction):
    connection = extraction.db
    version = extraction.version
    candidates = defaultdict(list)
    for row in rows(connection, """SELECT r.*,s.source_page,s.bbox FROM measurement_result r
        JOIN result_source l ON l.result_id=r.id JOIN source_reference s ON s.id=l.source_reference_id
        WHERE r.pdf_version_id=? AND r.extraction_method=? AND r.manual=0 AND r.deleted=0
        AND r.duplicate_of IS NULL AND r.normalized_value IS NOT NULL AND s.fragment_role='value'""",
        version,d.AI_EXTRACTION_METHODS["local"]):
        candidates[row["source_page"],tuple(row["bbox"])].append(row)
    if not candidates:
        return

    devices = {r["device_label"].casefold():r["id"] for r in rows(connection,
        "SELECT id,device_label FROM device WHERE publication_id=?",extraction.pub)}
    approved = set()
    lines = getattr(extraction,"body_lines",extraction.lines)
    for text,group in prose_windows(lines):
        for value in VALUE.finditer(text):
            if value["op"] or value["end"]:
                continue
            hit = locate_prose_span(group,*value.span("n"))
            if not hit or hit[0].ocr or hit[0].image_annotation:
                continue
            line,span = hit
            records = candidates.get((line.page,tuple(span.bbox)))
            if not records:
                continue
            tail = text[value.end():]
            unit = UNIT.match(text,value.end()+len(tail)-len(tail.lstrip()))
            unit_source = locate_prose_span(group,*unit.span()) if unit else None
            if not unit_source or unit_source[0].ocr:
                continue
            start = max((m.end() for m in BOUNDARY.finditer(text,0,value.start())),default=0)
            boundary = BOUNDARY.search(text,unit.end())
            end = boundary.start() if boundary else len(text)
            sentence = text[start:end]
            labels = {metric:loc for metric,loc in labels_for_value(text,value,unit).items()
                      if start<=loc[0] and loc[1]<=end}
            if len(labels)!=1:
                continue
            metric,location = next(iter(labels.items()))
            # Scope words like "loss" and "relative" can change the meaning of a literal value.
            around = text[max(start,min(location[0],value.start("n"))-22):min(end,max(location[1],unit.end())+20)]
            evidence = locate_prose_span(group,*location)
            if (UNRELATED.search(around) or RELATIVE_PREFIX.search(text[start:value.start("n")])
                    or not evidence or evidence[0].ocr):
                continue
            samples = {devices[m[1].casefold()] for m in SAMPLE.finditer(sentence) if m[1].casefold() in devices}
            for record in records:
                if (record["id"] in approved or record["metric_name"]!=metric
                        or (record["raw_value"] or "").strip()!=span.text.strip() or record["raw_unit"]!=unit.group()):
                    continue
                if record["result_origin"]=="cited" and not record["cited_publication_id"]:
                    continue
                if metric!="Eg" and record["result_origin"]=="primary":
                    if len(samples)>1 or (samples and record["device_id"] and record["device_id"] not in samples):
                        continue
                device = record["device_id"]
                if metric!="Eg" and record["result_origin"]=="primary" and len(samples)==1:
                    device = next(iter(samples))
                insert(connection,"result_source",result_id=record["id"],
                       source_reference_id=extraction.ref(*evidence,"condition"))
                connection.execute("""UPDATE measurement_result SET extraction_status=?,value_state=?,
                    device_id=?,note=NULL WHERE id=? AND manual=0 AND deleted=0 AND extraction_status=?""",
                    (d.AUTO,"numeric_zero" if record["metric_value"]==0 else "numeric",device,record["id"],d.REVIEW))
                approved.add(record["id"])
