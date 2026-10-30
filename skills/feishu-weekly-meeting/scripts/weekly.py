#!/usr/bin/env python3
"""Create recurring Docx table rows and register meeting artifacts with lark-cli."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

HEADERS = ["日期", "AI 总结", "摘要（妙记）", "关键结论"]
PENDING = "会后补充"
DEFAULT_CONFIG = Path.home() / ".config/feishu-weekly-meeting/config.json"


class WorkflowError(RuntimeError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        os.chmod(temp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temp.replace(path)


def xml_text(node):
    return "".join(node.itertext()).strip()


def parse_xml(content):
    # Full CLI responses are sibling blocks; scoped responses already have a wrapper.
    return ET.fromstring("<document-fragment>" + content + "</document-fragment>")


def digest(node):
    return hashlib.sha256(ET.tostring(node, encoding="utf-8")).hexdigest()


def period(date):
    return f"{date.year}-Q{(date.month - 1) // 3 + 1}"


def meeting_dates(date, single=False):
    if single:
        if date.weekday() not in (2, 4):
            raise WorkflowError("Meeting date must be Wednesday or Friday")
        return [date]
    monday = date - dt.timedelta(days=date.weekday())
    return [monday + dt.timedelta(days=2), monday + dt.timedelta(days=4)]


def parse_row_date(text, year):
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:[（(]周[三五][）)])?", text)
    if not match:
        raise WorkflowError(f"Unrecognized date cell: {text!r}")
    return dt.date(year, int(match[1]), int(match[2]))


def child_xml(cell):
    return "".join(ET.tostring(child, encoding="unicode") for child in cell)


def semantic(node):
    """Compare text and resource identities without server-generated block metadata."""
    return (xml_text(node), tuple(c.get("user-id") for c in node.iter("cite")
                                  if c.get("type") == "user"),
            tuple(c.get("doc-id") for c in node.iter("cite")
                  if c.get("type") == "doc"),
            tuple(c.get("href") for c in node.iter("a")))


def placeholder(cell):
    return (xml_text(cell) in ("", PENDING, "待补充")
            and not list(cell.iter("cite")) and not list(cell.iter("a")))


def conclusions(config, date):
    root = ET.Element("fragment")
    ET.SubElement(ET.SubElement(root, "p"), "b").text = (
        f"Q{(date.month - 1) // 3 + 1} 专项进展与里程碑")
    for index, project in enumerate(config["projects"]):
        if index:
            ET.SubElement(root, "hr")
        bold = ET.SubElement(ET.SubElement(root, "p"), "b")
        bold.text = project["name"] + "【"
        for owner in project["owners"]:
            ET.SubElement(bold, "cite", {"type": "user", "user-id": owner})
        bold[-1].tail = "】"
        for label in project["fields"]:
            ET.SubElement(ET.SubElement(root, "p"), "b").text = label
            ET.SubElement(root, "p").text = "待补充"
    return child_xml(root)


def row_content(config, date):
    return [f"<p>{date:%m/%d}</p>", f"<p>{PENDING}</p>",
            f"<p>{PENDING}</p>", conclusions(config, date)]


def new_table(config, date):
    table = ET.Element("table")
    cols = ET.SubElement(table, "colgroup")
    for width in config.get("column_widths", [100, 187, 241, 460]):
        ET.SubElement(cols, "col", {"width": str(width)})
    head = ET.SubElement(ET.SubElement(table, "thead"), "tr")
    for label in HEADERS:
        ET.SubElement(ET.SubElement(head, "th"), "p", {"align": "center"}).text = label
    row = ET.SubElement(ET.SubElement(table, "tbody"), "tr")
    for content in row_content(config, date):
        cell = ET.SubElement(row, "td", {"vertical-align": "top"})
        cell.extend(ET.fromstring("<fragment>" + content + "</fragment>"))
    return ET.tostring(table, encoding="unicode")


class Lark:
    def call(self, args, content=None):
        env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1",
                   LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
        result = subprocess.run([os.environ.get("LARK_CLI", "lark-cli"), *args,
                                 "--as", "user"], input=content, text=True,
                                capture_output=True, env=env, timeout=120)
        try:
            data = json.loads(result.stdout if result.returncode == 0 else result.stderr)
        except (ValueError, TypeError):
            raise WorkflowError(f"lark-cli {' '.join(args[:2])} failed without JSON; "
                                "inspect CLI authentication; no automatic write retry") from None
        if result.returncode or data.get("ok") is not True:
            error = data.get("error", {})
            missing = error.get("missing_scopes", [])
            raise WorkflowError(json.dumps({"type": error.get("type"),
                "message": error.get("message", "CLI failure"),
                "missing_scopes": missing}, ensure_ascii=False))
        return data["data"]

    def fetch(self, doc, *scope):
        result = self.call(["docs", "+fetch", "--doc", doc,
                            "--detail", "full", *scope])["document"]
        return result, parse_xml(result["content"])

    def api(self, method, path, params=None, body=None):
        args = ["api", method, path]
        if params:
            args += ["--params", json.dumps(params)]
        if body is not None:
            args += ["--data", "-"]
        return self.call(args, json.dumps(body) if body is not None else None)

    def update(self, doc, revision, command, block, content):
        result = self.call(["docs", "+update", "--doc", doc,
            "--revision-id", str(revision), "--command", command,
            "--block-id", block, "--content", "-"], content)
        if result.get("result") != "success" or result.get("warnings"):
            raise WorkflowError("Partial document write; inspect live document before resuming")
        return result


class Snapshot:
    def __init__(self, document, root, table_id, year):
        self.revision = document["revision_id"]
        matches = [n for n in root.iter("table") if n.get("id") == table_id]
        if len(matches) != 1:
            raise WorkflowError("Configured table missing or ambiguous")
        self.table = matches[0]
        self.rows = self.table.findall("./tbody/tr")
        headers = self.table.findall("./thead/tr/th")
        if [xml_text(n) for n in headers] != HEADERS or any(len(r) != 4 for r in self.rows):
            raise WorkflowError("Expected an unmerged four-column weekly table")
        self.by_date = {}
        for index, row in enumerate(self.rows):
            text = xml_text(row[0])
            if not text:
                continue  # A journaled insertion may be awaiting its first cell.
            date = parse_row_date(text, year)
            if date in self.by_date:
                raise WorkflowError(f"Duplicate date rows: {date}")
            self.by_date[date] = (index, row)

    def assert_preserved(self, after, excluded=()):
        for date, (_, row) in self.by_date.items():
            if date not in excluded:
                found = after.by_date.get(date)
                if found is None or digest(row) != digest(found[1]):
                    raise WorkflowError(f"Historical row changed during operation: {date}; inspect before retry")
        for tag in ("colgroup", "thead"):
            if ET.tostring(self.table.find(tag)) != ET.tostring(after.table.find(tag)):
                raise WorkflowError("Table columns or header changed during operation")


class Workflow:
    def __init__(self, config_path, client=None, dry_run=False):
        self.path = Path(config_path).expanduser()
        self.config = json.loads(self.path.read_text())
        self.client = client or Lark()
        self.dry_run = dry_run
        self.doc = self.config["document_id"]
        self.base = f"/open-apis/docx/v1/documents/{self.doc}/blocks"
        self.journal = self.path.with_name(self.path.stem + ".pending.json")
        self.operations = self.path.with_name(self.path.stem + ".operations.jsonl")
        self.new_quarter_events = {}
        if not self.config.get("projects"):
            raise WorkflowError("Configure at least one project and its verified owners")
        for project in self.config["projects"]:
            if not project.get("owners") or any(not re.fullmatch(r"ou_[A-Za-z0-9]+", x)
                                                 for x in project["owners"]):
                raise WorkflowError("Project owners must be verified open_ids")

    def save_config(self):
        atomic_json(self.path, self.config)

    def log(self, action, date, before, after, **extra):
        event = {"operation_id": str(uuid.uuid4()), "operation": action,
                 "asset_id": "lark:docx:" + self.doc, "date": str(date),
                 "before_version": before, "after_version": after,
                 "result": "verified_success", "verified_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                 **extra}
        with open(self.operations, "a", encoding="utf-8") as f:
            os.chmod(self.operations, 0o600)
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def snapshot(self, table, date):
        document, root = self.client.fetch(self.doc, "--scope", "range",
                                          "--start-block-id", table, "--end-block-id", table)
        snap = Snapshot(document, root, table, date.year)
        if any(period(d) != period(date) for d in snap.by_date):
            raise WorkflowError("Configured table contains another quarter or year")
        return snap

    def table_cells(self, table, revision):
        block = self.client.api("GET", self.base + "/" + table,
            {"document_revision_id": revision, "user_id_type": "open_id"})["block"]
        if block["table"]["property"]["column_size"] != 4:
            raise WorkflowError("Unexpected table column count")
        return block["table"]["cells"]

    def table_for(self, date):
        key = period(date)
        if key in self.config["periods"]:
            return self.config["periods"][key]
        title = key.replace("-", " ")
        document, root = self.client.fetch(self.doc, "--scope", "outline", "--max-depth", "2")
        headings = [n for n in root.iter("h2") if xml_text(n) == title]
        if len(headings) > 1:
            raise WorkflowError(f"Multiple quarter headings: {title}")
        created = not headings
        if created:
            if self.dry_run:
                return None
            self.client.update(self.doc, document["revision_id"], "block_insert_after",
                self.config["meeting_anchor"], f"<h2>{title}</h2>" + new_table(self.config, date))
            _, root = self.client.fetch(self.doc, "--scope", "outline", "--max-depth", "2")
            headings = [n for n in root.iter("h2") if xml_text(n) == title]
        if len(headings) != 1:
            raise WorkflowError("Quarter heading readback failed; do not recreate blindly")
        doc, section = self.client.fetch(self.doc, "--scope", "section",
                                         "--start-block-id", headings[0].get("id"))
        tables = list(section.iter("table"))
        if len(tables) != 1:
            raise WorkflowError("Quarter table is missing or ambiguous; inspect partial creation")
        table = tables[0].get("id")
        snap = Snapshot(doc, section, table, date.year)
        if created and date in snap.by_date:
            self.verify_row(snap.by_date[date][1], date)
        if created and date not in snap.by_date:
            raise WorkflowError("New quarter is missing its date row")
        if not self.dry_run:
            self.config["periods"][key] = table
            self.save_config()
            if created:
                self.new_quarter_events[date] = self.log(
                    "create_quarter_and_row", date, document["revision_id"], doc["revision_id"])
        return table

    def verify_row(self, row, date):
        expected = row_content(self.config, date)
        for cell, xml in zip(row, expected):
            if semantic(cell) != semantic(ET.fromstring("<td>" + xml + "</td>")):
                raise WorkflowError(f"Created row readback differs: {date}")

    def fill(self, table, date, cells, column, xml):
        before = self.snapshot(table, date)
        current_cells = self.table_cells(table, before.revision)
        positions = [i for i in range(4, len(current_cells), 4)
                     if current_cells[i:i + 4] == cells]
        if len(positions) != 1:
            raise WorkflowError("Inserted row cells moved or changed unexpectedly")
        row_index = positions[0] // 4 - 1
        cell = before.rows[row_index][column]
        desired = ET.fromstring("<td>" + xml + "</td>")
        if semantic(cell) == semantic(desired):
            return
        if len(cell) == 0 and placeholder(cell):
            # Some table APIs return new cells without an initial paragraph.
            self.client.api("POST", self.base + "/" + cells[column] + "/children",
                {"document_revision_id": before.revision, "client_token": str(uuid.uuid4()),
                 "user_id_type": "open_id"},
                {"index": 0, "children": [{"block_type": 2,
                    "text": {"elements": [{"text_run": {
                        "content": f"{date:%m/%d}" if column == 0 else "待补充"}}]}}]})
            check = self.snapshot(table, date)
            if len(check.rows[row_index][column]) != 1:
                raise WorkflowError("Empty-cell paragraph creation failed; inspect before resuming")
            return self.fill(table, date, cells, column, xml)
        if not placeholder(cell) or len(cell) != 1 or not cell[0].get("id"):
            raise WorkflowError("Cell contains manual content; refusing to replace it")
        self.client.update(self.doc, before.revision, "block_replace", cell[0].get("id"), xml)
        after = self.snapshot(table, date)
        before.assert_preserved(after, excluded=[date])
        if semantic(after.rows[row_index][column]) != semantic(desired):
            raise WorkflowError("Cell readback failed; inspect partial write")

    def create(self, date):
        table = self.table_for(date)
        if table is None:
            return {"date": str(date), "status": "would_create_quarter_and_row"}
        before = self.snapshot(table, date)
        pending = json.loads(self.journal.read_text()) if self.journal.exists() else None
        if pending and (pending["date"] != str(date) or pending["table"] != table):
            raise WorkflowError(f"Finish pending row {pending['date']} before another creation")
        if date in before.by_date and not pending:
            if date in self.new_quarter_events:
                return {"date": str(date), "status": "created", "revision": before.revision,
                        "operation_id": self.new_quarter_events[date]["operation_id"]}
            return {"date": str(date), "status": "exists", "revision": before.revision}
        if self.dry_run:
            return {"date": str(date), "status": "would_resume" if pending else "would_create",
                    "revision": before.revision, "content": row_content(self.config, date)}
        if not pending:
            if any(not xml_text(row[0]) for row in before.rows):
                raise WorkflowError("Unidentified blank row exists; inspect it before inserting another")
            # Require the existing order to be descending; preserve it on insertion.
            ordered = list(before.by_date)
            if ordered != sorted(ordered, reverse=True):
                raise WorkflowError("Existing date rows are not in descending order")
            index = 1 + sum(d > date for d in ordered)
            pending = {"date": str(date), "table": table, "index": index,
                       "revision": before.revision, "client_token": str(uuid.uuid4()),
                       "old_cells": self.table_cells(table, before.revision),
                       "protected": {str(d): digest(row) for d, (_, row) in before.by_date.items()}}
            atomic_json(self.journal, pending)
            self.client.api("PATCH", self.base + "/batch_update",
                {"document_revision_id": before.revision, "client_token": pending["client_token"],
                 "user_id_type": "open_id"},
                {"requests": [{"block_id": table, "insert_table_row": {"row_index": index}}]})
        snap = self.snapshot(table, date)
        current = self.table_cells(table, snap.revision)
        offset = pending["index"] * 4
        if (len(current) != len(pending["old_cells"]) + 4
                or current[:offset] + current[offset + 4:] != pending["old_cells"]):
            raise WorkflowError("Pending insertion is uncertain or table changed; inspect journal and remote table")
        cells = current[offset:offset + 4]
        for column, xml in enumerate(row_content(self.config, date)):
            self.fill(table, date, cells, column, xml)
        after = self.snapshot(table, date)
        self.verify_row(after.by_date[date][1], date)
        for old_date, old_hash in pending["protected"].items():
            found = after.by_date.get(dt.date.fromisoformat(old_date))
            if found is None or digest(found[1]) != old_hash:
                raise WorkflowError("Historical row changed; inspect before clearing pending operation")
        event = self.log("create_row", date, pending["revision"], after.revision,
                         protected_rows=len(pending["protected"]))
        self.journal.unlink()
        return {"date": str(date), "status": "created", "revision": after.revision,
                "operation_id": event["operation_id"]}

    def find_meeting(self, date, meeting_id=None):
        if meeting_id is not None:
            if not re.fullmatch(r"\d{10,}", meeting_id):
                raise WorkflowError("Use the long meeting_id, not the nine-digit meeting number")
        else:
            found, page = {}, None
            timezone = ZoneInfo(self.config["timezone"])
            start = dt.datetime.combine(date, dt.time.min, timezone).isoformat()
            end = dt.datetime.combine(date, dt.time(23, 59, 59), timezone).isoformat()
            while True:
                args = ["vc", "+search", "--query", self.config["meeting_query"],
                        "--start", start, "--end", end, "--page-size", "30"]
                if page:
                    args += ["--page-token", page]
                data = self.client.call(args)
                for item in data.get("items", []):
                    found[item["id"]] = item
                if not data.get("has_more"):
                    break
                next_page = data.get("page_token")
                if not next_page or next_page == page or len(found) > 50:
                    raise WorkflowError("Meeting search exceeds bounded paging; narrow the query")
                page = next_page
            if not found:
                return None
            if len(found) != 1:
                raise WorkflowError("Multiple meeting candidates; select --meeting-id: " +
                    json.dumps([{ "meeting_id": x["id"], "description": x.get("display_info", "")}
                                for x in found.values()], ensure_ascii=False))
            meeting_id = next(iter(found))
        result = self.client.call(["vc", "+detail", "--meeting-ids", meeting_id])
        meetings = result.get("meetings", [])
        if len(meetings) != 1 or meetings[0].get("meeting_id") != meeting_id:
            raise WorkflowError("Meeting detail lookup failed")
        meeting = meetings[0]
        if not meeting.get("end_time") or not meeting.get("start_time", "").startswith(str(date)):
            raise WorkflowError("Meeting is unfinished or does not belong to the target date")
        return meeting

    def artifacts(self, meeting):
        note_id, minute = meeting.get("note_id"), meeting.get("minute_token")
        result = {"meeting_id": meeting["meeting_id"], "artifact_errors": {}}
        # These are independent branches: an absent recording must not discard a Note.
        if minute:
            try:
                data = self.client.call(["minutes", "+detail", "--minute-tokens", minute])
                rows = [x for x in data.get("minutes", []) if x.get("minute_token") == minute]
                if len(rows) != 1 or not rows[0].get("title"):
                    raise WorkflowError("Minutes detail is unavailable")
                result["minute_token"] = minute
                note_id = note_id or rows[0].get("note_id")
            except WorkflowError as error:
                result["artifact_errors"]["minutes"] = str(error)
        if note_id:
            try:
                note = self.client.call(["note", "+detail", "--note-id", note_id]).get("note", {})
                token = note.get("note_doc_token")
                if token:
                    # Read the actual summary document to prove the current user can access it.
                    doc, _ = self.client.fetch(token)
                    result.update(note_doc_token=token, summary_characters=len(doc["content"]))
            except WorkflowError as error:
                result["artifact_errors"]["note"] = str(error)
        return result

    def sync(self, date, meeting_id=None):
        table = self.table_for(date)
        if not table:
            return {"date": str(date), "status": "create_row_first"}
        before = self.snapshot(table, date)
        if date not in before.by_date:
            if self.dry_run:
                return {"date": str(date), "status": "create_row_first"}
            raise WorkflowError("Target row missing; run create first")
        meeting = self.find_meeting(date, meeting_id)
        if meeting is None:
            return {"date": str(date), "status": "pending_meeting"}
        artifacts = self.artifacts(meeting)
        replacements = {}
        if artifacts.get("note_doc_token"):
            p = ET.Element("p")
            ET.SubElement(p, "cite", {"type": "doc", "doc-id": artifacts["note_doc_token"]})
            replacements[1] = ET.tostring(p, encoding="unicode")
        if artifacts.get("minute_token"):
            host = self.config["document_origin"]
            replacements[2] = f"<p>{host}/minutes/{artifacts['minute_token']}</p>"
        changes = []
        # Validate every target before writing either field.
        for column, xml in replacements.items():
            cell = before.by_date[date][1][column]
            desired = ET.fromstring("<td>" + xml + "</td>")
            if semantic(cell) == semantic(desired):
                continue
            if not placeholder(cell):
                raise WorkflowError(f"Existing artifact differs in column {column}; preserve manual content")
            changes.append((column, xml))
        if self.dry_run:
            return {"date": str(date), "status": "would_sync" if changes else "already_synced",
                    "columns": [x[0] for x in changes], **artifacts}
        protected_conclusions = digest(before.by_date[date][1][3])
        for column, xml in changes:
            snap = self.snapshot(table, date)
            index, _ = snap.by_date[date]
            cells = self.table_cells(table, snap.revision)[(index + 1) * 4:(index + 2) * 4]
            self.fill(table, date, cells, column, xml)
        after = self.snapshot(table, date)
        before.assert_preserved(after, excluded=[date])
        if digest(after.by_date[date][1][3]) != protected_conclusions:
            raise WorkflowError("Key conclusions changed during sync; inspect live content")
        status = "synced" if changes else "already_synced"
        missing = [name for name in ("note_doc_token", "minute_token") if not artifacts.get(name)]
        if missing:
            status = "partial_artifacts" if replacements else "pending_artifacts"
        if changes:
            self.log("sync_artifacts", date, before.revision, after.revision,
                     meeting_id=meeting["meeting_id"], columns=[x[0] for x in changes])
        return {"date": str(date), "status": status, "revision": after.revision,
                "pending": missing, **artifacts}


def initialize(path, doc_url, query, date, client=None):
    from urllib.parse import urlparse
    path = Path(path).expanduser()
    if path.exists():
        raise WorkflowError("Config already exists; edit it explicitly instead of overwriting")
    client = client or Lark()
    document, root = client.fetch(doc_url)
    quarter = f"Q{(date.month - 1) // 3 + 1}"
    candidates = []
    active_heading = None
    for node in root:
        if node.tag in ("h1", "h2"):
            active_heading = xml_text(node)
        if node.tag == "table" and active_heading in (quarter, f"{date.year} {quarter}"):
            if [xml_text(n) for n in node.findall("./thead/tr/th")] == HEADERS:
                candidates.append(node)
    if len(candidates) != 1:
        raise WorkflowError("Cannot uniquely discover current-quarter table; configure IDs from a live fetch")
    table = candidates[0]
    projects = []
    for row in table.findall("./tbody/tr"):
        current = None
        candidate_projects = []
        for node in row[3]:
            owners = [c.get("user-id") for c in node.iter("cite") if c.get("type") == "user"]
            text = xml_text(node)
            if owners and "【" in text:
                current = {"name": text.split("【", 1)[0].strip(), "owners": owners, "fields": []}
                candidate_projects.append(current)
            elif current:
                # Field headings may be paragraphs or ordered-list items.
                for entry in (list(node) if node.tag in ("ol", "ul") else [node]):
                    label = xml_text(entry).rstrip("：:")
                    if re.fullmatch(r"当前.{0,10}进展|后续建设|能力交付节点|风险与依赖", label):
                        if label not in current["fields"]:
                            current["fields"].append(label)
        if candidate_projects and all(p["fields"] for p in candidate_projects):
            projects = candidate_projects
            break
    anchors = [n for n in root.iter("h1") if xml_text(n) == "会议纪要"]
    if not projects or len(anchors) != 1:
        raise WorkflowError("Cannot discover project owners/fields or meeting anchor; inspect live structure")
    parsed = urlparse(doc_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise WorkflowError("Initialize with the HTTPS Feishu document URL")
    config = {"schema_version": 1, "document_url": doc_url,
              "document_id": document["document_id"], "document_origin": f"https://{parsed.netloc}",
              "timezone": "Asia/Shanghai", "meeting_query": query,
              "meeting_anchor": anchors[0].get("id"),
              "periods": {period(date): table.get("id")}, "projects": projects,
              "column_widths": [int(c.get("width")) for c in table.findall("./colgroup/col")]}
    atomic_json(path, config)
    return {"status": "configured", "config": str(path), "projects": len(projects),
            "period": period(date), "revision": document["revision_id"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=["init", "create", "sync", "run"], default="run")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--date", help="One Wednesday/Friday, YYYY-MM-DD")
    parser.add_argument("--week", help="Any date in the target Monday-Sunday week, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--doc", help="Used once by init")
    parser.add_argument("--meeting-query", help="Real meeting topic keyword, used by init")
    parser.add_argument("--meeting-id", help="Explicit long meeting ID; requires sync --date")
    args = parser.parse_args(argv)
    if args.date and args.week:
        parser.error("Choose --date or --week")
    if args.meeting_id and (args.command != "sync" or not args.date):
        parser.error("--meeting-id requires sync --date")
    path = Path(args.config).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path.with_suffix(".lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkflowError("Another workflow is running with this config") from None
        if args.command == "init":
            if not args.doc or not args.meeting_query or args.dry_run:
                parser.error("init requires --doc and --meeting-query; it only writes local config")
            date = dt.date.fromisoformat(args.date or args.week) if (args.date or args.week) else dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
            print(json.dumps(initialize(path, args.doc, args.meeting_query, date), ensure_ascii=False, indent=2))
            return
        workflow = Workflow(path, dry_run=args.dry_run)
        today = dt.datetime.now(ZoneInfo(workflow.config["timezone"])).date()
        date = dt.date.fromisoformat(args.date or args.week) if (args.date or args.week) else today
        dates = meeting_dates(date, single=bool(args.date))
        results = []
        if args.command in ("create", "run"):
            for day in dates:
                results.append(workflow.create(day))
        if args.command in ("sync", "run"):
            for day in dates:
                if day > today:
                    results.append({"date": str(day), "status": "future_meeting"})
                else:
                    results.append(workflow.sync(day, args.meeting_id))
        print(json.dumps({"ok": True, "dry_run": args.dry_run, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, ValueError, KeyError, ET.ParseError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
