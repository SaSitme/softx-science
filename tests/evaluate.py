"""Fixed acceptance metrics for the declared synthetic ground truth.

Run with a fresh extraction snapshot, before manual review. Match metrics
one-to-one on file, metric name, origin, aggregation, numeric value and
normalized unit. Numeric equality is abs_tol=1e-8, rel_tol=0. A PDF anchor is
correct on the annotated page if IoU >= .50 or the actual box center falls
inside the independently annotated target region. JV pairs are identified in
PDF coordinate space with no point reuse; an error pair must lie within 2%
of the plotted diagonal. Both physical axes use their independent own units.
"""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE={"PCE","Voc","Jsc","FF"}
RESULT_EXTENDED={"Eg","Rs","Rsh","HI","Jsc_EQE","T80"}
ENTITY_FIELDS={
    "material":("material_name_raw","material_class","formula_normalized","dimensionality","additives","bandgap_value","bandgap_unit","bandgap_method"),
    "device":("device_role","device_type","architecture","substrate_type","active_area","area_unit","area_definition","encapsulation"),
    "device_layer":("layer_order","layer_function","layer_material","layer_thickness","thickness_unit","deposition_method",
                    "annealing_temperature","annealing_time","atmosphere"),
    "measurement":("measurement_type","spectrum","light_intensity","light_intensity_unit","scan_direction","scan_rate",
                   "mask_used","certified","temperature","stability_protocol","stress_conditions")
}


def ratio(numerator,denominator):
    return dict(numerator=numerator,denominator=denominator,value=numerator/denominator if denominator else None)


def anchor(actual,expected):
    if actual["source_page"]!=expected["page"]:
        return False
    a=actual["bbox"]
    b=expected["bbox"]
    overlap=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    area=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-overlap
    return (area>0 and overlap/area>=.5) or (b[0]<=(a[0]+a[2])/2<=b[2] and b[1]<=(a[1]+a[3])/2<=b[3])


def same(actual,expected):
    if type(actual) in (int,float) and type(expected) in (int,float):
        return math.isclose(actual,expected,rel_tol=0,abs_tol=1e-8)
    if isinstance(actual,str) and isinstance(expected,str):
        return " ".join(actual.split()).casefold()==" ".join(expected.split()).casefold()
    return actual==expected


def evaluate(truth,data):
    versions={v["id"]:v["filename"] for v in data["pdf_version"]}
    pubs={p["id"]:p for p in data["publication"]}
    name_to_pub={v["filename"]:v["publication_id"] for v in data["pdf_version"]}
    extraction_files={record["file"] for record in truth["expected_metrics"]}
    predicted=[r for r in data["measurement_result"] if versions[r["pdf_version_id"]] in extraction_files
               and r["acquisition_method"]=="published_numeric"]
    devices={d["id"]:d for d in data["device"]}
    measures={m["id"]:m for m in data["measurement"]}
    sources={s["id"]:s for s in data["source_reference"]}
    refs=defaultdict(list)
    for link in data["result_source"]:
        source=sources[link["source_reference_id"]]
        if source["fragment_role"]=="value":
            refs[link["result_id"]].append(source)

    def match_metrics(expected,metrics):
        actual=[r for r in predicted if r["metric_name"] in metrics]
        unmatched={r["id"]:r for r in actual}
        matches=[]
        for target in expected:
            candidates=[r for r in unmatched.values() if versions[r["pdf_version_id"]]==target["file"]
                        and r["metric_name"]==target["metric"] and r["aggregation"]==target["aggregation"]
                        and r["result_origin"]==target["origin"] and r["normalized_unit"]==target["unit"]
                        and same(r["normalized_value"],target["value"])]
            if candidates:
                best=max(candidates,key=lambda r:any(anchor(s,target) for s in refs[r["id"]]))
                matches.append((best,target))
                del unmatched[best["id"]]
        return matches, len(actual)

    def pair_stats(pairs,nactual,nexpected):
        return {"precision":ratio(len(pairs),nactual),"recall":ratio(len(pairs),nexpected)}

    expected=truth["expected_metrics"]
    base,base_num=match_metrics(expected,BASE)
    ext,ext_num=match_metrics(truth["extended"],RESULT_EXTENDED)
    matches=base+ext
    by_metric={}
    for metric in sorted(BASE|RESULT_EXTENDED):
        relevant=[r for r in expected+truth["extended"] if r["metric"]==metric]
        matched,pred=match_metrics(relevant,{metric})
        by_metric[metric]=pair_stats(matched,pred,len(relevant))
    linked=[(r,target) for r,target in matches if target["device"] and target["scan"]]
    correct_device=sum(devices.get(r["device_id"],{}).get("device_label")==target["device"] for r,target in linked)
    correct_measurement=sum(devices.get(r["device_id"],{}).get("device_label")==target["device"] and
                            measures.get(r["measurement_id"],{}).get("scan_direction")==target["scan"] for r,target in linked)
    hits=sum(any(anchor(s,target) for s in refs[r["id"]]) for r,target in matches)
    status_correct=sum(pubs.get(name_to_pub.get(file),{}).get("corpus_status")==v for file,v in truth["classifications"].items())

    entity_by_table={}
    extraction_pubs={name_to_pub.get(file):file for file in extraction_files}
    def entity_key(table,row):
        if table=="material":
            return row["publication_id"],row["material_class"]
        if table=="device":
            return row["publication_id"],row["device_label"]
        device=devices[row["device_id"]]
        suffix=row["layer_order"] if table=="device_layer" else row["scan_direction"]
        return device["publication_id"],f"{device['device_label']}/{suffix}"

    annotated_fields=Counter()
    for table,field_list in ENTITY_FIELDS.items():
        predictions=[]
        expected_values=[]
        for row in data[table]:
            pub,label=entity_key(table,row)
            if pub in extraction_pubs:
                predictions.extend((extraction_pubs[pub],label,field,row[field]) for field in field_list if row.get(field) is not None)
        for row in truth["entity_fields"][table]:
            for field in field_list:
                if field in row:
                    expected_values.append((row["file"],row["key"],field,row[field]))
                    annotated_fields[field]+=1
        matched=0
        unused=predictions[:]
        for value in expected_values:
            index=next((i for i,row in enumerate(unused) if row[:3]==value[:3] and same(row[3],value[3])),None)
            if index is not None:
                matched+=1
                unused.pop(index)
        entity_by_table[table]={"precision":ratio(matched,len(predictions)),"recall":ratio(matched,len(expected_values))}

    gold=truth["JV"]
    curve=next((plot for plot in data["plot_dataset"] if versions[plot["pdf_version_id"]]==gold["file"]
                and plot["source_page"]==gold["page"] and plot["source_figure"]==gold["figure"]
                and plot["panel_id"]==gold["panel"] and plot["plot_type"]=="JV"),None)
    target=gold["points"]
    a,b,c,d=gold["bbox"]
    xmin,xmax=min(x for x,y in target),max(x for x,y in target)
    ymin,ymax=min(y for x,y in target),max(y for x,y in target)
    tolerance=.02*math.hypot(c-a,d-b)
    points=[p for p in curve["points"] if p["x"] is not None and p["y"] is not None] if curve else []
    errors={"x":[],"y":[]}
    if curve and (curve["axes"]["x"]["unit"],curve["axes"]["y"]["unit"])==(gold["x_unit"],gold["y_unit"]):
        for x,y in target:
            px=a+(x-xmin)/(xmax-xmin)*(c-a)
            py=b+(y-ymin)/(ymax-ymin)*(d-b)
            closest=min(points,key=lambda p:math.hypot(p["px"]-px,p["py"]-py),default=None)
            if closest and math.hypot(closest["px"]-px,closest["py"]-py)<=tolerance:
                points.remove(closest)
                errors["x"].append(closest["x"]-x)
                errors["y"].append(closest["y"]-y)
    jv={}
    for axis,delta in errors.items():
        mae=ratio(sum(abs(x) for x in delta),len(delta))
        rmse=ratio(sum(x*x for x in delta),len(delta))
        if rmse["value"] is not None:
            rmse["value"]=math.sqrt(rmse["value"])
        jv[axis]={"unit":gold[axis+"_unit"],"mae":mae,"rmse":rmse}
    return {
        "sample":{"synthetic":True,"classification_files":truth["classifications"],"extraction_files":sorted(extraction_files),
                  "curve_file":gold["file"],"curve_expected_points":len(target),"damaged_files_excluded_from_classification":1},
        "base":pair_stats(base,base_num,len(expected)),
        "extended_result_metrics":pair_stats(ext,ext_num,len(truth["extended"])),
        "all_extended_fields":{
            kind:ratio(len(ext)+sum(v[kind]["numerator"] for v in entity_by_table.values()),
                       (ext_num if kind=="precision" else len(truth["extended"]))+sum(v[kind]["denominator"] for v in entity_by_table.values()))
            for kind in ("precision","recall")},
        "by_metric":by_metric,"expanded_fields_by_entity":entity_by_table,"annotated_expanded_field_counts":dict(annotated_fields),
        "corpus_accuracy":ratio(status_correct,len(truth["classifications"])),
        "device_link_accuracy":ratio(correct_device,len(linked)),
        "measurement_link_accuracy":ratio(correct_measurement,len(linked)),
        "pdf_fragment_accuracy":ratio(hits,len(matches)),
        "needs_review_rate":ratio(sum(r["extraction_status"]=="Требует проверки" for r in predicted),len(predicted)),
        "digitization_jv":{"points":ratio(len(errors["x"]),len(target)),**jv},
    }


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--dataset",type=Path,default=ROOT/"export"/"dataset.json")
    parser.add_argument("--truth",type=Path,default=ROOT/"fixtures"/"ground_truth.json")
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    report=evaluate(json.loads(args.truth.read_text()),json.loads(args.dataset.read_text()))
    rendered=json.dumps(report,ensure_ascii=False,indent=2)+"\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered,end="")


if __name__=="__main__":
    main()
