"""The bill (bill design B1–B3) on fake clients: every launch of an epic as a row with its observed and reported
parts, the aggregates by story, role and client/model, the unreported counts, the scopes and the refusals."""
import json
import subprocess
import unittest
from pathlib import Path

from coordinator_core import bill, drive_epic
from coordinator_core.repo import resolve_repo
from tests.test_bmad_core import git
from tests.test_bmad_implementation import SPEC_PATH, SPEC_READY, SPEC_WITH_QUESTION
from tests.test_drive_epic import ARTIFACTS, EpicDriveCase
from tests.test_drive_story import Q1

CLAUDE_ROLES = {"worker": {"client": "claude-code", "model": "fake-model", "timeout": 60},
                "reviewer": {"client": "claude-code", "model": "fake-review", "timeout": 60},
                "judgment": {"client": "codex-cli", "model": "fake-judge", "timeout": 60}}
FAKE_USD = 0.0123          # what the fake Claude reports per launch (tests/fake_clients.py)
CODEX_USAGE = {"input": 1000, "cached_input": 400, "cache_write": 0, "output": 50, "reasoning": 5}


class BillUnitTests(unittest.TestCase):
    def test_reported_copies_the_clients_figures_and_maps_their_token_keys(self):
        claude = bill.reported("claude-code", {"total_cost_usd": 0.5, "num_turns": 2, "usage": {
            "input_tokens": 3, "cache_read_input_tokens": 40, "cache_creation_input_tokens": 5, "output_tokens": 7}})
        self.assertEqual((0.5, 2, {"input": 3, "cached_input": 40, "cache_write": 5, "output": 7}, None),
                         (claude["usd"], claude["client_turns"], claude["tokens"], claude["premium_requests"]))
        old = bill.reported("claude-code", {"total_cost_usd": 0.5, "num_turns": 2})   # a receipt sealed before B2
        self.assertEqual((0.5, 2, None), (old["usd"], old["client_turns"], old["tokens"]))
        codex = bill.reported("codex-cli", {"usage": {"input_tokens": 10, "cached_input_tokens": 4,
                                                      "output_tokens": 2, "reasoning_output_tokens": 1}})
        self.assertEqual((None, None, {"input": 10, "cached_input": 4, "output": 2, "reasoning": 1}),
                         (codex["usd"], codex["client_turns"], codex["tokens"]))
        copilot = bill.reported("copilot", {"usage": {"premiumRequests": 2}, "session_usage": {
            "usage": {"inputTokens": 9, "outputTokens": 1, "cacheReadTokens": 3}, "requests": {}}})
        self.assertEqual((None, 2, {"input": 9, "cached_input": 3, "output": 1}),
                         (copilot["usd"], copilot["premium_requests"], copilot["tokens"]))
        self.assertIsNone(bill.reported("codex-cli", None))
        self.assertEqual({"usd": None, "tokens": None, "premium_requests": None, "client_turns": None,
                          "client_facts": {}}, bill.reported("codex-cli", {}))

    def test_aggregate_sums_what_is_reported_and_counts_what_is_not(self):
        rows = [{"observed": {"wall_seconds": 1.5}, "reported": {"usd": 0.25, "premium_requests": None,
                                                                  "tokens": {"input": 10, "output": 1}}},
                {"observed": {"wall_seconds": None}, "reported": None},
                {"observed": {"wall_seconds": 2}, "reported": {"usd": None, "premium_requests": 1, "tokens": None}}]
        total = bill.aggregate(rows, {"runs": 2, "driver_turns": 9})
        self.assertEqual({"launches": 3, "wall_seconds": 3.5, "runs": 2, "driver_turns": 9}, total["observed"])
        self.assertEqual({"usd": 0.25, "premium_requests": 1, "tokens": {"input": 10, "cached_input": 0,
                                                                          "cache_write": 0, "output": 1,
                                                                          "reasoning": 0}}, total["reported"])
        self.assertEqual({"launches": 1, "usd": 2, "premium_requests": 2, "tokens": 2}, total["unreported"])


class BillTests(EpicDriveCase):
    def git_show(self, ref: str, path: str) -> str:
        return git(self.main, "show", f"{ref}:{path}")

    def bill_of(self, *args, code=0):
        _, out, err = self.run_cli("bill", *args, code=code)
        return out if code == 0 else err

    def test_the_bill_of_an_epic_with_claude_workers_and_reviewers(self):
        code, summary, err = self.epic_start(code=0, template=self.write_template(roles=CLAUDE_ROLES))
        self.assertEqual("epic-done", summary["reason"], (summary, err))
        out = self.bill_of("--epic", summary["epic_id"])
        # B4: the epic summary, epic status and the spec's result section carry the same numbers
        self.assertEqual(out["totals"], summary["bill"])
        status = self.run_cli("epic", "status", "--epic", summary["epic_id"], code=0)[1]["epics"][0]
        self.assertEqual(out["totals"], status["bill"])
        self.assertEqual([None] + [item["observed"]["launches"] for item in out["by_story"]],
                         [story.get("bill") and story["bill"]["observed"]["launches"]
                          for story in status["state"]["stories"]])
        self.assertEqual(out["by_story"][1]["reported"], status["state"]["stories"][2]["bill"]["reported"])
        spec = self.git_show("story/1-3-shout", f"{ARTIFACTS}/spec-1-3-shout.md")
        line = next(line for line in spec.splitlines() if line.startswith("- bill: "))
        self.assertRegex(line, r"^- bill: 4 launches, [0-9]+ s client wall clock; reported by the clients: "
                               r"USD 0\.05, 4 input and 4 output tokens$")
        self.assertEqual(("bill", 1, ["1-2-farewell", "1-3-shout", "1-4-whisper"], summary["epic_id"]),
                         (out["kind"], out["format"], out["scope"]["stories"], out["scope"]["epic_id"]))
        rows = out["launches"]
        self.assertEqual(12, len(rows))
        self.assertEqual([("1-2-farewell", "worker", "spec"), ("1-2-farewell", "reviewer", "spec-review"),
                          ("1-2-farewell", "worker", "impl"), ("1-2-farewell", "reviewer", "impl-review")] * 1 +
                         [(key, role, step) for key in ("1-3-shout", "1-4-whisper")
                          for role, step in (("worker", "spec"), ("reviewer", "spec-review"), ("worker", "impl"),
                                             ("reviewer", "impl-review"))],
                         [(row["story"], row["role"], row["step_id"]) for row in rows])
        self.assertEqual(sorted(row["started_at"] for row in rows), [row["started_at"] for row in rows])
        for row in rows:
            self.assertEqual(("observed", "claude-code", "completed", None), (row["state"], row["client"],
                                                                             row["outcome"], row["attempt"]))
            self.assertEqual("fake-review" if row["role"] == "reviewer" else "fake-model", row["model"])
            self.assertGreater(row["observed"]["wall_seconds"], 0)
            self.assertEqual((FAKE_USD, 3, {"input": 1, "output": 1}, None),
                             (row["reported"]["usd"], row["reported"]["client_turns"], row["reported"]["tokens"],
                              row["reported"]["premium_requests"]))
            self.assertEqual(FAKE_USD, row["reported"]["client_facts"]["total_cost_usd"])
            self.assertTrue(row["lease_id"] and row["run_id"] and row["feature_id"] and row["assignment_id"])
        totals = out["totals"]
        self.assertEqual((12, 3), (totals["observed"]["launches"], totals["observed"]["runs"]))
        self.assertGreater(totals["observed"]["driver_turns"], 12)
        self.assertGreater(totals["observed"]["wall_seconds"], 0)
        self.assertEqual({"usd": round(12 * FAKE_USD, 6), "premium_requests": 0,
                          "tokens": {"input": 12, "cached_input": 0, "cache_write": 0, "output": 12, "reasoning": 0}},
                         totals["reported"])
        self.assertEqual({"launches": 0, "usd": 0, "premium_requests": 12, "tokens": 0}, totals["unreported"])
        self.assertEqual([("1-2-farewell", 4, 1, round(4 * FAKE_USD, 6)), ("1-3-shout", 4, 1, round(4 * FAKE_USD, 6)),
                          ("1-4-whisper", 4, 1, round(4 * FAKE_USD, 6))],
                         [(item["story"], item["observed"]["launches"], item["observed"]["runs"],
                           item["reported"]["usd"]) for item in out["by_story"]])
        self.assertEqual([("worker", 6), ("reviewer", 6)],
                         [(item["role"], item["observed"]["launches"]) for item in out["by_role"]])
        self.assertEqual([("claude-code", "fake-model", 6), ("claude-code", "fake-review", 6)],
                         [(item["client"], item["model"], item["observed"]["launches"])
                          for item in out["by_client_model"]])
        # the scopes: one feature, one lease; the whole repository without a scope
        feature = out["by_story"][1]["feature_id"]
        one = self.bill_of("--feature", feature)
        self.assertEqual((4, feature, ["1-3-shout"]), (len(one["launches"]), one["scope"]["feature_id"],
                                                       sorted({row["story"] for row in one["launches"]})))
        lease = rows[-1]["lease_id"]
        leased = self.bill_of("--lease", lease)
        self.assertEqual((4, lease, out["by_story"][2]["feature_id"], 1),
                         (len(leased["launches"]), leased["scope"]["lease_id"], leased["scope"]["feature_id"],
                          leased["totals"]["observed"]["runs"]))
        self.assertEqual(12, len(self.bill_of()["launches"]))
        # a sealed return that changed reads unknown: its row keeps the launch's observed part and reports nothing
        changed = next(row for row in self.snapshot()["events"] if row["kind"] == "assignment.return-observed")
        path = self.base_dir / changed["evidence_ref"]
        path.chmod(0o600)
        with open(path, "ab") as out_file:
            out_file.write(b" ")
        after = self.bill_of("--epic", summary["epic_id"])
        broken = next(row for row in after["launches"] if row["assignment_id"] == changed["subject_id"])
        self.assertEqual(("unknown", None, "claude-code"), (broken["state"], broken["reported"], broken["client"]))
        self.assertEqual({"launches": 1, "usd": 1, "premium_requests": 12, "tokens": 1}, after["totals"]["unreported"])
        self.assertEqual(round(11 * FAKE_USD, 6), after["totals"]["reported"]["usd"])

    def test_codex_launches_report_tokens_and_no_money(self):
        code, summary, err = self.epic_start(code=0)
        self.assertEqual("epic-done", summary["reason"], (summary, err))
        out = self.bill_of("--epic", summary["epic_id"])
        self.assertEqual(12, len(out["launches"]))
        for row in out["launches"]:
            self.assertEqual(("codex-cli", None, None, CODEX_USAGE),
                             (row["client"], row["reported"]["usd"], row["reported"]["client_turns"],
                              row["reported"]["tokens"]))
        self.assertEqual({"usd": 0.0, "premium_requests": 0,
                          "tokens": {name: 12 * value for name, value in CODEX_USAGE.items()}},
                         out["totals"]["reported"])
        self.assertEqual({"launches": 0, "usd": 12, "premium_requests": 12, "tokens": 0}, out["totals"]["unreported"])

    def test_refusals_and_the_uninitialized_answer(self):
        self.assertEqual("NOT_FOUND", self.bill_of("--epic", "0" * 32, code=2)["error"])
        self.assertEqual("NOT_FOUND", self.bill_of("--feature", "0" * 32, code=2)["error"])
        self.assertEqual("NOT_FOUND", self.bill_of("--lease", "0" * 32, code=2)["error"])
        self.assertEqual("INVALID_INPUT", self.bill_of("--epic", "a", "--feature", "b", code=2)["error"])
        fresh = self.root / "fresh"
        fresh.mkdir()
        subprocess.run(["git", "init", "-q", str(fresh)], check=True)
        self.assertEqual({"state": "uninitialized"}, bill.command(resolve_repo(fresh)))
        self.assertEqual(0, len(self.bill_of()["launches"]))


ANSWER = {"schema": "coordinator-judgment-v3", "verdict": "answer", "deferred": [], "rationale": "P1 delegates",
          "refusal": "", "answers": [{"index": 1, "text": "keep", "clause": {
              "authority_id": "P1", "field": "authority", "pointer": "/delegated/0/text",
              "quote": "The coordinator commits; never ask whether"}}]}


class BoundTests(EpicDriveCase):
    """The money bound (bill design B5, B6) on fake clients: the fake Claude reports USD 0.0123 per launch."""

    def bounded_template(self, max_cost_usd, roles=CLAUDE_ROLES):
        budgets = {"max_updates": 3, "max_judgments": 8, "max_corrections": 3, "max_cost_usd": max_cost_usd}
        return self.write_template(**({"roles": roles} if roles else {}), budgets=budgets)

    def bill_of(self, *args, code=0):
        _, out, err = self.run_cli("bill", *args, code=code)
        return out if code == 0 else err

    def test_the_story_bound_stops_before_the_launch_that_would_exceed_it_and_a_restart_widens_it(self):
        template = self.bounded_template(0.02)   # the second launch reaches it; the third never starts
        data = self.epic_input(stories={"1-3-shout": {"budgets": {"max_cost_usd": 1}}})   # B5: a story's own box
        code, summary, err = self.epic_start(code=11, template=template, data=data)
        self.assertEqual(("needs-human", "cost-budget", "1-2-farewell"),
                         (summary["outcome"], summary["reason"], summary["story"]["key"]), (summary, err))
        subjects = summary["subjects"]
        self.assertEqual(("story", round(2 * FAKE_USD, 6), 0.02, 0, "impl", "worker"),
                         (subjects["scope"], subjects["used"], subjects["max"], subjects["unreported_launches"],
                          subjects["step_id"], subjects["role"]))
        self.assertIn("--budgets <json file>", summary["epic_action"])
        self.assertIn("the story's reported cost USD", summary["operator_action"])
        self.assertEqual(2, len(self.launch_log()))   # the spec author and its reviewer ran; the implementer did not
        code, again, _ = self.epic_resume(code=11)   # a plain resume stops at the same launch, launching nothing
        self.assertEqual(("cost-budget", 2), (again["reason"], len(self.launch_log())))
        budgets = self.root / "budgets.json"
        budgets.write_text(json.dumps({"max_cost_usd": 1}))
        _, _, err = self.epic_resume("--budgets", str(budgets), code=2)
        self.assertEqual("INVALID_INPUT", err["error"])
        code, resumed, err = self.epic_resume("--restart", "1-2-farewell", "--budgets", str(budgets), code=11)
        # 1-2 restarts under USD 1 and reaches done, 1-3 has its own box, 1-4 stops under the template's 0.02
        self.assertEqual(("cost-budget", "1-4-whisper", "story", 1),
                         (resumed["reason"], resumed["story"]["key"], resumed["subjects"]["scope"],
                          resumed["counts"]["stories_restarted"]), (resumed, err))
        self.assertEqual([("1-1-greeting", "already-done"), ("1-2-farewell", "done"), ("1-3-shout", "done"),
                          ("1-4-whisper", "current")], self.states(resumed))
        code, done, _ = self.epic_resume("--skip", "1-4-whisper", code=0)
        self.assertEqual(("epic-done", ["1-4-whisper"]), (done["reason"], done["subjects"]["skipped"]))
        out = self.bill_of("--epic", done["epic_id"])
        story = next(item for item in out["by_story"] if item["story"] == "1-2-farewell")
        # both chains of the story: the spec and its review before the stop, the implementation and its review after
        # the restart (the accepted spec stands); three runs: the start, the plain resume, the restart's run
        self.assertEqual((4, 3, round(4 * FAKE_USD, 6)),
                         (story["observed"]["launches"], story["observed"]["runs"], story["reported"]["usd"]))
        self.assertEqual([("1-2-farewell", 4), ("1-3-shout", 4), ("1-4-whisper", 2)],
                         [(item["story"], item["observed"]["launches"]) for item in out["by_story"]])
        self.assertEqual(round(10 * FAKE_USD, 6), done["bill"]["reported"]["usd"])

    def test_the_epic_bound_stops_across_stories_and_a_raise_continues(self):
        code, summary, err = self.epic_start(code=11, template=self.write_template(roles=CLAUDE_ROLES),
                                             data=self.epic_input(budgets={"max_cost_usd": 0.1}))
        # 4 + 4 + 1 launches report USD 0.1107: the stop comes before 1-4's spec review
        self.assertEqual(("cost-budget", "1-4-whisper", "epic", round(9 * FAKE_USD, 6), 0.1, "spec-review",
                          "reviewer", 0),
                         (summary["reason"], summary["story"]["key"], summary["subjects"]["scope"],
                          summary["subjects"]["used"], summary["subjects"]["max"], summary["subjects"]["step_id"],
                          summary["subjects"]["role"], summary["subjects"]["unreported_launches"]), (summary, err))
        self.assertIn("--max-cost-usd N", summary["epic_action"])
        self.assertNotIn("--restart", summary["epic_action"])
        self.assertIn("the epic's reported cost USD", summary["operator_action"])
        self.assertEqual(({"max_cost_usd": 0.1}, [], 9), (summary["budgets"], summary["interventions"],
                                                           len(self.launch_log())))
        code, raised, err = self.epic_resume("--max-cost-usd", "1", code=0)
        self.assertEqual("epic-done", raised["reason"], (raised, err))
        self.assertEqual(({"max_cost_usd": 1.0}, [("epic-budget-raised", 0.1, 1.0)], round(12 * FAKE_USD, 6)),
                         (raised["budgets"], [(item["type"], item["from"], item["to"])
                                              for item in raised["interventions"]],
                          raised["bill"]["reported"]["usd"]))
        status = self.run_cli("epic", "status", "--epic", raised["epic_id"], code=0)[1]["epics"][0]
        self.assertEqual(({"max_cost_usd": 1.0}, 1), (status["state"]["budgets"],
                                                      len(status["state"]["interventions"])))

    def test_a_skip_under_a_reached_epic_bound_stops_again_at_the_next_story(self):
        code, summary, _ = self.epic_start(code=11, template=self.write_template(roles=CLAUDE_ROLES),
                                           data=self.epic_input(budgets={"max_cost_usd": 0.05}))
        # four launches report 0.0492, the fifth (1-3's spec author) runs, the sixth stops
        self.assertEqual(("1-3-shout", "epic", round(5 * FAKE_USD, 6)),
                         (summary["story"]["key"], summary["subjects"]["scope"], summary["subjects"]["used"]))
        code, again, _ = self.epic_resume("--skip", "1-3-shout", code=11)
        self.assertEqual(("cost-budget", "1-4-whisper", "epic", "spec", "worker", 5),
                         (again["reason"], again["story"]["key"], again["subjects"]["scope"],
                          again["subjects"]["step_id"], again["subjects"]["role"], len(self.launch_log())))
        code, done, _ = self.epic_resume("--skip", "1-4-whisper", code=0)
        self.assertEqual(("epic-done", ["1-3-shout", "1-4-whisper"]), (done["reason"], done["subjects"]["skipped"]))

    def test_a_judgment_launch_is_checked_like_any_other(self):
        self.scenario(stories=self.story_scenarios(**{"1-2-farewell": {
            "spec-1": {"files": [[SPEC_PATH, SPEC_WITH_QUESTION]], "questions": ["Strip whitespace from the name?"],
                       "options": ["strip: callers get a trimmed name", "keep: the name is used exactly as given"],
                       "open_questions": Q1},
            "spec-2": {"files": [[SPEC_PATH, SPEC_READY]], "open_questions": []}}}),
            judgments={"default": {"payload": ANSWER}})
        code, summary, err = self.epic_start(code=11, template=self.bounded_template(0.01))
        # the spec author's launch reports 0.0123; the judgment would be the second launch
        self.assertEqual(("cost-budget", "judgment", "spec", "story", round(FAKE_USD, 6), 1),
                         (summary["reason"], summary["subjects"]["role"], summary["subjects"]["step_id"],
                          summary["subjects"]["scope"], summary["subjects"]["used"], len(self.launch_log())),
                         (summary, err))

    def test_refusals_and_the_unchanged_digest_without_a_bound(self):
        _, _, err = self.epic_start(code=2, template=self.bounded_template(0.5, roles=None))   # Codex everywhere
        self.assertIn("reports cost", err["message"])
        for bad in (0.123, -1, 10001, "1", True):
            _, _, err = self.epic_start(code=2, template=self.bounded_template(bad))
            self.assertEqual("INVALID_INPUT", err["error"], bad)
        _, _, err = self.epic_start(code=2, template=self.write_template(),   # the Codex template, no bound of its own
                                    data=self.epic_input(budgets={"max_cost_usd": 5}))
        self.assertIn("reports cost", err["message"])
        for bad in ({"max_cost_usd": 5, "other": 1}, {"other": 1}, {"max_cost_usd": 0.001}, []):
            _, _, err = self.epic_start(code=2, template=self.write_template(roles=CLAUDE_ROLES),
                                        data=self.epic_input(budgets=bad))
            self.assertEqual("INVALID_INPUT", err["error"], bad)
        _, _, err = self.epic_start(code=2, template=self.write_template(roles=CLAUDE_ROLES),
                                    data=self.epic_input(stories={"1-3-shout": {"budgets": {"max_cost_usd": -1}}}))
        self.assertEqual("INVALID_INPUT", err["error"])
        filled = drive_epic.fill_template(self.template(), "0" * 32, "1-2-farewell")
        self.assertNotIn("max_cost_usd", filled["budgets"])   # no default inserted: the digest stands
        widened = drive_epic.fill_template(self.template(roles=CLAUDE_ROLES), "0" * 32, "1-2-farewell",
                                           budgets={"max_cost_usd": 2})
        self.assertEqual((2, 3), (widened["budgets"]["max_cost_usd"], widened["budgets"]["max_updates"]))
