"""
agent.py - CLI entry point for the DevOps Repository Auditing Agent.

Usage:
    python agent.py --repo <github_repository_url> --output report.json

The agent:
  1. Clones the target repository to a temporary directory.
  2. Runs an Observe→Reason→Act loop, letting the LLM select tools.
  3. Aggregates violations and writes a machine-readable JSON report.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import tools as _tools
from llm_client import get_llm_client, parse_json_response

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent")

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert DevOps auditing agent. Your job is to inspect a
GitHub repository for DevOps best-practice violations.

## Scope
Software engineering and DevOps ONLY. Do NOT report ML/data-science issues.

## Categories to inspect
- repository_hygiene: README, .gitignore, tracked generated files
- dependency_management: lockfiles, version pins, requirements
- code_quality: linting config, formatting, obvious maintainability issues
- testing: test presence, CI test gates
- ci_cd: GitHub Actions triggers, gates, workflow design
- security: hardcoded secrets, unsafe permissions, credential exposure
- configuration: hardcoded paths, committed local settings
- collaboration: PR automation, review guidance
- build_release: build validation, deployment automation

## Tool loop
You will receive observations and must respond with EXACTLY ONE JSON object per turn:

To call a tool:
{
  "action": "tool_call",
  "tool": "<tool_name>",
  "args": { ... }
}

To finish:
{
  "action": "finish",
  "violations": [
    {
      "rule_id": "CAT-NNN",
      "category": "<category>",
      "severity": "critical|high|medium|low",
      "file": "<relative_path or null>",
      "line": <integer or null>,
      "description": "<concise description>",
      "evidence": "<exact evidence from tool output, never reproduce secrets>",
      "recommendation": "<specific actionable fix>"
    }
  ]
}

## Rules
- Only report violations supported by evidence from tool outputs.
- Allowed severities: critical, high, medium, low.
- Use null for file/line when not applicable.
- Do NOT invent findings; do NOT reproduce secret values in evidence or description.
- Distinguish real violations from acceptable alternatives (e.g., pyproject.toml instead of requirements.txt is fine).
- Limit to genuine, distinct violations. No duplicate findings.
- Available tools: list_files, read_file, search_code, git_log, git_status, list_github_actions, run_ruff, check_gitignore, check_dockerfile, check_docker_compose, check_pr_template

## Investigation Strategy
1. Start with list_files to understand the repo structure
2. Check for README.md, .gitignore, dependency files, test directories
3. Read relevant config files (CI workflows, Dockerfiles, pyproject.toml, etc.)
4. Search for security issues: hardcoded secrets, API keys, passwords, tokens
5. Inspect GitHub Actions workflows for quality gates and security
6. Check for linting/formatting configuration
7. Look for configuration issues (hardcoded paths, local settings)
8. Review collaboration setup (PR templates, CODEOWNERS)
9. Check build and release automation

## Rule ID Prefixes
- HYG: repository hygiene
- DEP: dependency management
- CQ: code quality
- TEST: testing
- CI: CI/CD
- SEC: security
- CFG: configuration
- COLLAB: collaboration
- BLD: build and release

## IMPORTANT
- Do NOT treat an absent optional file as a defect when equivalent solutions exist.
  For example: poetry.lock vs requirements.txt, setup.cfg vs pyproject.toml are alternatives.
- Do NOT infer remote branch protection settings from repository files.
- Do NOT flag docker-compose.yml environment variables as hardcoded secrets if they
  are clearly development/example defaults.
- Be thorough: make multiple tool calls to investigate different aspects.
- Prefer precision over recall: only report clear violations with evidence.
"""

TOOL_SCHEMAS = {
    "list_files": {
        "description": "List all repository files (up to 6 levels deep).",
        "args": {"repo_path": "string (always pass the repo_path variable)"},
    },
    "read_file": {
        "description": "Read the contents of a file in the repository.",
        "args": {
            "repo_path": "string",
            "relative_path": "string – path relative to repo root",
        },
    },
    "search_code": {
        "description": "Search for a regex pattern across repository files.",
        "args": {
            "repo_path": "string",
            "pattern": "string – Python regex",
            "file_glob": "string – optional glob filter (e.g. '*.py')",
        },
    },
    "git_log": {
        "description": "Retrieve recent git commit history.",
        "args": {
            "repo_path": "string",
            "max_entries": "integer (default 50)",
        },
    },
    "git_status": {
        "description": "Show tracked/untracked changes (git status --porcelain).",
        "args": {"repo_path": "string"},
    },
    "list_github_actions": {
        "description": "List all GitHub Actions workflow files.",
        "args": {"repo_path": "string"},
    },
    "run_ruff": {
        "description": "Run the ruff linter on Python files (if ruff is available).",
        "args": {"repo_path": "string"},
    },
    "check_gitignore": {
        "description": "Read and summarise the .gitignore file.",
        "args": {"repo_path": "string"},
    },
    "check_dockerfile": {
        "description": "Find and analyse Dockerfiles in the repository.",
        "args": {"repo_path": "string"},
    },
    "check_docker_compose": {
        "description": "Find and analyse docker-compose files.",
        "args": {"repo_path": "string"},
    },
    "check_pr_template": {
        "description": "Check for PR templates, CODEOWNERS, and CONTRIBUTING files.",
        "args": {"repo_path": "string"},
    },
}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

TOOL_MAP = {
    "list_files": _tools.list_files,
    "read_file": _tools.read_file,
    "search_code": _tools.search_code,
    "git_log": _tools.git_log,
    "git_status": _tools.git_status,
    "list_github_actions": _tools.list_github_actions,
    "run_ruff": _tools.run_ruff,
    "check_gitignore": _tools.check_gitignore,
    "check_dockerfile": _tools.check_dockerfile,
    "check_docker_compose": _tools.check_docker_compose,
    "check_pr_template": _tools.check_pr_template,
}


def _dispatch_tool(tool_name: str, args: dict, repo_path: str) -> Any:
    """Execute a tool call and return its result (always a JSON-serialisable dict)."""
    if tool_name not in TOOL_MAP:
        return {"error": f"Unknown tool: {tool_name}"}
    fn = TOOL_MAP[tool_name]
    # Inject repo_path if the tool accepts it and it wasn't supplied
    args = dict(args)
    if "repo_path" not in args:
        args["repo_path"] = repo_path
    try:
        return fn(**args)
    except TypeError as exc:
        return {"error": f"Tool call error ({tool_name}): {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Tool runtime error ({tool_name}): {exc}"}


# ---------------------------------------------------------------------------
# Repository cloning
# ---------------------------------------------------------------------------

def clone_repo(url: str, target_dir: str) -> None:
    """Clone a public GitHub repository (read-only, no credentials stored)."""
    log.info("Cloning %s → %s", url, target_dir)
    result = subprocess.run(
        ["git", "clone", "--depth=50", url, target_dir],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to clone repository:\n{result.stderr.strip()}"
        )
    log.info("Clone complete.")


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

MAX_TURNS = 30  # safety cap


def run_agent(repo_url: str, repo_path: str) -> list[dict]:
    """
    Run the Observe→Reason→Act agent loop.
    Returns the final list of violation dicts.
    """
    client = get_llm_client()

    tool_schema_text = json.dumps(TOOL_SCHEMAS, indent=2)

    initial_user_message = f"""
Repository URL  : {repo_url}
Local repo path : {repo_path}

Available tools (use repo_path="{repo_path}" in every tool call):
{tool_schema_text}

## Recommended Investigation Plan
1. list_files → understand overall structure
2. check_gitignore → verify .gitignore covers common patterns
3. read_file for README.md → check documentation exists and is adequate
4. Read dependency files (requirements.txt, pyproject.toml, setup.py, Pipfile, package.json)
5. list_github_actions → enumerate CI workflows
6. Read each workflow file → check triggers, gates, permissions
7. search_code for secrets → patterns like API_KEY, PASSWORD, SECRET, TOKEN, hardcoded credentials
8. search_code for hardcoded paths → patterns like /home/, /Users/, C:\\\\, absolute paths
9. check_dockerfile → if Docker is used
10. check_docker_compose → if compose is used
11. check_pr_template → collaboration files
12. Read any config files for sensitive data or local settings
13. Look for test directories and assess coverage

Start with list_files to understand the repo structure.
"""

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_user_message},
    ]

    for turn in range(1, MAX_TURNS + 1):
        log.info("Agent turn %d/%d – calling LLM …", turn, MAX_TURNS)

        # Retry LLM calls up to 3 times with exponential backoff
        raw_response = None
        last_error = None
        for attempt in range(1, 4):
            try:
                raw_response = client.chat(messages)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 3:
                    wait = 2 ** attempt
                    log.warning(
                        "LLM call failed (attempt %d/3): %s – retrying in %ds",
                        attempt, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    log.error("LLM call failed after 3 attempts: %s", exc)
                    raise

        if raw_response is None:
            raise RuntimeError(f"LLM call failed: {last_error}")

        log.debug("LLM response:\n%s", raw_response)

        # Parse the JSON decision
        try:
            decision = parse_json_response(raw_response)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("Could not parse LLM response as JSON: %s", exc)
            # Feed the error back and ask for a proper response
            messages.append({"role": "assistant", "content": raw_response})
            messages.append({
                "role": "user",
                "content": (
                    f"Your response was not valid JSON. Error: {exc}\n"
                    "Please respond with EXACTLY ONE JSON object as described."
                ),
            })
            continue

        action = decision.get("action")

        if action == "finish":
            violations = decision.get("violations", [])
            log.info("Agent finished with %d violation(s).", len(violations))
            return violations

        if action == "tool_call":
            tool_name = decision.get("tool")
            tool_args = decision.get("args", {})
            log.info("Tool call: %s(%s)", tool_name, _fmt_args(tool_args))

            tool_result = _dispatch_tool(tool_name, tool_args, repo_path)

            # Summarise large results to keep context manageable
            result_text = _summarise_result(tool_name, tool_result)
            log.info("Tool result length: %d chars", len(result_text))

            # Append assistant decision and tool result to history
            messages.append({"role": "assistant", "content": raw_response})
            messages.append({
                "role": "user",
                "content": f"Tool result for {tool_name}:\n{result_text}",
            })
        else:
            log.warning("Unexpected action: %s", action)
            messages.append({"role": "assistant", "content": raw_response})
            messages.append({
                "role": "user",
                "content": (
                    "Unexpected action type. Respond with either "
                    '{"action":"tool_call",...} or {"action":"finish",...}.'
                ),
            })

    log.warning("Reached max turns (%d) without a finish action.", MAX_TURNS)
    return []


def _fmt_args(args: dict) -> str:
    """Format tool args for log display (truncate long values)."""
    parts = []
    for k, v in args.items():
        v_str = str(v)
        if len(v_str) > 60:
            v_str = v_str[:57] + "..."
        parts.append(f"{k}={v_str!r}")
    return ", ".join(parts)


def _summarise_result(tool_name: str, result: Any, max_chars: int = 6000) -> str:
    """Serialise tool result to string, truncating if necessary."""
    try:
        text = json.dumps(result, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(result)

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated – {len(text) - max_chars} chars omitted]"
    return text


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_report(repo_url: str, violations: list[dict]) -> dict:
    """Validate and assemble the final JSON report."""
    REQUIRED_FIELDS = {
        "rule_id", "category", "severity", "file", "line",
        "description", "evidence", "recommendation",
    }
    VALID_SEVERITIES = {"critical", "high", "medium", "low"}

    clean_violations: list[dict] = []
    for v in violations:
        if not isinstance(v, dict):
            log.warning("Skipping non-dict violation: %r", v)
            continue
        missing = REQUIRED_FIELDS - v.keys()
        if missing:
            log.warning("Violation missing fields %s – skipping: %s", missing, v)
            continue
        if v.get("severity") not in VALID_SEVERITIES:
            log.warning("Invalid severity %r – defaulting to 'medium'", v.get("severity"))
            v["severity"] = "medium"
        # Ensure file and line are null when not applicable (not empty string)
        if v.get("file") == "":
            v["file"] = None
        if v.get("line") == "" or v.get("line") == 0:
            v["line"] = None
        clean_violations.append(v)

    return {
        "repository": repo_url,
        "violations": clean_violations,
    }


def write_report(report: dict, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Report written to %s", output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="DevOps Repository Auditing Agent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--repo",
        required=True,
        metavar="URL",
        help="Public GitHub repository URL to audit.",
    )
    parser.add_argument(
        "--output",
        default="report.json",
        metavar="FILE",
        help="Path to write the JSON report.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="LLM provider override: gemini | openai | anthropic",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="LLM model name override.",
    )
    parser.add_argument(
        "--keep-clone",
        action="store_true",
        help="Keep the cloned repo after the run (for debugging).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Provider / model overrides
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    if args.model:
        os.environ["LLM_MODEL"] = args.model

    # Validate repo URL
    repo_url = args.repo.strip()
    if not repo_url.startswith(("https://github.com/", "http://github.com/")):
        log.error("Only public GitHub URLs are supported (https://github.com/...).")
        sys.exit(1)

    # Clone to a temp directory
    tmp_dir = tempfile.mkdtemp(prefix="devops_audit_")
    repo_path = os.path.join(tmp_dir, "repo")

    try:
        clone_repo(repo_url, repo_path)
        violations = run_agent(repo_url, repo_path)
        report = build_report(repo_url, violations)
        write_report(report, args.output)
        print(f"\n[+] Audit complete. Report saved to: {args.output}")
        print(f"  Violations found: {len(report['violations'])}")
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected error: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        if not args.keep_clone and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
