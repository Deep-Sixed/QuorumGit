"""The application code carries no vendor or agent-provider identity —
the only brand is QuorumGit. Deployments coordinating specific agent
stacks can extend FORBIDDEN with their own legacy or internal names."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

FORBIDDEN = re.compile(
    r"openai|anthropic|claude|gemini|copilot", re.IGNORECASE
)


def test_no_foreign_branding_in_application_code():
    offenders = []
    for path in SRC.rglob("*"):
        if path.suffix not in {".py", ".sql"}:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if FORBIDDEN.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, "Forbidden identity strings:\n" + "\n".join(offenders)
