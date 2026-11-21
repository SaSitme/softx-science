from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from werkzeug.exceptions import Conflict, NotFound

from softx import domain, store
from softx.publications import delete_publication


class DeletePublication(TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.addCleanup(store.workspace.reset,store.workspace.set(self.directory))
        store.initialize()

    def add_pdf(self, connection, publication, version, status="completed"):
        store.insert(connection,"pdf_version", id=version, sha256=version,
                     publication_id=publication, filename=f"{publication}.pdf", processing_status=status)
        directory = self.directory / version
        directory.mkdir()
        (directory / "original.pdf").write_bytes(b"fixture")

    def test_removes_all_versions_and_leaves_other_articles_and_citations(self):
        first, second, other = "1"*64, "2"*64, "3"*64
        with store.db() as connection:
            for ident in ("removed","other"):
                store.insert(connection,"publication",id=ident,title=ident)
            self.add_pdf(connection,"removed",first)
            self.add_pdf(connection,"removed",second,"queued")
            self.add_pdf(connection,"other",other)
            for ident,version,publication,extra in (
                ("main",first,"removed",{}),("duplicate",second,"removed",{"duplicate_of":"main"}),
                ("citation",other,"other",{"cited_publication_id":"removed","result_origin":"cited"}),
            ):
                store.insert(connection,"measurement_result",id=ident,pdf_version_id=version,
                             publication_id=publication,metric_name="PCE",metric_value=12,raw_record={},
                             value_state="numeric",extraction_status=domain.AUTO,**extra)
                store.insert(connection,"audit_log",entity_type="measurement_result",entity_id=ident,
                             field_name="created",author="test")
        delete_publication("removed")
        self.assertFalse((self.directory/first).exists())
        self.assertFalse((self.directory/second).exists())
        self.assertTrue((self.directory/other/"original.pdf").exists())
        with store.db() as connection:
            self.assertEqual(store.rows(connection,"SELECT id FROM pdf_version"),[{"id":other}])
            self.assertEqual(store.rows(connection,"SELECT id,cited_publication_id FROM measurement_result"),
                             [{"id":"citation","cited_publication_id":"removed"}])
            self.assertEqual(store.rows(connection,"SELECT entity_id FROM audit_log"),[{"entity_id":"citation"}])
            self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")),[])
        with self.assertRaises(NotFound):
            delete_publication("removed")

    def test_rejects_deletion_during_processing(self):
        version = "4"*64
        with store.db() as connection:
            store.insert(connection,"publication",id="active",title="Article")
            self.add_pdf(connection,"active",version,"processing")
        with self.assertRaises(Conflict):
            delete_publication("active")
        self.assertTrue((self.directory/version/"original.pdf").exists())
        with store.db() as connection:
            self.assertEqual(store.one(connection,"SELECT processing_status FROM pdf_version WHERE id=?",version),
                             {"processing_status":"processing"})
