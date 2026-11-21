"""Consolidate repeated readings of one printed source without deleting originals."""
from collections import defaultdict
from itertools import combinations
import re

from . import domain as d
from .extraction_options import enabled
from .store import insert, rows


TABLE_CITATION = re.compile(r"\b(?:as\s+(?:shown|listed|reported|summarized|given)|are\s+(?:shown|listed|summarized|given))"
                            r"\s+in\s+(?:the\s+)?Table\s+(\d+[a-z]?)\b",re.I)


def same_fragment(a, b):
    ax,ay,ar,at = a
    bx,by,br,bt = b
    width = min(ar-ax,br-bx)
    height = min(at-ay,bt-by)
    return (width>0 and height>0 and min(ar,br)-max(ax,bx)>=.8*width
            and min(at,bt)-max(ay,by)>=.6*height
            and abs((ax+ar)-(bx+br))<=max(ar-ax,br-bx)
            and abs((ay+at)-(by+bt))<=max(at-ay,bt-by))


def compatible(a,b):
    if a["manual"] and b["manual"]:
        return False
    for name in ("device_id","measurement_id","cited_publication_id","time_point","time_unit"):
        if a[name] is not None and b[name] is not None and a[name]!=b[name]:
            return False
    return "unknown" in (a["aggregation"],b["aggregation"]) or a["aggregation"]==b["aggregation"]


def merge_sources(connection,primary,other):
    for link in rows(connection,"SELECT source_reference_id FROM result_source WHERE result_id=?",other):
        insert(connection,"result_source",result_id=primary,**link)
    connection.execute("UPDATE measurement_result SET duplicate_of=? WHERE id=?",(primary,other))


def recorded_value(record):
    value=record["normalized_value"]
    if value is not None:
        return record["metric_name"],round(value,8),record["normalized_unit"]
    return None


def consolidate_table_citations(connection,version,lines):
    """Link an explicitly referenced table row only if two reported metrics uniquely identify it."""
    if not enabled("citations"):
        return
    table_rows, prose = defaultdict(lambda:defaultdict(list)), defaultdict(list)
    for record in rows(connection,"""SELECT r.*,s.source_page,s.source_table,s.bbox
        FROM measurement_result r JOIN result_source l ON l.result_id=r.id
        JOIN source_reference s ON s.id=l.source_reference_id
        WHERE r.pdf_version_id=? AND r.duplicate_of IS NULL AND r.deleted=0
          AND r.acquisition_method='published_numeric' AND r.normalized_value IS NOT NULL AND s.fragment_role='value'
        ORDER BY r.rowid""",version):
        table=record["source_table"]
        if table and record["device_id"] and record["extraction_method"] in ("geometry_table","local_ocr"):
            group=(record["source_page"],record["device_id"],record["measurement_id"])
            measured_y=(record["bbox"][1]+record["bbox"][3])/2
            cohorts=table_rows[table.lower()]
            anchor=next((row for row in cohorts if row[:3]==group and abs(row[3]-measured_y)<=3),
                        (*group,measured_y))
            cohorts[anchor].append(record)
        elif not table and not record["manual"] and record["extraction_method"] in ("pdf_text","pdf_prose_context"):
            prose[record["source_page"]].append(record)
    for index,line in enumerate(lines):
        if not (citation:=TABLE_CITATION.search(line.text)) or citation[1].lower() not in table_rows:
            continue
        nearby=[other for other in lines[max(0,index-1):index+3] if other.page==line.page
                and len(other.cells)==1 and abs(other.span.bbox[0]-line.span.bbox[0])<18
                and abs(other.span.bbox[1]-line.span.bbox[1])<42]
        observations={r["id"]:r for r in prose[line.page] if r["duplicate_of"] is None and any(
            same_fragment(r["bbox"],near.span.bbox) or
            (near.span.bbox[0]<=r["bbox"][0]<=r["bbox"][2]<=near.span.bbox[2]
             and abs(r["bbox"][1]-near.span.bbox[1])<3)
            for near in nearby)}
        comparisons = []
        for table_row in table_rows[citation[1].lower()].values():
            pairs=[(table_result,text_result) for table_result in table_row for text_result in observations.values()
                   if table_result["result_origin"]==text_result["result_origin"] and compatible(table_result,text_result)
                   and recorded_value(table_result)==recorded_value(text_result)]
            if len(pairs)>=2 and len({pair[0]["metric_name"] for pair in pairs})==len(pairs):
                comparisons.append(pairs)
        if len(comparisons)!=1:
            continue
        for primary,duplicate in comparisons[0]:
            if duplicate["duplicate_of"] is None:
                merge_sources(connection,primary["id"],duplicate["id"])
                duplicate["duplicate_of"]=primary["id"]


def consolidate(connection, version):
    references = defaultdict(list)
    buckets = defaultdict(list)
    for record in rows(connection,"""SELECT r.*,s.source_page,s.bbox FROM measurement_result r
        JOIN result_source l ON l.result_id=r.id JOIN source_reference s ON s.id=l.source_reference_id
        WHERE r.pdf_version_id=? AND r.deleted=0 AND r.duplicate_of IS NULL
          AND r.acquisition_method='published_numeric' AND s.fragment_role='value'
        ORDER BY r.manual DESC, CASE WHEN r.measurement_id IS NOT NULL THEN 0 ELSE 1 END,
          CASE WHEN r.extraction_method='geometry_table' THEN 0 ELSE 1 END, r.rowid""",version):
        ident = record["id"]
        references[ident].append((record["source_page"],record["bbox"]))
        if len(references[ident])>1:
            continue
        value,unit = record["normalized_value"],record["normalized_unit"]
        if value is None:
            value,unit = record["metric_value"],record["metric_unit"]
        if value is None:
            continue
        signature = (record["metric_name"],record["result_origin"],round(value,8),unit,
                     record["inequality_op"],record["range_min"],record["range_max"],record["spread_value"])
        buckets[signature].append(record)
    for matches in buckets.values():
        groups = []
        for record in matches:
            anchors = references[record["id"]]
            possible = [group for group in groups if all(compatible(other,record) and any(
                page==other_page and same_fragment(box,other_box)
                for page,box in anchors for other_page,other_box in references[other["id"]]
            ) for other in group)]
            if len(possible)==1:
                possible[0].append(record)
            else:
                groups.append([record])
        for primary,*duplicates in groups:
            for other in duplicates:
                merge_sources(connection,primary["id"],other["id"])


def flag_reading_conflicts(connection, version):
    """Request review when different readers disagree over the same printed value."""
    if not enabled("reading-conflicts"):
        return
    groups = defaultdict(list)
    for record in rows(connection, """SELECT r.*,s.source_page,s.bbox FROM measurement_result r
        JOIN result_source l ON l.result_id=r.id JOIN source_reference s ON s.id=l.source_reference_id
        WHERE r.pdf_version_id=? AND r.deleted=0 AND r.duplicate_of IS NULL
          AND r.acquisition_method='published_numeric' AND s.fragment_role='value'
          AND r.normalized_value IS NOT NULL""", version):
        group = (record["source_page"], record["metric_name"], record["normalized_unit"], record["result_origin"])
        groups[group].append(record)

    for records in groups.values():
        for left, right in combinations(records, 2):
            if (left["id"] == right["id"] or left["extraction_method"] == right["extraction_method"]
                    or not compatible(left, right) or not same_fragment(left["bbox"], right["bbox"])
                    or recorded_value(left) == recorded_value(right)):
                continue
            for current, other in ((left, right), (right, left)):
                if current["manual"]:
                    continue
                unit = other["raw_unit"] or other["metric_unit"] or ""
                value = other["raw_value"] or str(other["metric_value"])
                reading = f"{value} {unit}".strip()
                reason = f"Другой способ извлечения прочитал тот же фрагмент как «{reading}»."
                connection.execute(
                    """UPDATE measurement_result SET extraction_status=?, note=trim(coalesce(note, '') || ' ' || ?)
                       WHERE id=? AND manual=0 AND instr(coalesce(note, ''), ?)=0""",
                    (d.REVIEW, reason, current["id"], reason),
                )
