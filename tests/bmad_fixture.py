"""Synthetic mini BMAD tree. It proves coordinator logic only, never live BMAD behavior."""

import csv
import hashlib
import io
from pathlib import Path

SKILLS = ("bmad-prd", "bmad-architecture", "bmad-review")
CLIENT = ".agents/skills"

PRD_SCHEMAS = '''# Headless Mode JSON Schemas
`status` is "complete", "blocked", or "partial"; "prd", "memlog", "open_questions", "assumptions",
"conflicts_with_prior_decisions".
'''
PRD_HEADLESS = '''# Headless Mode
Headless mode is in effect when the caller sets `headless: true`. Inputs include `doc_workspace`.
Update logs via `uv run {project-root}/_bmad/scripts/memlog.py append --workspace {doc_workspace}`.
'''
ARCH_HEADLESS = '''# Headless
Detect headless from `headless: true`. Payload carries `altitude`, `purpose`, `doc_workspace`.
Every write goes through `memlog.py`. End with JSON:
"status": "complete | partial | blocked", "spine", "memlog", "assumptions", "open_questions",
"conflicts_with_prior_decisions", "altitude", "purpose".
'''

# Stub resolvers: same CLI and JSON shape as the installed scripts, 3-layer merge (scalars override, arrays append).
MERGE = '''
import json, sys, tomllib
from pathlib import Path
def load(p):
    return tomllib.loads(p.read_text()) if p.is_file() else {}
def merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        return {**a, **{k: merge(a[k], v) if k in a else v for k, v in b.items()}}
    if isinstance(a, list) and isinstance(b, list):
        return a + b
    return b
def arg(name):
    return sys.argv[sys.argv.index(name) + 1]
'''
RESOLVE_CUSTOMIZATION = MERGE + '''
skill, root = Path(arg("--skill")), Path(arg("--project-root"))
custom = root / "_bmad" / "custom"
merged = {}
for layer in (skill / "customize.toml", custom / (skill.name + ".toml"), custom / (skill.name + ".user.toml")):
    merged = merge(merged, load(layer))
print(json.dumps({"workflow": merged["workflow"]}, indent=2))
'''
RESOLVE_CONFIG = MERGE + '''
bmad = Path(arg("--project-root")) / "_bmad"
merged = {}
for layer in (bmad / "config.toml", bmad / "config.user.toml", bmad / "custom" / "config.toml",
              bmad / "custom" / "config.user.toml"):
    merged = merge(merged, load(layer))
print(json.dumps(merged, indent=2))
'''
MEMLOG = '''
import argparse
argparse.ArgumentParser(description="memlog stub").parse_args()
'''

CONFIG = '''[core]
project_name = "fixture"
output_folder = "{project-root}/_bmad-output"

[modules.bmm]
planning_artifacts = "{project-root}/_bmad-output/planning-artifacts"
'''
USER_CONFIG = '[core]\nuser_name = "private-value"\n'


def customize(skill: str, extra: str = "") -> str:
    template = {"bmad-prd": 'prd_template = "assets/prd-template.md"\n',
                "bmad-architecture": 'spine_template = "assets/spine-template.md"\n'}.get(skill, "")
    return ('[workflow]\nactivation_steps_prepend = []\nactivation_steps_append = []\npersistent_facts = []\n'
            'on_complete = ""\nexternal_sources = []\nexternal_handoffs = []\n' + template +
            'doc_standards = ["skill:bmad-review lenses=structure,prose"]\n' + extra +
            ('[[workflow.lenses]]\ncode = "adversarial"\n[[workflow.lenses]]\ncode = "edge-case-hunter"\n'
             if skill == "bmad-review" else ""))


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_bmad(root: Path, client: str = CLIENT, shims: bool = True) -> None:
    """Create the synthetic tree; tests then mutate it to remove capabilities or add overrides."""
    skills = {"bmad-prd": {"references/headless.md": PRD_HEADLESS, "assets/headless-schemas.md": PRD_SCHEMAS,
                           "assets/prd-template.md": "# PRD template\n"},
              "bmad-architecture": {"references/headless.md": ARCH_HEADLESS,
                                    "assets/spine-template.md": "# Spine template\n"},
              "bmad-review": {"references/lens-adversarial.md": "# Adversarial\n"}}
    for skill, files in skills.items():
        write(root, f"{client}/{skill}/SKILL.md", f"---\nname: {skill}\ndescription: fixture\n---\n# {skill}\n")
        write(root, f"{client}/{skill}/customize.toml", customize(skill))
        for rel, text in files.items():
            write(root, f"{client}/{skill}/{rel}", text)
    write(root, "_bmad/scripts/resolve_customization.py", RESOLVE_CUSTOMIZATION)
    write(root, "_bmad/scripts/resolve_config.py", RESOLVE_CONFIG)
    write(root, "_bmad/scripts/memlog.py", MEMLOG)
    write(root, "_bmad/config.toml", CONFIG)
    write(root, "_bmad/config.user.toml", USER_CONFIG)
    write(root, "_bmad/custom/.gitignore", "*.user.toml\n")
    write(root, "_bmad/_config/manifest.yaml", "version: fixture\n")
    write(root, "_bmad/_config/bmad-help.csv", "a,b\n")
    rows = [["canonicalId", "name", "description", "module", "path"]] + [
        [skill, skill, "fixture", "core", f"_bmad/never/opened/{skill}/SKILL.md"] for skill in SKILLS]
    if shims:
        rows.append(["bmad-editorial-review", "bmad-editorial-review", "Deprecated — forwards to bmad-review",
                     "core", "_bmad/never/opened/shim/SKILL.md"])
    out = io.StringIO()
    csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(rows)
    write(root, "_bmad/_config/skill-manifest.csv", out.getvalue())
    manifest = [["type", "name", "module", "path", "hash"]]
    for skill in SKILLS:
        for path in sorted((root / client / skill).rglob("*")):
            if path.is_file():
                rel = path.relative_to(root / client / skill).as_posix()
                manifest.append(["md", path.stem, "core", f"core/{skill}/{rel}",
                                 hashlib.sha256(path.read_bytes()).hexdigest()])
    out = io.StringIO()
    csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(manifest)
    write(root, "_bmad/_config/files-manifest.csv", out.getvalue())
