Golden Codex fixtures for the client-adapter slice (plan Task 1, design D4 and section 10)
=======================================================================================

What: the bytes a fake `codex` run produces with the unchanged baseline code, before any client-adapter
edit. They pin Codex byte compatibility: argv after argv[0], the planning brief, the planning binding,
`spec_sha256`, the generic launch and return key sets, and the receipt shapes. They prove coordinator
logic against the test fake only; they are not evidence about a live Codex CLI.

How they were captured (2026-10-05):
- Code: the worktree at HEAD 0fc87c4 with `coordinator/` clean. `git diff 322752a 0fc87c4` touches only
  coordinator/docs and coordinator/plans, so every code path, test helper and fake is byte-identical to
  322752a. The capture ran in that checkout before any source edit instead of a separate detached
  worktree. The full suite at that code: `Ran 273 tests ... OK`.
- Flows: the existing test harness (tests.test_bmad_planning.PlanningCase, which installs the
  tests.test_bmad_core FAKE as `codex` and the synthetic BMAD fixture of tests/bmad_fixture.py), with
  `uuid.uuid4` patched to a counter (UUID(int=1), UUID(int=2), ...) from the start of setUp, so ids and
  `spec_sha256` are deterministic. Then, in one test:
  1. generic: `assign()` (unbound workspace-write) and `run_it(..., FAKE_WRITE=[["result.md", "r"]])`;
  2. planning: `prep()` (bmad-prd create, slug gitops) and
     `execute(id, flow(), first_files())`.
  Both ended `completed-claim`.
- Saved per flow: launch.json, return.json, envelope.json, events.jsonl, last.json, return.schema.json
  from the run directory; argv-tail.json (launch argv[1:], indented); argv0.txt; launch-keys.json and
  return-keys.json (sorted key sets). Planning also: brief.md, brief_ref.txt, binding.json (the sealed
  binding file) and spec_sha256.txt (raw, from the binding).
- The capture ran twice in two different temp roots. After normalization every file was identical
  except digests over root-dependent bytes: return.json launch_sha256 (both flows), planning
  return.json last_sha256, planning launch.json binding_sha256 and envelope_sha256, and planning
  envelope.json initial_inventory_digest. Inventory digests (envelope initial_inventory_digest,
  return.json before/after, and launch envelope_sha256) also depend on the installed package bytes, so
  tests compare those keys by shape only.

Normalizer (applied to every saved file except spec_sha256.txt, argv0.txt and the key-set files):
- literal replacements, longest first: the temp root (as given and resolved) -> <ROOT>, the run
  directory name -> <RUN>, the assignment request fingerprint -> <FINGERPRINT>, and for planning the
  brief digest -> <BRIEF-SHA256>;
- every uuid -> <UUID:n>, n being its integer value (the counter value above);
- every ISO-8601 UTC timestamp -> <TIME>;
- the launch pid -> "<PID>" (a quoted placeholder also where the raw value was a JSON number, so the
  files stay valid JSON).
tests/fake_clients.py `golden_normalize` is this function; the full capture script follows verbatim.

---- capture script (run with PYTHONPATH=coordinator:<dir of this script>) ----
"""Golden Codex capture at the unchanged baseline code (HEAD 0fc87c4 == 322752a for all code paths).

Usage: PYTHONPATH=coordinator PYTHONDONTWRITEBYTECODE=1 python golden_capture.py OUT_DIR
"""
import itertools
import json
import re
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests.test_bmad_core import writes
from tests.test_bmad_planning import PlanningCase, flow

OUT = Path(sys.argv.pop(1))
UUID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TIME = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?\+00:00")


def normalize(data: bytes, literals: dict[str, str], pids=()) -> bytes:
    """Golden normalizer (copied verbatim into tests/fake_clients.py and README.txt)."""
    for literal, placeholder in sorted(literals.items(), key=lambda item: -len(item[0])):
        data = data.replace(literal.encode(), placeholder.encode())
    data = UUID.sub(lambda m: b"<UUID:%d>" % uuid.UUID(m.group().decode()).int, data)
    data = TIME.sub(b"<TIME>", data)
    for pid in pids:
        data = data.replace(b'"%d"' % pid, b'"<PID>"')
        data = re.sub(rb'(?<=:)%d(?=[,}])' % pid, b'"<PID>"', data)
    return data


RECEIPTS = ("launch.json", "return.json", "envelope.json", "events.jsonl", "last.json", "return.schema.json")


class Capture(PlanningCase):
    def setUp(self):
        counter = itertools.count(1)
        patcher = patch("uuid.uuid4", side_effect=lambda: uuid.UUID(int=next(counter)))
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def save(self, flow_name, run_dir, fingerprint, extra=None):
        out = OUT / flow_name
        out.mkdir(parents=True, exist_ok=True)
        launch = json.loads((run_dir / "launch.json").read_bytes())
        literals = {str(self.root.resolve()): "<ROOT>", str(self.root): "<ROOT>", run_dir.name: "<RUN>",
                    fingerprint: "<FINGERPRINT>"}
        literals.update(extra or {})
        pids = (int(launch["pid"]),)
        for name in RECEIPTS:
            (out / name).write_bytes(normalize((run_dir / name).read_bytes(), literals, pids))
        tail = json.dumps(launch["argv"][1:], indent=1).encode() + b"\n"
        (out / "argv-tail.json").write_bytes(normalize(tail, literals, pids))
        (out / "argv0.txt").write_text(launch["argv"][0] + "\n")
        terminal = json.loads((run_dir / "return.json").read_bytes())
        (out / "launch-keys.json").write_text(json.dumps(sorted(launch), indent=1) + "\n")
        (out / "return-keys.json").write_text(json.dumps(sorted(terminal), indent=1) + "\n")
        return literals, pids

    def test_capture(self):
        generic = self.assign()
        report = self.run_it(generic, FAKE_WRITE=writes(("result.md", "r")))
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        row = self.coordinator.detail("assignment", generic)
        self.save("generic", Path(report.run_dir), row["request_fingerprint"])

        prepared = self.prep()
        assignment = prepared["assignment_id"]
        row = self.coordinator.detail("assignment", assignment)
        brief_digest = next(item["digest"] for item in row["source_digests"] if item["path"] == prepared["brief_ref"])
        report = self.execute(assignment, flow(), self.first_files())
        self.assertEqual("completed-claim", report.state, report.evidence_needs)
        literals, pids = self.save("planning", Path(report.run_dir), row["request_fingerprint"],
                                   {brief_digest: "<BRIEF-SHA256>"})
        out = OUT / "planning"
        (out / "brief.md").write_bytes(normalize((self.t.path / prepared["brief_ref"]).read_bytes(), literals))
        binding_path = self.t.repo.common_dir / "coordinator" / "bindings" / (prepared["binding_sha256"] + ".json")
        binding = json.loads(binding_path.read_bytes())
        (out / "binding.json").write_bytes(normalize(binding_path.read_bytes(), literals))
        (out / "spec_sha256.txt").write_text(binding["spec_sha256"] + "\n")
        (out / "brief_ref.txt").write_text(prepared["brief_ref"] + "\n")


if __name__ == "__main__":
    unittest.main()

---- regeneration note (2026-10-05, post-smoke brief-contract fix) ----
Only planning/brief.md and the "brief" value inside planning/envelope.json were regenerated. The first live
Claude smoke showed the planning brief never stated the return rules the profile check enforces (result versus
BMAD status, the canonical unresolved_question, complete versus partial, the memlog requirement); S1 returned
`completed` with open questions and was refused. bmad_planning.RETURN_RULES now adds those rules, in the check's
own words, to the end of the Rules section of every planning brief for every client, so the Codex brief gains
exactly those five lines after its last rule. This is a
worker-facing contract fix, not drift. The regeneration re-ran the golden flow on the current code with the same
uuid counter and normalizer, asserted that every other envelope key is unchanged, and replaced only the brief
text. Every receipt fixture (launch, return, events, last, return.schema, binding, spec_sha256, argv) is
byte-identical to the 322752a capture.
Second regeneration (smoke attempt 2): S3 returned PRD requirement ids in `affected_ids`, which the service refuses.
bmad_planning.AFFECTED_IDS_RULE (only assignment ids of this feature, usually `[]`) is now the last rule, so the
Codex brief gains one more line; the same script and assertions were used and receipts stay byte-identical.
Third regeneration (generic-instruction round): the envelope `instructions` (worker.INSTRUCTIONS, used for every
assignment) now state the generic return contract and worker.AFFECTED_IDS, and the brief rule names decision ids in
its never-list. generic/envelope.json changed only in `instructions`, planning/envelope.json only in `brief` and
`instructions`, planning/brief.md only in that one rule line (asserted by the same scratch script); every receipt
stays byte-identical.
Fourth regeneration (instruction wording): worker.INSTRUCTIONS now says `changed_paths` holds created or modified
files only (deletion, rename, symlink or mode-only change is refused) and lists every protected class. Only the
`instructions` value of generic/envelope.json and planning/envelope.json changed; brief.md and every receipt are
unchanged.
Fifth regeneration (every refusal announced): the first planning rule now quotes "workflow_json is not JSON" and
"unknown BMAD status", and worker.INSTRUCTIONS quotes "Immutable source or contract bytes changed", says Git-ignored
or byte-identical files must not be listed and names `_bmad/`. Only planning/brief.md (that one rule line) and the
envelopes' `brief`/`instructions` values changed; every receipt is unchanged.
