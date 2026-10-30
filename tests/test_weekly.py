import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

MODULE = Path(__file__).parents[1] / "skills/feishu-weekly-meeting/scripts/weekly.py"
spec = importlib.util.spec_from_file_location("weekly", MODULE)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

WED = dt.date(2026, 10, 28)
FRI = dt.date(2026, 10, 30)


def config():
    return {"document_id": "testdoc", "document_origin": "https://example.feishu.cn",
            "timezone": "Asia/Shanghai", "meeting_query": "Example weekly",
            "meeting_anchor": "heading", "periods": {"2026-Q4": "table"},
            "projects": [{"name": "Project A", "owners": ["ou_exampleA"],
                          "fields": ["当前进展", "能力交付节点", "风险与依赖"]},
                         {"name": "Project B", "owners": ["ou_exampleB"],
                          "fields": ["当前进展", "风险与依赖"]}]}


class FakeLark:
    """Synthetic remote with native cell IDs and optimistic revision checks."""
    def __init__(self):
        self.config = config()
        self.revision = 10
        self.counter = 0
        self.writes = []
        self.table = ET.fromstring(w.new_table(self.config, WED))
        self.table.set("id", "table")
        self.cell_ids = []
        for cell in self.table.findall("./thead/tr/th") + self.table.findall("./tbody/tr/td"):
            self.cell_ids.append(self.new_id())
        self.assign_ids(self.table)
        self.fail_after_update = False
        self.conflict_on_update = False
        self.minutes_denied = False
        self.empty_inserted_cells = False
        self.candidates = [{"id": "1234567890123", "display_info": "Example weekly"}]
        self.meeting = {"meeting_id": "1234567890123", "start_time": "2026-10-28 10:00",
                        "end_time": "2026-10-28 11:00", "note_id": "note1",
                        "minute_token": "minute1"}

    def new_id(self):
        self.counter += 1
        return f"block{self.counter}"

    def assign_ids(self, node):
        for item in node.iter():
            if item.tag in ("p", "hr", "li", "h2") and not item.get("id"):
                item.set("id", self.new_id())

    def fetch(self, doc, *scope):
        if doc == "summarydoc":
            root = ET.fromstring("<document><p>Real synthetic summary</p></document>")
        else:
            root = ET.Element("fragment")
            root.append(copy.deepcopy(self.table))
        return {"revision_id": self.revision, "document_id": doc,
                "content": ET.tostring(root, encoding="unicode")}, root

    def api(self, method, path, params=None, body=None):
        if params["document_revision_id"] != self.revision:
            raise w.WorkflowError("revision conflict")
        if method == "GET":
            return {"block": {"table": {"property": {"column_size": 4},
                                       "cells": self.cell_ids[:]}}}
        if method == "POST":
            cell_id = path.split("/")[-2]
            index = self.cell_ids.index(cell_id)
            cells = self.table.findall("./thead/tr/th") + self.table.findall("./tbody/tr/td")
            ET.SubElement(cells[index], "p", {"id": self.new_id()}).text = (
                body["children"][0]["text"]["elements"][0]["text_run"]["content"])
            self.revision += 1
            self.writes.append(("create_paragraph", index))
            return {"document_revision_id": self.revision}
        assert method == "PATCH"
        assert params["client_token"]
        assert params["user_id_type"] == "open_id"
        index = body["requests"][0]["insert_table_row"]["row_index"]
        row = ET.Element("tr")
        for _ in range(4):
            cell = ET.SubElement(row, "td")
            if not self.empty_inserted_cells:
                ET.SubElement(cell, "p")
        self.assign_ids(row)
        self.table.find("tbody").insert(index - 1, row)
        self.cell_ids[index * 4:index * 4] = [self.new_id() for _ in range(4)]
        self.writes.append(("insert", copy.deepcopy(params)))
        self.revision += 1
        return {"document_revision_id": self.revision}

    def update(self, doc, revision, command, block, content):
        if self.conflict_on_update:
            self.revision += 1
            self.conflict_on_update = False
        if revision != self.revision:
            raise w.WorkflowError("revision conflict")
        assert command == "block_replace"
        found = [(parent, child) for parent in self.table.iter() for child in parent
                 if child.get("id") == block]
        assert len(found) == 1
        parent, child = found[0]
        index = list(parent).index(child)
        replacement = ET.fromstring("<fragment>" + content + "</fragment>")
        self.assign_ids(replacement)
        parent.remove(child)
        for offset, item in enumerate(replacement):
            parent.insert(index + offset, item)
        self.writes.append(("update", revision))
        self.revision += 1
        if self.fail_after_update:
            self.fail_after_update = False
            raise w.WorkflowError("response lost after successful update")
        return {"result": "success", "warnings": []}

    def call(self, args, content=None):
        if args[:2] == ["vc", "+search"]:
            return {"items": self.candidates, "has_more": False}
        if args[:2] == ["vc", "+detail"]:
            return {"meetings": [self.meeting]}
        if args[:2] == ["minutes", "+detail"]:
            if self.minutes_denied:
                raise w.WorkflowError("missing_scope: minutes:minutes.basic:read")
            return {"minutes": [{"minute_token": "minute1", "title": "Example weekly",
                                  "note_id": "note1"}]}
        if args[:2] == ["note", "+detail"]:
            return {"note": {"note_doc_token": "summarydoc"}}
        raise AssertionError(args)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        w.atomic_json(self.path, config())
        self.remote = FakeLark()
        self.flow = w.Workflow(self.path, self.remote)

    def test_week_dates_include_both_meetings(self):
        self.assertEqual(w.meeting_dates(FRI), [WED, FRI])
        self.assertEqual(w.meeting_dates(dt.date(2027, 1, 1)),
                         [dt.date(2026, 12, 30), dt.date(2027, 1, 1)])
        with self.assertRaises(w.WorkflowError):
            w.meeting_dates(dt.date(2026, 10, 29), single=True)

    def test_new_row_preserves_history_and_mentions(self):
        original = w.digest(self.remote.table.find("./tbody/tr"))
        result = self.flow.create(FRI)
        self.assertEqual(result["status"], "created")
        rows = self.remote.table.findall("./tbody/tr")
        self.assertEqual(w.xml_text(rows[0][0]), "10/30")
        self.assertEqual(w.digest(rows[1]), original)
        self.assertEqual([n.get("user-id") for n in rows[0][3].iter("cite")],
                         ["ou_exampleA", "ou_exampleB"])
        self.assertTrue(self.flow.operations.exists())
        self.assertFalse(self.flow.journal.exists())

    def test_empty_native_cells_are_initialized_before_fill(self):
        self.remote.empty_inserted_cells = True
        self.assertEqual(self.flow.create(FRI)["status"], "created")
        self.assertEqual(sum(x[0] == "create_paragraph" for x in self.remote.writes), 4)

    def test_repeated_create_does_not_write(self):
        self.flow.create(FRI)
        writes = len(self.remote.writes)
        self.assertEqual(self.flow.create(FRI)["status"], "exists")
        self.assertEqual(len(self.remote.writes), writes)

    def test_recover_response_loss_without_duplicate_row(self):
        self.remote.fail_after_update = True
        with self.assertRaisesRegex(w.WorkflowError, "response lost"):
            self.flow.create(FRI)
        self.assertTrue(self.flow.journal.exists())
        self.assertEqual(self.flow.create(FRI)["status"], "created")
        self.assertEqual(len(self.remote.table.findall("./tbody/tr")), 2)
        self.assertEqual(sum(x[0] == "insert" for x in self.remote.writes), 1)

    def test_revision_conflict_stops_remaining_writes(self):
        self.remote.conflict_on_update = True
        with self.assertRaisesRegex(w.WorkflowError, "revision conflict"):
            self.flow.create(FRI)
        self.assertEqual([x[0] for x in self.remote.writes], ["insert"])
        self.assertTrue(self.flow.journal.exists())

    def test_dry_run_has_no_remote_mutation_or_journal(self):
        self.flow.dry_run = True
        self.assertEqual(self.flow.create(FRI)["status"], "would_create")
        self.assertEqual(self.flow.sync(FRI)["status"], "create_row_first")
        self.assertEqual(self.remote.writes, [])
        self.assertFalse(self.flow.journal.exists())

    def test_sync_fills_correct_columns_and_preserves_conclusions(self):
        before = w.digest(self.remote.table.find("./tbody/tr")[3])
        self.assertEqual(self.flow.sync(WED)["status"], "synced")
        row = self.remote.table.find("./tbody/tr")
        self.assertEqual(row[1].find(".//cite").get("doc-id"), "summarydoc")
        self.assertEqual(w.xml_text(row[2]), "https://example.feishu.cn/minutes/minute1")
        self.assertEqual(w.digest(row[3]), before)
        writes = len(self.remote.writes)
        self.assertEqual(self.flow.sync(WED)["status"], "already_synced")
        self.assertEqual(len(self.remote.writes), writes)

    def test_manual_content_is_not_overwritten_even_in_other_column(self):
        self.remote.table.find("./tbody/tr")[2][0].text = "Manually curated recording"
        with self.assertRaisesRegex(w.WorkflowError, "preserve manual content"):
            self.flow.sync(WED)
        self.assertFalse(self.remote.writes)

    def test_ambiguous_meeting_never_writes(self):
        self.remote.candidates.append({"id": "4567890123456", "display_info": "Another meeting"})
        with self.assertRaisesRegex(w.WorkflowError, "Multiple meeting candidates"):
            self.flow.sync(WED)
        self.assertFalse(self.remote.writes)

    def test_wrong_date_or_nine_digit_number_is_rejected(self):
        with self.assertRaisesRegex(w.WorkflowError, "long meeting_id"):
            self.flow.find_meeting(WED, "123456789")
        with self.assertRaisesRegex(w.WorkflowError, "target date"):
            self.flow.find_meeting(FRI, "1234567890123")

    def test_no_meeting_remains_pending(self):
        self.remote.candidates = []
        self.assertEqual(self.flow.sync(WED)["status"], "pending_meeting")
        self.assertFalse(self.remote.writes)

    def test_note_does_not_require_a_recording(self):
        self.remote.meeting.pop("minute_token")
        result = self.flow.sync(WED)
        self.assertEqual(result["status"], "partial_artifacts")
        self.assertEqual(result["pending"], ["minute_token"])
        self.assertIsNotNone(self.remote.table.find("./tbody/tr")[1].find(".//cite"))

    def test_minutes_permission_does_not_discard_available_note(self):
        self.remote.minutes_denied = True
        result = self.flow.sync(WED)
        self.assertEqual(result["status"], "partial_artifacts")
        self.assertIn("minutes:minutes.basic:read", result["artifact_errors"]["minutes"])
        self.assertIsNotNone(self.remote.table.find("./tbody/tr")[1].find(".//cite"))

    def test_duplicate_dates_stop_before_insertion(self):
        self.remote.table.find("tbody").append(copy.deepcopy(self.remote.table.find("./tbody/tr")))
        with self.assertRaisesRegex(w.WorkflowError, "Duplicate date"):
            self.flow.create(FRI)
        self.assertFalse(self.remote.writes)

    def test_unknown_blank_row_is_not_adopted(self):
        self.remote.table.find("./tbody/tr")[0][0].text = ""
        with self.assertRaisesRegex(w.WorkflowError, "Unidentified blank row"):
            self.flow.create(FRI)
        self.assertFalse(self.remote.writes)

    def test_pending_other_day_blocks_new_insert(self):
        w.atomic_json(self.flow.journal, {"date": "2026-10-21", "table": "table"})
        with self.assertRaisesRegex(w.WorkflowError, "Finish pending row"):
            self.flow.create(FRI)
        self.assertFalse(self.remote.writes)

    def test_xml_escapes_labels_and_keeps_native_mention(self):
        cfg = config()
        cfg["projects"][0]["name"] = "A & B < C"
        root = ET.fromstring("<fragment>" + w.conclusions(cfg, WED) + "</fragment>")
        self.assertIn("A & B < C", w.xml_text(root))
        self.assertEqual(root.find(".//cite").get("user-id"), "ou_exampleA")

    def test_full_fetch_can_have_sibling_xml_roots(self):
        root = w.parse_xml('<title>Weekly</title><h1 id="heading">会议纪要</h1><h2>Q4</h2>')
        self.assertEqual([n.tag for n in root], ["title", "h1", "h2"])
        nested = w.parse_xml('<fragment mode="range"><table id="table"/></fragment>')
        self.assertEqual(list(nested.iter("table"))[0].get("id"), "table")

    def test_initialize_extracts_live_owners_and_fields(self):
        remote = self.remote

        class FullClient:
            def fetch(self, doc):
                root = w.parse_xml('<title>Weekly</title><h1 id="heading">会议纪要</h1>'
                                   '<h2>Q4</h2>' + ET.tostring(remote.table, encoding="unicode"))
                return {"document_id": "testdoc", "revision_id": 10}, root

        target = self.path.with_name("new-config.json")
        result = w.initialize(target, "https://example.feishu.cn/wiki/example",
                              "Example weekly", WED, FullClient())
        self.assertEqual(result["projects"], 2)
        stored = json.loads(target.read_text())
        self.assertEqual(stored["projects"], config()["projects"])
        self.assertEqual(stored["periods"], {"2026-Q4": "table"})

    def test_new_year_gets_a_separate_table_and_operation_log(self):
        flow = self.flow

        class QuarterClient(FakeLark):
            def __init__(self):
                super().__init__()
                self.created = False

            def fetch(self, doc, *scope):
                if "outline" in scope:
                    xml = ('<fragment><outline><h1 id="heading">会议纪要</h1>' +
                           ('<h2 id="newheading">2027 Q1</h2>' if self.created else '') +
                           '</outline></fragment>')
                    return {"revision_id": self.revision}, w.parse_xml(xml)
                return super().fetch(doc, *scope)

            def update(self, doc, revision, command, block, content):
                self.assert_revision = revision
                assert command == "block_insert_after"
                assert block == "heading"
                assert revision == self.revision
                root = w.parse_xml(content)
                self.table = root.find("table")
                self.table.set("id", "newtable")
                self.assign_ids(self.table)
                self.created = True
                self.revision += 1
                return {"result": "success"}

        flow.client = QuarterClient()
        date = dt.date(2027, 1, 1)
        self.assertEqual(flow.create(date)["status"], "created")
        self.assertEqual(flow.config["periods"]["2026-Q4"], "table")
        self.assertEqual(flow.config["periods"]["2027-Q1"], "newtable")
        event = json.loads(flow.operations.read_text().splitlines()[0])
        self.assertEqual(event["operation"], "create_quarter_and_row")
        self.assertEqual(event["after_version"], 11)


if __name__ == "__main__":
    unittest.main()
