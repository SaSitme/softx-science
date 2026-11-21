"""Rule execution and issue persistence independent of the scientific schema."""
from dataclasses import dataclass, field

from .store import edit, insert, key, one, rows


@dataclass
class Finding:
    code: str
    anchor: str
    severity: str
    description: str
    records: list = field(default_factory=list)
    values: list = field(default_factory=list)
    threshold: dict | None = None
    sources: list = field(default_factory=list)
    hypothesis: str | None = None

    @property
    def state(self):
        return {"not_applicable":"not_applicable","info":"passed"}.get(self.severity,"open")


def run(connection, publication, findings):
    """A rerun updates matching checks and retains old findings that no longer apply."""
    seen=set()
    for finding in findings:
        ident=key(publication,finding.code,finding.anchor)
        seen.add(ident)
        previous=one(connection,"SELECT * FROM validation_issue WHERE id=?",ident)
        status=finding.state
        if previous and previous["resolution_status"] in ("open","resolved") and status=="passed":
            status="resolved"
        columns=dict(severity=finding.severity, compared_values=finding.values, threshold=finding.threshold,
                     description=finding.description,hypothesis=finding.hypothesis,resolution_status=status)
        if previous:
            edit(connection,"validation_issue",previous,columns,"softx-rules")
            if any(previous[k]!=value for k,value in columns.items()):
                connection.execute("UPDATE validation_issue SET updated_at=CURRENT_TIMESTAMP WHERE id=?",(ident,))
        else:
            insert(connection,"validation_issue",id=ident,publication_id=publication,rule_code=finding.code,**columns)
        result_ids={r["id"] for r in finding.records if r}
        source_ids={s for s in finding.sources if s}
        for result in result_ids:
            insert(connection,"issue_result",issue_id=ident,result_id=result)
            source_ids.update(r["source_reference_id"] for r in rows(connection,
                "SELECT source_reference_id FROM result_source WHERE result_id=?",result))
        obsolete_results=rows(connection,"SELECT result_id FROM issue_result WHERE issue_id=?",ident)
        for row in obsolete_results:
            if row["result_id"] not in result_ids:
                connection.execute("DELETE FROM issue_result WHERE issue_id=? AND result_id=?",(ident,row["result_id"]))
        for source in source_ids:
            insert(connection,"issue_source",issue_id=ident,source_reference_id=source)
        for row in rows(connection,"SELECT source_reference_id FROM issue_source WHERE issue_id=?",ident):
            if row["source_reference_id"] not in source_ids:
                connection.execute("DELETE FROM issue_source WHERE issue_id=? AND source_reference_id=?",(ident,row["source_reference_id"]))
    for previous in rows(connection,"SELECT * FROM validation_issue WHERE publication_id=? AND resolution_status IN ('open','passed','not_applicable')",publication):
        if previous["id"] not in seen:
            edit(connection,"validation_issue",previous,{"resolution_status":"superseded"},"softx-rules")
