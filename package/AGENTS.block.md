<!-- bmad-coordinator:begin v1 -->
## Portable BMAD coordinator opt-in

For a substantial request, first inspect this repository and its installed BMAD instructions, then identify the intended outcome, authority, scope, exclusions, and a concrete Git base commit. Use the installed coordinator CLI at `.bmad-coordinator/run.py` to inspect state and record a mandate before assigning bounded work. A small direct request may be handled directly. The coordinator tracks state and evidence; its presence does not automatically launch workers or prove review.

If an assignment explicitly gives you `role: worker` or `role: reviewer`, follow that frozen assignment and its permitted paths. Do not perform top-level intake or claim coordinator ownership from inside that assignment. You may use bounded nested investigation or review when the mandate and run budget permit it. Return evidence and unresolved questions honestly; a self-report is not independent review or proof of effective permissions.

Use `python3 .bmad-coordinator/run.py --repo /absolute/worktree status` to inspect state. For a feature worktree, record the resolved base commit at creation and run `doctor --base COMMIT` before execution. A moving branch name is not a substitute for that concrete commit.
<!-- bmad-coordinator:end v1 -->
