"""Flag likely rounded reprints while keeping each printed measurement."""
from collections import defaultdict
from itertools import combinations
from decimal import Decimal
import re

from . import domain as d
from .extraction_options import enabled
from .store import rows

REVIEW_REASON = "В другой таблице есть совпадающий набор показателей с более точной записью. Проверьте, относится ли строка к тому же измерению."
DECIMAL = re.compile(r"[+−-]?\d+(?:\.\d+)?")


def rounded_observation(record):
    literal=record["raw_value"] or ""
    if not DECIMAL.fullmatch(literal):
        return None
    unit=record["metric_unit"]
    factor,target=d.normalize(record["metric_name"],1,unit)
    if factor is None or target!=record["normalized_unit"]:
        return None
    places=max(0,-Decimal(literal.replace("−","-")).as_tuple().exponent)
    return record["normalized_value"],factor*10**-places


def flag_rounded_review(connection,version):
    if not enabled("rounded"):
        return
    groups=defaultdict(list)
    for record in rows(connection,"""SELECT r.*,s.source_page,s.source_table,s.bbox
        FROM measurement_result r JOIN result_source rs ON rs.result_id=r.id
        JOIN source_reference s ON s.id=rs.source_reference_id
        WHERE r.pdf_version_id=? AND r.deleted=0 AND r.duplicate_of IS NULL AND r.device_id IS NOT NULL
          AND r.normalized_value IS NOT NULL AND r.acquisition_method='published_numeric'
          AND s.source_type='table_cell' AND s.source_table IS NOT NULL AND s.fragment_role='value'""",version):
        if number:=rounded_observation(record):
            scope=(record["source_table"],record["source_page"],record["device_id"],record["measurement_id"],record["result_origin"])
            y=(record["bbox"][1]+record["bbox"][3])/2
            cohorts=groups[scope]
            row=next((values for center,values in cohorts if abs(center-y)<=3),None)
            if row is None:
                row={}
                cohorts.append((y,row))
            row[record["metric_name"]]=(record,*number)
    tables=[(key,cohort) for key,entries in groups.items() for _,cohort in entries if len(cohort)>=4]
    for (left,a),(right,b) in combinations(tables,2):
        if left[0]==right[0] or left[4]!=right[4]:
            continue
        metrics=a.keys()&b.keys()
        if len(metrics)<4 or not (metrics & {"Voc","Jsc","FF","PCE"}):
            continue
        pairs=[(a[m],b[m]) for m in metrics]
        if any(abs(v-w)>.5*max(s,t)+1e-9 or first["normalized_unit"]!=second["normalized_unit"]
               for (first,v,s),(second,w,t) in pairs):
            continue
        if not any(abs(v-w)<=1e-9 for (_,v,_),(_,w,_) in pairs):
            continue
        for oriented in (pairs,[(second,first) for first,second in pairs]):
            changed=[(first,s,t) for (first,v,s),(_,w,t) in oriented if abs(v-w)>1e-9]
            if len(changed)<2 or any(s<=t for _,s,t in changed):
                continue
            for record,_,_ in changed:
                note=record["note"] or ""
                if record["manual"] or REVIEW_REASON in note:
                    continue
                connection.execute("UPDATE measurement_result SET extraction_status=?,note=? WHERE id=? AND manual=0",
                                   (d.REVIEW," ".join(filter(None,(note,REVIEW_REASON))),record["id"]))
