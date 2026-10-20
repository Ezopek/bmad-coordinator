"""The backlog contract (backlog design A4): one set of checks every registered layout passes, run here against the
BMAD layout on the epic fixture. A second adapter adds its case class (and its fixtures) and joins `COVERED`, or the
registry test fails."""
import unittest

from coordinator_core import backlog, drive_epic, drive_pipeline
from coordinator_core.model import DomainError
from tests.test_bmad_core import git
from tests.test_drive_epic import (ARTIFACTS, EPIC_FILE, EPICS_PATH, LEDGER_FILE, LEDGER_OPEN, EpicDriveCase,
                                   spec_path, sprint_file)

COVERED = {"bmad"}


class RegistryTests(unittest.TestCase):
    def test_every_registered_kind_has_a_contract_case_and_the_registry_is_closed(self):
        self.assertEqual(COVERED, set(backlog.KINDS))
        self.assertIs(backlog.get(None), backlog.get("bmad"))
        self.assertEqual("bmad", backlog.get(None).kind)
        for bad in ("nope", "", 1, "BMAD"):
            with self.assertRaises(DomainError) as caught:
                backlog.get(bad)
            self.assertEqual("INVALID_INPUT", caught.exception.code, bad)
        self.assertEqual(("bmad", "bmad", "x"), (backlog.kind_of(None), backlog.kind_of({}),
                                                 backlog.kind_of({"backlog": {"kind": "x"}})))
        self.assertIs(backlog.resolve({}), backlog.get("bmad"))


class BmadContractTests(EpicDriveCase):
    """The checks of design A4 against the `bmad` adapter; the fixture is the epic test's (four stories, 1.1 done,
    1.3 gated on DW-1, the ledger settling it)."""
    KIND = "bmad"

    def setUp(self):
        super().setUp()
        self.layout = backlog.get(self.KIND)

    def test_stories_in_order_with_their_keys_and_statuses(self):
        rows = self.layout.stories(self.repo, self.base, EPICS_PATH, 1)
        self.assertEqual([("1.1", "Add a greeting", "1-1-greeting", "done"), ("1.2", "Add a farewell", "1-2-farewell", "backlog"),
                          ("1.3", "Add a shout", "1-3-shout", "backlog"), ("1.4", "Add a whisper", "1-4-whisper", "backlog")],
                         [(row["number"], row["title"], row["key"], row["status"]) for row in rows])
        two = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": sprint_file().replace(
            "  1-4-whisper: backlog", "  1-3-shoutier: backlog\n  1-4-whisper: backlog")}, "two keys")
        rows = self.layout.stories(self.repo, two, EPICS_PATH, 1)
        self.assertEqual((None, ["1-3-shout", "1-3-shoutier"], None), (rows[2]["key"], rows[2]["keys"], rows[2]["status"]))
        none = self.commit_files({f"{ARTIFACTS}/sprint-status.yaml": sprint_file().replace("  1-4-whisper: backlog\n", "")},
                                 "no key")
        rows = self.layout.stories(self.repo, none, EPICS_PATH, 1)
        self.assertEqual((None, []), (rows[3]["key"], rows[3]["keys"]))
        with self.assertRaises(DomainError) as caught:
            self.layout.stories(self.repo, self.base, EPICS_PATH, 7)
        self.assertEqual("STALE_INPUT", caught.exception.code)

    def test_the_entry_and_its_refusals(self):
        entry = self.layout.entry(self.repo, self.base, "1-3-shout", EPICS_PATH, spec_path("1-3-shout"))
        self.assertEqual(("Add a shout", ["As a user,", "I want a shout,", "So that the package can be loud."],
                          ["DW-1 — the decision on the exclamation mark"]),
                         (entry.title, entry.user_story, entry.preconditions))
        self.assertEqual(["**Given** a name", "**When** shout runs", "**Then** it returns HEY, <name>!"], entry.acceptance)
        self.assertTrue(entry.text.startswith("### Story 1.3: Add a shout") and "**Preconditions:**" in entry.text)
        self.assertEqual(entry.text, self.layout.entry_of_number(self.repo, self.base, EPICS_PATH, "1.3").text)
        for key, why in (("9-9-nothing", "not in sprint-status"), ("1-1-greeting", "not in sprint-status")):
            with self.assertRaises(DomainError) as caught:
                self.layout.entry(self.repo, self.base, key, EPICS_PATH, spec_path(key))
            self.assertEqual("INVALID_INPUT", caught.exception.code, key)
            self.assertIn(why, caught.exception.message)
        with self.assertRaises(DomainError):
            self.layout.entry_of_number(self.repo, self.base, EPICS_PATH, "1.9")
        bare = self.commit_files({EPICS_PATH: EPIC_FILE.replace("**Given** a name\n**When** farewell runs\n"
                                                                "**Then** it returns Goodbye, <name>!\n", "")}, "no acceptance")
        with self.assertRaises(DomainError) as caught:
            self.layout.entry(self.repo, bare, "1-2-farewell", EPICS_PATH, spec_path("1-2-farewell"))
        self.assertIn("no acceptance criteria", caught.exception.message)

    def test_preconditions_met_unmet_keep_open_and_archived(self):
        bullets = ["DW-1 — the decision on the exclamation mark", "a bullet naming nothing", "DW-7 — not in any ledger"]
        found = self.layout.preconditions(self.repo, self.base, ARTIFACTS, bullets)
        self.assertEqual([("DW-1", "met"), (None, "unmet"), ("DW-7", "unmet")],
                         [(item["id"], item["state"]) for item in found])
        self.assertTrue(found[1]["reason"] and found[2]["reason"])
        lines = self.layout.precondition_lines(found, self.base)
        self.assertTrue(any("DW-1" in line for line in lines) and any("unmet" in line for line in lines), lines)
        kept = self.commit_files({f"{ARTIFACTS}/deferred-work.md": LEDGER_OPEN}, "keep open")
        self.assertEqual("unmet", self.layout.preconditions(self.repo, kept, ARTIFACTS, bullets[:1])[0]["state"])
        archived = self.commit_files({f"{ARTIFACTS}/deferred-work.md": "# Deferred work\n",
                                      f"{ARTIFACTS}/deferred-work-archive.md": LEDGER_FILE.replace("status: open", "status: done")},
                                     "archived")
        self.assertEqual("met", self.layout.preconditions(self.repo, archived, ARTIFACTS, bullets[:1])[0]["state"])
        self.assertEqual("unmet", self.layout.preconditions(self.repo, archived, ARTIFACTS, ["DW-5 — in no ledger"])[0]["state"])

    def test_status_paths_edits_and_the_ledger_append(self):
        paths = self.layout.status_paths(ARTIFACTS)
        self.assertEqual({"sprint": f"{ARTIFACTS}/sprint-status.yaml", "ledger": f"{ARTIFACTS}/deferred-work.md"}, paths)
        text = sprint_file()
        started = self.layout.edit_status(text, "1-2-farewell", "in-progress")
        self.assertIn("  1-2-farewell: in-progress", started)
        self.assertEqual(started, self.layout.edit_status(started, "1-2-farewell", "in-progress"))   # idempotent
        done = text
        for key in ("1-2-farewell", "1-3-shout", "1-4-whisper"):
            done = self.layout.edit_status(done, key, "done")
        self.assertIn("  epic-1: done", done)
        self.assertIn("  epic-2: backlog", done)   # the next epic untouched, order kept
        styled = "﻿" + text.replace("\n", "\r\n")
        edited = self.layout.edit_status(styled, "1-2-farewell", "done")
        self.assertTrue(edited.startswith("﻿") and "\r\n" in edited and "\n" not in edited.replace("\r\n", ""))
        entry = {"title": "Trailing space", "origin": "coordinator d1", "location": "pkg/x.py", "source_spec": "spec-1-2-farewell.md",
                 "severity": "medium", "reason": "left open", "decision": "2026-10-20 keep"}
        appended = self.layout.append_ledger(LEDGER_FILE, [entry])
        self.assertIn("### DW-2: Trailing space", appended)
        self.assertTrue(appended.startswith(LEDGER_FILE.rstrip("\n")))
        self.assertIn("### DW-3:", self.layout.append_ledger(appended, [entry]))
        self.assertEqual(LEDGER_FILE, self.layout.append_ledger(LEDGER_FILE, []))

    def test_protected_directories_and_the_pipeline_block(self):
        self.assertEqual(("_bmad/", "_bmad-output/", ".bmad-loop/"), self.layout.protected())
        plain = drive_epic.fill_template(self.template(), "0" * 32, "1-2-farewell")
        self.assertNotIn("backlog", plain["project"])   # absent: no default inserted, the digest stands
        named = self.template()
        named["project"] = {**named["project"], "backlog": {"kind": "bmad"}}
        self.assertEqual({"kind": "bmad"}, drive_epic.fill_template(named, "0" * 32, "1-2-farewell")["project"]["backlog"])
        for bad in ({"kind": "nope"}, {"kind": "bmad", "x": 1}, {}, "bmad", ["bmad"]):
            broken = self.template()
            broken["project"] = {**broken["project"], "backlog": bad}
            with self.assertRaises(DomainError) as caught:
                drive_pipeline.validate(drive_epic.fill_template.__wrapped__(broken, "0" * 32, "1-2-farewell")
                                        if hasattr(drive_epic.fill_template, "__wrapped__") else
                                        _filled(broken))
            self.assertEqual("INVALID_INPUT", caught.exception.code, bad)


def _filled(template: dict) -> dict:
    """The template with the sample values, before validation (fill_template validates itself)."""
    import json
    text = json.dumps(template).replace("__FEATURE__", "0" * 32).replace("__KEY__", "1-2-farewell")
    return json.loads(text)
