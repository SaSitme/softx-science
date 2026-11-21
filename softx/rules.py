"""Thin-film PV rules and explicit comparison gates."""
import math
import re
from collections import defaultdict
from itertools import combinations

from .domain import normalize
from .spectrum import REFERENCE, detailed_balance
from .store import rows
from .validation import Finding

PCE_TOL=.03
EQE_TOL=.05
AREA_TOL=.05
RULES={
    "PCE_CONSISTENCY":"Согласованность КПД",
    "VOC_BANDGAP":"Напряжение холостого хода и ширина запрещённой зоны",
    "PHYSICAL_RANGE":"Физические границы",
    "SHOCKLEY_QUEISSER":"Предел однопереходного элемента",
    "EQE_JSC":"Плотность тока по EQE и ВАХ",
    "BATCH_CHAMPION":"Лучший прибор и среднее по партии",
    "SCAN_ISOLATION":"Раздельность направлений развёртки",
    "CROSS_SOURCE":"Сверка источников",
}


def val(r):
    return None if not r or r["value_state"] not in ("numeric","numeric_zero") else r["normalized_value"]


def item(name,value,unit=None):
    return {"name":name,"value":value,"unit":unit}


def observed(record):
    return item(record["metric_name"],val(record),record["normalized_unit"])


def finding(code, anchor, records, status, description, values=None, threshold=None, sources=None, hypothesis=None):
    return Finding(code,anchor,status,description,records,values if values is not None else [observed(r) for r in records if r],
                   threshold,sources or [],hypothesis)


def unique(records, metric, aggregation):
    filtered=[r for r in records if r["metric_name"]==metric and r["aggregation"]==aggregation and val(r) is not None
              and r["acquisition_method"]=="published_numeric"]
    return filtered[0] if len(filtered)==1 else None


def one_sun(record):
    return record["spectrum"]=="AM1.5G" and record["light_intensity_unit"]=="mW/cm²" and record["light_intensity"] is not None and math.isclose(record["light_intensity"],100,abs_tol=1e-7) and bool(record["intensity_source_id"])


def measurement_match(lhs, rhs, fields):
    return all(lhs.get(field) is not None and lhs.get(field)==rhs.get(field) for field in fields)


def gap_for(record, eg_by_material):
    values=eg_by_material.get(record["material_id"],[])
    return values[0] if len(values)==1 and val(values[0]) and val(values[0])>0 else None


def precision(r):
    raw=r.get("raw_value")
    match=re.search(r"[+-]?\d+(?:[.,](\d+))?(?:[Ee]([+-]?\d+))?",raw or "")
    if not match or r["manual"]:
        return 0
    scale=10**max(-100,min(100,int(match[2] or 0)-len(match[1] or "")))
    return abs(normalize(r["metric_name"],scale/2,r["raw_unit"])[0] or 0)


def physical(r, gap):
    value,metric=val(r),r["metric_name"]
    if value is None:
        return
    if metric=="FF":
        severity,message,limits=("error" if not 0<value<=100 else "info","FF должен удовлетворять 0 < FF ≤ 100%.",{"min_exclusive":0,"max":100,"unit":"%"})
    elif metric=="PCE":
        severity,message,limits=("error" if not 0<=value<=100 else "info","КПД должен находиться в диапазоне 0–100%.",{"min":0,"max":100,"unit":"%"})
    elif metric=="Jsc":
        pin=r["light_intensity"]
        if pin is None or not r["intensity_source_id"] or not gap or r["light_intensity_unit"]!="mW/cm²":
            yield finding("PHYSICAL_RANGE",r["id"],[r],"not_applicable",
                          "Для энергетической границы |Jsc| нужны подтверждённые Pin и Eg этого материала.")
            return
        ceiling=pin/val(gap)
        yield finding("PHYSICAL_RANGE",r["id"],[r,gap],"error" if abs(value)>ceiling else "info",
                      "Энергетическая граница |Jsc| × Eg/q ≤ Pin для одного собранного носителя на фотон.",
                      [observed(r),observed(gap),item("Pin",pin,"mW/cm²")],{"max":ceiling,"unit":"mA/cm²"},
                      [r["intensity_source_id"]])
        return
    else:
        return
    yield finding("PHYSICAL_RANGE",r["id"],[r],severity,message,threshold=limits)


def same_pce_observation(records, references, verified):
    if any(r["condition_label"] for r in records) and len({r["condition_label"] for r in records})==1:
        return True
    if all(r["id"] in verified for r in records):
        return True
    anchors=[references.get(r["id"]) for r in records]
    if any(ref is None for ref in anchors):
        return False
    first=anchors[0]
    ordinate=lambda ref: (ref["bbox"][1]+ref["bbox"][3])/2
    return all(ref["source_page"]==first["source_page"] and ref["source_type"]==first["source_type"]
               and ref["source_table"]==first["source_table"] and abs(ordinate(ref)-ordinate(first))<4 for ref in anchors)


def pce(r, group, references, verified):
    related=[unique(group,k,r["aggregation"]) for k in ("Voc","Jsc","FF")]
    records=[r,*[a for a in related if a]]
    threshold={"relative":PCE_TOL,"denominator":"abs(PCE_calc)"}
    problems=[]
    if not r["measurement_id"]:
        problems.append("не подтверждена привязка к одному измерению")
    if not r["scan_direction"]:
        problems.append("не указан ход развёртки")
    for m, rec in zip(("Voc","Jsc","FF"),related):
        if not rec:
            problems.append(f"нет единственного нормализованного {m} в том же измерении и с той же агрегацией")
    pin=r["light_intensity"]
    if pin is None or pin<=0 or r["light_intensity_unit"]!="mW/cm²" or not r["intensity_source_id"]:
        problems.append("нет подтверждённой Pin в мВт/см² для этого измерения")
    if val(r) is None:
        problems.append("КПД не является однозначным числом")
    if all(related) and not same_pce_observation(records,references,verified):
        problems.append("фрагменты разнесены и не подтверждена их принадлежность одному наблюдению")
    if all(related) and (val(related[0])<=0 or val(related[1])==0 or not 0<val(related[2])<=100):
        problems.append("исходные Voc, Jsc или FF не задают положительный расчётный КПД")
    values=[observed(x) for x in records]+[item("Pin",pin,r["light_intensity_unit"])]
    if problems:
        return finding("PCE_CONSISTENCY",r["id"],records,"not_applicable",
                       "Проверка невозможна: "+"; ".join(problems)+".",values,threshold,[r["intensity_source_id"]])
    voc,jsc,ff=map(val,related)
    calculated=100*voc*abs(jsc)*(ff/100)/pin
    delta=abs(val(r)-calculated)/abs(calculated)
    values.extend([item("PCE_calc",calculated,"%"),item("Расхождение",100*delta,"%")])
    message="PCE_calc = 100 × Voc × |Jsc| × (FF/100) / Pin."
    return finding("PCE_CONSISTENCY",r["id"],records,"warning" if delta>PCE_TOL else "info",message,values,threshold,
                   [r["intensity_source_id"]],"Проверьте округление, единицы и выбор строки измерения." if delta>PCE_TOL else None)


def voc_gap(r, gap):
    if val(r) is None or gap is None or not r["device_id"]:
        return finding("VOC_BANDGAP",r["id"],[r],"not_applicable","Нет однозначной пары Voc и Eg для материала прибора.")
    violates=val(r)>=val(gap)
    return finding("VOC_BANDGAP",r["id"],[r,gap],"error" if violates else "info",
                   "Должно выполняться Voc < Eg/q.", threshold={"max_exclusive":val(gap),"unit":"V"},
                   hypothesis="Уточните метод определения Eg и принадлежность образца." if violates else None)


def sq(r,gap):
    temperature=25.0
    raw=r["temperature"]
    if raw:
        match=re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*°?([CK])\s*",raw,re.I)
        if not match:
            return finding("SHOCKLEY_QUEISSER",r["id"],[r,gap],"not_applicable","Температура прибора указана неоднозначно.")
        temperature=float(match[1].replace(",","."))-(273.15 if match[2].upper()=="K" else 0)
    limit=detailed_balance(val(gap),temperature+273.15) if gap and val(r) is not None and one_sun(r) else None
    if not limit:
        return finding("SHOCKLEY_QUEISSER",r["id"],[r,gap],"not_applicable",
                       "Нужны однозначный Eg, нормализованный КПД или Jsc и подтверждённое освещение AM1.5G 100 мВт/см².")
    quantity="pce_percent" if r["metric_name"]=="PCE" else "jsc_mA_cm2"
    ceiling=limit[quantity]
    violation=abs(val(r))>ceiling
    return finding("SHOCKLEY_QUEISSER",r["id"],[r,gap],"warning" if violation else "info",
                   f"Предел детального баланса для идеального однопереходного прибора, {REFERENCE}. Температура модели {temperature:g} °C"
                   +(" (опубликованная)." if raw else " (эталонный ориентир, температура измерения неизвестна)."),
                   threshold={"max":ceiling,"unit":r["normalized_unit"],"model_temperature_C":temperature},
                   sources=[r["intensity_source_id"]])


def eqe(r, group):
    jsc=unique(group,"Jsc",r["aggregation"])
    eligible=jsc and r["measurement_id"] and r["device_id"] and r["scan_direction"] and val(jsc)!=0 and val(r) is not None
    if eligible and r["acquisition_method"]=="calculated":
        eligible=one_sun(jsc) and (r["calculation_inputs"] or {}).get("coverage_confirmed") is True
    if eligible and not (r["condition_label"] or (r["spectrum"] and r["intensity_source_id"])):
        eligible=False
    threshold={"relative":EQE_TOL,"denominator":"abs(Jsc_JV)"}
    if not eligible:
        return finding("EQE_JSC",r["id"],[r],"not_applicable",
                       "Не найдена единственная пара Jsc_EQE и Jsc с подтверждённым прибором, ходом, агрегацией и условиями. Для расчёта из графика также нужны AM1.5G 100 мВт/см² и подтверждение спектрального покрытия.",threshold=threshold)
    difference=abs(abs(val(r))-abs(val(jsc)))/abs(val(jsc))
    return finding("EQE_JSC",r["id"],[r,jsc],"warning" if difference>EQE_TOL else "info",
                   "Сопоставлены модули интегрального Jsc по EQE и Jsc по ВАХ.",
                   [observed(r),observed(jsc),item("Расхождение",100*difference,"%")],threshold,[jsc["intensity_source_id"]],
                   "Проверьте калибровку спектра или соответствие образцов." if difference>EQE_TOL else None)


def checks(connection, publication, corpus_status):
    if corpus_status.startswith("excluded"):
        return
    rs=rows(connection,"""SELECT r.*,m.scan_direction,m.light_intensity,m.light_intensity_unit,m.intensity_source_id,
        m.spectrum,m.temperature,m.batch_label,m.condition_label,m.stability_protocol,m.stress_conditions
        FROM measurement_result r LEFT JOIN measurement m ON m.id=r.measurement_id
        WHERE r.publication_id=? AND r.result_origin='primary'""",publication)
    references={}
    for ref in rows(connection,"""SELECT l.result_id,s.source_page,s.bbox,s.source_type,s.source_table
        FROM result_source l JOIN source_reference s ON s.id=l.source_reference_id
        WHERE s.publication_id=? AND s.fragment_role='value' ORDER BY s.id""",publication):
        references.setdefault(ref["result_id"],ref)
    verified={r["id"] for r in rs if r["extraction_status"]=="Подтверждено пользователем" and r["measurement_id"]}
    measurement_groups=defaultdict(list)
    eg_by_material=defaultdict(list)
    for r in rs:
        measurement_groups[r["pdf_version_id"],r["measurement_id"]].append(r)
        if r["metric_name"]=="Eg":
            eg_by_material[r["material_id"]].append(r)
    for r in rs:
        metric=r["metric_name"]
        gap=gap_for(r,eg_by_material)
        group=measurement_groups[r["pdf_version_id"],r["measurement_id"]] if r["measurement_id"] else []
        yield from physical(r,gap)
        if metric=="PCE":
            yield pce(r,group,references,verified)
        if metric in ("PCE","Jsc"):
            yield sq(r,gap)
        if metric=="Voc":
            yield voc_gap(r,gap)
        if metric=="Jsc_EQE":
            yield eqe(r,group)
        published_scan=r["raw_record"].get("scan_direction")
        if published_scan or r["scan_direction"]:
            ok=published_scan==r["scan_direction"]
            status="info" if ok else "warning" if published_scan and r["scan_direction"] else "not_applicable"
            yield finding("SCAN_ISOLATION",r["id"],[r],status,
                          "Согласованность указанного и связанного направления развёртки.",
                          [item("Ход в статье",published_scan),item("Ход измерения",r["scan_direction"])],
                          hypothesis="Проверьте выбор хода в исходной таблице." if status=="warning" else None)
    mean_champion=[r for r in rs if r["aggregation"] in ("champion","mean") and val(r) is not None]
    for champion in (r for r in mean_champion if r["aggregation"]=="champion"):
        matches=[m for m in mean_champion if m["aggregation"]=="mean" and champion["metric_name"]==m["metric_name"]
                 and champion["normalized_unit"]==m["normalized_unit"] and champion["pdf_version_id"]==m["pdf_version_id"]
                 and champion["material_id"] and champion["material_id"]==m["material_id"]
                 and measurement_match(champion,m,("batch_label","condition_label","scan_direction"))]
        if len(matches)!=1:
            yield finding("BATCH_CHAMPION",champion["id"],[champion],"not_applicable","Нет единственного среднего с явной принадлежностью к той же партии и тем же условиям.")
            continue
        mean=matches[0]
        spread=normalize(mean["metric_name"],mean["spread_value"],mean["raw_unit"])[0]
        diff=val(champion)-val(mean)
        exceeds=diff<0 or spread is not None and diff>spread
        yield finding("BATCH_CHAMPION",champion["id"],[champion,mean],"warning" if exceeds else "info",
                      "Сравнены лучший результат и среднее по одной партии.",
                      [observed(champion),observed(mean),item("Опубликованный разброс",spread,mean["normalized_unit"])],
                      {"min_delta":0,"max_delta":spread,"unit":mean["normalized_unit"]},
                      hypothesis="Уточните, описывает ли разброс стандартное отклонение или предельный интервал." if exceeds else None)
    yield from cross_sources(connection,rs)


def cross_sources(connection,records):
    value_sources=defaultdict(set)
    for r in rows(connection,"""SELECT rs.result_id,s.source_type FROM result_source rs
        JOIN source_reference s ON s.id=rs.source_reference_id WHERE s.fragment_role='value' OR s.source_type='figure'"""):
        value_sources[r["result_id"]].add(r["source_type"])
    groups=defaultdict(list)
    for r in records:
        if r["measurement_id"] and r["device_id"] and r["scan_direction"] and r["aggregation"]!="unknown" and val(r) is not None:
            groups[(r["pdf_version_id"],r["measurement_id"],r["device_id"],r["aggregation"],r["metric_name"],r["normalized_unit"])].append(r)
    for group in groups.values():
        for a,b in combinations(group,2):
            if not value_sources[a["id"]] or not value_sources[b["id"]] or value_sources[a["id"]]==value_sources[b["id"]]:
                continue
            ids=sorted((a["id"],b["id"]))
            ua=(a.get("calculation_inputs") or {}).get("uncertainty")
            ub=(b.get("calculation_inputs") or {}).get("uncertainty")
            from_plot=any(r["acquisition_method"] in ("digitized_plot","calculated") for r in (a,b))
            if from_plot and any(r["acquisition_method"] in ("digitized_plot","calculated") and u is None for r,u in ((a,ua),(b,ub))):
                yield finding("CROSS_SOURCE",":".join(ids),[a,b],"not_applicable","Для сравнения с графиком требуется оценка погрешности оцифровки.")
                continue
            if a["metric_name"]=="Jsc_EQE" and from_plot:
                coverage=(a.get("calculation_inputs") or {}).get("coverage") or (b.get("calculation_inputs") or {}).get("coverage")
                if coverage=="observed_interval" and not a["condition_label"]:
                    yield finding("CROSS_SOURCE",":".join(ids),[a,b],"not_applicable","Не подтверждён одинаковый спектральный диапазон интегрирования.")
                    continue
            tolerance=precision(a)+precision(b)+(ua or 0)+(ub or 0)
            delta=abs(val(a)-val(b))
            yield finding("CROSS_SOURCE",":".join(ids),[a,b],"warning" if delta>tolerance+1e-12 else "info",
                          "Сопоставлены источники для одного прибора, измерения, агрегации и хода.",
                          [observed(a),observed(b),item("Абсолютное расхождение",delta,a["normalized_unit"])],
                          {"absolute":tolerance,"unit":a["normalized_unit"]})


def comparison(a,b,mode):
    criteria={}
    def same(field):
        criteria[field]=bool(a.get(field) is not None and a.get(field)==b.get(field))
    for field in ("material_class","device_type","metric_name","normalized_unit","area_definition"):
        same(field)
    criteria["normalized_value"]=val(a) is not None and val(b) is not None
    criteria["AM1.5G 100 mW/cm²"]=one_sun(a) and one_sun(b)
    areas=[r["active_area"] for r in (a,b)]
    criteria["active_area"]=bool(all(x is not None and x>0 for x in areas) and a["area_unit"]==b["area_unit"]=="cm²"
                                 and abs(areas[0]-areas[1])/max(areas)<=AREA_TOL)
    for field in ("aggregation","scan_direction"):
        criteria[field]=a.get(field) not in (None,"unknown") and a.get(field)==b.get(field)
    criteria["primary"]=all(r["result_origin"]=="primary" for r in (a,b))
    criteria["corpus"]=all(r["corpus_status"]=="included" for r in (a,b))
    if mode=="certified":
        criteria["certified"]=a["certified"]==b["certified"]==1
    elif mode=="stability":
        for field in ("stability_protocol","stress_conditions"):
            same(field)
        criteria["T80"]=a["metric_name"]==b["metric_name"]=="T80"
    else:
        criteria["ordinary"]=not any(r["certified"] or r["metric_name"]=="T80" for r in (a,b))
    return {"allowed":all(criteria.values()),"criteria":criteria,"area_tolerance":AREA_TOL}
