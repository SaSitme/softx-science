"""Exercise the cloud extraction transports and evidence without real keys."""
import io
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
from urllib.error import HTTPError

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from softx import app as server, store
from softx.extract import prose_metrics, shared_percentage_values, table_headers
from softx.local_ai import candidates
from softx.pdf import Line, Span


def article(photovoltaic, extra=()):
    writer=PdfWriter()
    page=writer.add_blank_page(width=620,height=790)
    page[NameObject("/Resources")]=DictionaryObject({
        NameObject("/Font"):DictionaryObject({
            NameObject("/F1"):DictionaryObject({
                NameObject("/Type"):NameObject("/Font"),
                NameObject("/Subtype"):NameObject("/Type1"),
                NameObject("/BaseFont"):NameObject("/Helvetica"),
            })
        })
    })
    if photovoltaic:
        lines=("An experimental single-junction CdTe solar cell was fabricated.",
               "Voc was 800 mV.",
               "The sweep yielded 83.2%, which represents the filling factor.")
    else:
        lines=("An AC/DC power supply was tested.",
               "The power conversion efficiency was 92.6%.")
    stream=DecodedStreamObject()
    stream.set_data("".join(f"BT /F1 11 Tf 40 {700-20*i} Td ({line}) Tj ET\n"
                            for i,line in enumerate((*lines,*extra))).encode())
    page[NameObject("/Contents")]=writer._add_object(stream)
    output=io.BytesIO()
    writer.write(output)
    return output.getvalue()


class OpenAIExtraction(TestCase):
    def setUp(self):
        directory=self.enterContext(TemporaryDirectory(prefix="softx-openai-test-"))
        data=Path(directory)
        self.addCleanup(store.workspace.reset,store.workspace.set(data))
        self.enterContext(mock.patch("softx.app.worker.submit"))
        self.enterContext(mock.patch.dict("os.environ",{"SOFTX_OPENAI_MODEL":"gpt-5-mini","OPENAI_API_KEY":"sk-test-only",
                                                         "SOFTX_DEEPSEEK_MODEL":"","SOFTX_LOCAL_ONLY":"0","SOFTX_OCR":"0"}))
        self.opener=self.enterContext(mock.patch("softx.local_ai.build_opener")).return_value
        store.initialize()

    def process(self,data,name):
        version=server.ingest(data,name,allow_cloud=True)["id"]
        server.process(version)
        return server.app.test_client().get(f"/api/pdf/{version}").json

    def test_openai_adds_evidence_once(self):
        def respond(request,timeout):
            self.assertEqual(request.full_url,"https://api.openai.com/v1/responses")
            self.assertEqual(request.get_header("Authorization"),"Bearer sk-test-only")
            body=json.loads(request.data)
            self.assertFalse(body["store"])
            self.assertIn("[83.2%]",body["input"])
            self.assertNotIn("800 mV]",body["input"])
            self.assertTrue(body["text"]["format"]["strict"])
            self.assertEqual(body["text"]["format"]["schema"]["properties"]["0"]["enum"],[None,"FF"])
            return io.BytesIO(json.dumps({"status":"completed","output":[
                {"type":"reasoning"},
                {"type":"message","content":[{"type":"output_text","text":'{"0":"FF","invented":"PCE"}'}]},
            ]}).encode())
        self.opener.open.side_effect=respond
        detail=self.process(article(True),"cell.pdf")
        self.assertEqual(detail["pdf"]["processing_status"],"completed")
        self.assertEqual(len(detail["results"]),2)
        result=next(item for item in detail["results"] if item["extraction_method"]=="openai_pdf_quote")
        self.assertEqual((result["metric_name"],result["raw_value"],result["raw_unit"]),("FF","83.2","%"))
        self.assertEqual(result["extraction_status"],"Требует проверки")
        refs={link["source_reference_id"] for link in detail["links"] if link["result_id"]==result["id"]}
        self.assertEqual({s["source_fragment"] for s in detail["sources"] if s["id"] in refs},{"83.2","%","filling factor"})
        version=detail["pdf"]["id"]
        with store.db() as connection:
            connection.execute("UPDATE pdf_version SET processing_status='queued' WHERE id=?",(version,))
        server.process(version)
        self.assertEqual(self.opener.open.call_count,1)
        self.assertEqual(len(server.app.test_client().get(f"/api/pdf/{version}").json["results"]),2)

    def test_off_topic_pdf_never_reaches_openai(self):
        detail=self.process(article(False),"power-supply.pdf")
        self.assertEqual(detail["pdf"]["processing_status"],"completed")
        self.assertEqual(detail["results"],[])
        self.opener.open.assert_not_called()

    def test_uploaded_pdf_denies_cloud_by_default(self):
        version=server.ingest(article(True),"without-consent.pdf")["id"]
        server.process(version)
        self.opener.open.assert_not_called()

    def test_unambiguous_local_model_result_is_automatic_after_reprocessing(self):
        with (mock.patch.dict("os.environ",{"SOFTX_LOCAL_AI_PORT":"18090","SOFTX_LOCAL_ONLY":"1"}),
              mock.patch("softx.local_ai.local_response",return_value={"0":"FF"}) as local):
            detail=self.process(article(True),"local-verified.pdf")
            result=next(r for r in detail["results"] if r["extraction_method"]=="local_ai_pdf_quote")
            self.assertEqual(result["extraction_status"],"Извлечено автоматически")
            version=detail["pdf"]["id"]
            with store.db() as connection:
                connection.execute("UPDATE measurement_result SET extraction_status='Требует проверки' WHERE id=?",
                                   (result["id"],))
                connection.execute("UPDATE pdf_version SET processing_status='queued' WHERE id=?",(version,))
            server.process(version)
            after=server.app.test_client().get(f"/api/pdf/{version}").json
            self.assertEqual(next(r for r in after["results"] if r["id"]==result["id"])["extraction_status"],
                             "Извлечено автоматически")
        local.assert_called_once()

    def test_automatic_deepseek_upload_and_retry_need_no_form_flag(self):
        client=server.app.test_client()
        old=server.ingest(article(False),"before-automatic.pdf")["id"]
        server.process(old)
        with (mock.patch.dict("os.environ",{"SOFTX_DEEPSEEK_AUTOMATIC":"1","SOFTX_LOCAL_ONLY":"0",
                                            "SOFTX_DEEPSEEK_MODEL":"deepseek-flash","DEEPSEEK_API_KEY":"sk-test-only"}),
              mock.patch("softx.local_ai.deepseek_response",return_value={"0":"FF"}) as deepseek):
            self.assertTrue(client.get("/api/library").json["cloud_automatic"])
            upload=client.post("/api/upload",data={"files":(io.BytesIO(article(True)),"automatic.pdf")})
            self.assertEqual(upload.status_code,202)
            version=upload.json[0]["id"]
            self.assertEqual(upload.json[0]["allow_cloud"],1)
            server.process(version)
            self.assertEqual(client.post(f"/api/pdf/{old}/retry").status_code,202)
            self.assertEqual(client.get(f"/api/pdf/{old}").json["pdf"]["allow_cloud"],1)
        deepseek.assert_called_once()

    def test_cloud_fills_gaps_after_local_provider(self):
        with (mock.patch.dict("os.environ",{"SOFTX_LOCAL_AI_PORT":"18090","SOFTX_DEEPSEEK_MODEL":"deepseek-flash",
                                            "DEEPSEEK_API_KEY":"sk-test-only"}),
              mock.patch("softx.local_ai.local_response",return_value={"0":None}) as local,
              mock.patch("softx.local_ai.deepseek_response",return_value={"0":"FF"}) as remote):
            detail=self.process(article(True),"both-providers.pdf")
        local.assert_called_once()
        remote.assert_called_once()
        self.assertEqual([r["normalized_value"] for r in detail["results"]
                          if r["extraction_method"]=="deepseek_pdf_quote"],[83.2])

    def test_printed_shared_percentage_does_not_assign_a_delta(self):
        values=shared_percentage_values("The mean PCEs of pristine and modified devices are 14.66 and 12.96%, respectively.")
        self.assertEqual([(metric,value["n"],unit.group()) for metric,value,unit in values],
                         [("PCE","14.66","%"),("PCE","12.96","%")])
        self.assertEqual(list(shared_percentage_values("PCE improvements were 10 and 14%.")),[])

    def test_voltage_deficit_and_eqe_are_distinct_from_jv_metrics(self):
        cells=[Span(text,[[start+i,10,start+i+1,20] for i in range(len(text))])
               for start,text in ((0,"Voc deficit (V)"),(60,"PCE (%)"))]
        self.assertEqual([entry[2] for entry in table_headers(Line(1,cells)).values()],["PCE"])
        text="The integrated Jsc of 22.34 mA/cm2 was measured."
        self.assertEqual([metric for metric,*_ in prose_metrics(text,"External quantum efficiency (EQE):")],["Jsc_EQE"])
        self.assertEqual([metric for metric,*_ in prose_metrics(text)],["Jsc"])

    def test_reprocessing_refines_a_previously_read_metric(self):
        data = article(True, ("The stabilized PCE of 14.8% was measured.",))
        legacy = re.compile(r"\b(?:(?P<PCE>PCE)|(?P<Voc>Voc))\b",re.I)
        with mock.patch("softx.extract.METRIC",legacy):
            version = server.ingest(data,"deprecated-metric.pdf")["id"]
            server.process(version)
        client = server.app.test_client()
        old = next(r for r in client.get(f"/api/pdf/{version}").json["results"] if r["metric_name"]=="PCE")
        client.post(f"/api/pdf/{version}/retry")
        server.process(version)
        after = [r for r in client.get(f"/api/pdf/{version}").json["results"] if r["id"]==old["id"]]
        self.assertEqual([(r["metric_name"],r["raw_value"],r["raw_unit"]) for r in after],
                         [("stabilized_PCE","14.8","%")])

    def test_mpp_context_supplies_the_efficiency_metric(self):
        texts = (
            "The current density at the MPP was 18.0 mA/cm2, corresponding to an efficiency of 14.8%.",
            "The steady-state output efficiency at the maximum power point is 14.8%.",
        )
        for sentence in texts:
            self.assertEqual([(metric,value["n"]) for metric,value,_ in prose_metrics(sentence)],
                             [("stabilized_PCE","14.8")])
        self.assertEqual(list(prose_metrics("The current at the MPP was recorded. Evaluation indicates an efficiency of 14.8%.")),[])

    def test_rate_limit_keeps_local_results(self):
        self.opener.open.side_effect=HTTPError("https://api.openai.com/v1/responses",429,"test",None,None)
        detail=self.process(article(True),"cell.pdf")
        self.assertEqual(detail["pdf"]["processing_status"],"completed")
        self.assertEqual([r["metric_name"] for r in detail["results"]],["Voc"])

    def test_invalid_key_does_not_leave_softx(self):
        with mock.patch.dict("os.environ",{"OPENAI_API_KEY":"unexpected-input","SOFTX_LOCAL_AI_PORT":""}):
            detail=self.process(article(True),"cell.pdf")
        self.opener.open.assert_not_called()
        self.assertEqual([r["metric_name"] for r in detail["results"]],["Voc"])

    def test_local_only_keeps_text_requests_on_loopback(self):
        def respond(request,timeout):
            self.assertEqual(request.full_url,"http://127.0.0.1:18090/v1/chat/completions")
            self.assertIsNone(request.get_header("Authorization"))
            return io.BytesIO(b'{"choices":[{"message":{"content":"{\\"0\\":\\"FF\\"}"}}]}')
        self.opener.open.side_effect=respond
        with mock.patch.dict("os.environ",{"SOFTX_LOCAL_ONLY":"1","SOFTX_LOCAL_AI_PORT":"18090",
                                             "SOFTX_DEEPSEEK_MODEL":"deepseek-flash","DEEPSEEK_API_KEY":"sk-test-only"}):
            detail=self.process(article(True),"cell.pdf")
        self.opener.open.assert_called_once()
        self.assertTrue(any(r["extraction_method"]=="local_ai_pdf_quote" for r in detail["results"]))

    def test_shared_percentage_is_located_separately(self):
        text="The PCE values were 9.6 and 11.4%."
        line=Line(1,[Span(text,[[i,10,i+1,20] for i in range(len(text))])])
        first,second=candidates([line])
        self.assertIn("[9.6] and 11.4%",first[0])
        self.assertIn("[11.4%]",second[0])
        self.assertEqual([candidate[4].group() for candidate in (first,second)],["%","%"])

    def test_model_candidates_need_an_explicit_measurement_relationship(self):
        def options(text):
            line=Line(1,[Span(text,[[i,10,i+1,20] for i in range(len(text))])])
            return [item[1] for item in candidates([line])]
        self.assertEqual(options("The sweep yielded 83.2%, which represents the filling factor."),[["FF"]])
        self.assertEqual(options("The PCE improved by 12% after annealing."),[])
        self.assertEqual(options("Figure 3. The PCE axis spans 5 and 25%."),[])
        self.assertEqual(options("The PCE retained 90% of its initial value."),[])

    def test_deepseek_uses_its_own_key_and_checks_metric_codes(self):
        def respond(request,timeout):
            self.assertEqual(request.full_url,"https://api.deepseek.com/chat/completions")
            self.assertEqual(request.get_header("Authorization"),"Bearer sk-deepseek-test")
            body=json.loads(request.data)
            self.assertEqual(body["model"],"deepseek-flash")
            self.assertEqual(body["response_format"],{"type":"json_object"})
            self.assertEqual(body["thinking"],{"type":"disabled"})
            self.assertIn("[83.2%]",body["messages"][1]["content"])
            self.assertNotIn("800 mV]",body["messages"][1]["content"])
            return io.BytesIO(json.dumps({"choices":[{"finish_reason":"stop",
                "message":{"content":'{"0":"FF","1":"PCE"}'}}]}).encode())
        self.opener.open.side_effect=respond
        with mock.patch.dict("os.environ",{"SOFTX_DEEPSEEK_MODEL":"deepseek-flash","DEEPSEEK_API_KEY":"sk-deepseek-test"}):
            detail=self.process(article(True),"cell.pdf")
        self.assertEqual(detail["pdf"]["processing_status"],"completed")
        results=[r for r in detail["results"] if r["extraction_method"]=="deepseek_pdf_quote"]
        self.assertEqual(len(results),1)
        self.assertEqual((results[0]["metric_name"],results[0]["raw_value"],results[0]["raw_unit"]),("FF","83.2","%"))
        self.assertEqual(results[0]["extraction_status"],"Требует проверки")

    def test_deepseek_rejects_a_metric_outside_the_candidate_options(self):
        self.opener.open.return_value=io.BytesIO(json.dumps({"choices":[{"finish_reason":"stop",
            "message":{"content":'{"0":"PCE"}'}}]}).encode())
        with mock.patch.dict("os.environ",{"SOFTX_DEEPSEEK_MODEL":"deepseek-flash","DEEPSEEK_API_KEY":"sk-deepseek-test"}):
            detail=self.process(article(True),"cell.pdf")
        self.assertEqual(detail["pdf"]["processing_status"],"completed")
        self.assertEqual([r["metric_name"] for r in detail["results"]],["Voc"])
