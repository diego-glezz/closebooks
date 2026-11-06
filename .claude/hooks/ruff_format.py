"""PostToolUse hook: ruff format + ruff check --fix on the .py file Claude just edited.
Never blocks: formatting problems are fixed silently, remaining lint errors are reported
back to Claude as context so it can fix them."""

import json
import subprocess
import sys

payload = json.load(sys.stdin)
tool_input = payload.get("tool_input") or {}
path = tool_input.get("file_path") or (payload.get("tool_response") or {}).get("filePath")
if not path or not str(path).endswith(".py"):
    sys.exit(0)

subprocess.run(["uv", "run", "--quiet", "ruff", "format", "--quiet", path], check=False)
lint = subprocess.run(
    ["uv", "run", "--quiet", "ruff", "check", "--fix", "--quiet", "--output-format=concise", path],
    capture_output=True,
    text=True,
    check=False,
)
if lint.returncode != 0 and lint.stdout.strip():
    context = f"ruff found issues it could not auto-fix in {path}:\n{lint.stdout[:2000]}"
    output = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}
    print(json.dumps(output))
