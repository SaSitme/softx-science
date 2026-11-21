from flask import Blueprint, abort, jsonify, request

from . import rules
from .store import db, one, rows
from .validation import run

science=Blueprint("science",__name__)


def recheck(connection,pub):
    publication=one(connection,"SELECT corpus_status FROM publication WHERE id=?",pub)
    if publication:
        run(connection,pub,rules.checks(connection,pub,publication["corpus_status"]))


def issue_report(connection,pub=None):
    sql="""SELECT i.*, p.title AS publication_title FROM validation_issue i JOIN publication p ON p.id=i.publication_id"""
    issues=rows(connection,sql+(" WHERE i.publication_id=?" if pub else "")+
                " ORDER BY CASE i.severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 WHEN 'not_applicable' THEN 2 ELSE 3 END, i.rule_code, i.id",
                *([pub] if pub else []))
    result_links={}
    for row in rows(connection,"SELECT * FROM issue_result ORDER BY result_id"):
        result_links.setdefault(row["issue_id"],[]).append(row["result_id"])
    source_links={}
    for row in rows(connection,"""SELECT l.issue_id, s.* FROM issue_source l JOIN source_reference s
                                    ON s.id=l.source_reference_id ORDER BY s.source_page,s.fragment_role,s.id"""):
        iid=row.pop("issue_id")
        row["href"]=f"/#xray/{row['pdf_version_id']}/ref,{row['id']}"
        source_links.setdefault(iid,[]).append(row)
    for issue in issues:
        ident=issue["id"]
        issue["rule_name"]=rules.RULES[issue["rule_code"]]
        issue["result_ids"]=result_links.get(ident,[])
        issue["sources"]=source_links.get(ident,[])
    return issues


@science.get("/api/issues")
def get_issues():
    with db() as connection:
        return jsonify(issue_report(connection,request.args.get("publication_id")))


def candidates(connection, mode):
    clause={"ordinary":"r.metric_name!='T80' AND COALESCE(q.certified,0)<>1",
            "certified":"q.certified=1 AND r.metric_name!='T80'","stability":"r.metric_name='T80'"}.get(mode)
    if not clause:
        abort(400,"Неизвестный режим сопоставления")
    return rows(connection,f"""SELECT r.id,r.metric_name,r.normalized_value,r.normalized_unit,r.value_state,
        r.pdf_version_id,r.result_origin,r.aggregation,p.title,p.corpus_status,d.device_label,d.device_type,m.material_class,
        d.active_area,d.area_unit,d.area_definition,q.certified,q.scan_direction,q.spectrum,q.light_intensity,q.light_intensity_unit,
        q.intensity_source_id,q.stability_protocol,q.stress_conditions FROM measurement_result r
        JOIN publication p ON p.id=r.publication_id LEFT JOIN device d ON d.id=r.device_id
        LEFT JOIN material m ON m.id=COALESCE(d.material_id,r.material_id)
        LEFT JOIN measurement q ON q.id=r.measurement_id WHERE {clause}
        ORDER BY p.title,d.device_label,r.metric_name,r.id""")


@science.get("/api/compare")
def compare():
    mode=request.args.get("mode","ordinary")
    with db() as connection:
        records=candidates(connection,mode)
    left,right=request.args.get("a"),request.args.get("b")
    if not left or not right:
        return jsonify(records=records)
    by_id={r["id"]:r for r in records}
    if left==right or left not in by_id or right not in by_id:
        abort(400,"Выберите две разные записи из текущей вкладки")
    return jsonify(a=by_id[left],b=by_id[right],**rules.comparison(by_id[left],by_id[right],mode))
