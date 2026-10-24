"""
tools.py - Inspection tool implementations for the DevOps Auditing Agent.

Each tool interacts with a cloned local copy of the target repository and returns
structured results that the agent loop can interpret.
"""

import os
import re
import subprocess
import fnmatch
from pathlib import Path
from typing import Optional, Union


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: str, timeout: int = 30) -> tuple[str, str, int]:
    """Run a subprocess command and return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout}s: {' '.join(cmd)}", 1
    except FileNotFoundError as exc:
        return "", f"Command not found: {exc}", 1
    except Exception as exc:  # noqa: BLE001
        return "", f"Unexpected error: {exc}", 1


# ---------------------------------------------------------------------------
# Core inspection tools
# ---------------------------------------------------------------------------

def list_files(repo_path: str, max_depth: int = 6) -> dict:
    """
    Recursively list repository files up to *max_depth* levels deep.

    Returns a dict with keys:
        tree   - list of relative paths (strings)
        count  - total number of files found
        error  - error message if traversal failed, else None
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        return {"tree": [], "count": 0, "error": f"Directory not found: {repo_path}"}

    # Patterns to skip (mirrors a typical .gitignore)
    SKIP_DIRS = {
        ".git", "__pycache__", ".mypy_cache", ".pytest_cache",
        "node_modules", ".venv", "venv", "env", ".env",
        "dist", "build", ".tox", ".eggs",
        ".idea", ".vscode",
    }

    tree: list[str] = []
    try:
        for root, dirs, files in os.walk(repo_path):
            rel_root = Path(root).relative_to(repo_path)
            depth = len(rel_root.parts)
            if depth >= max_depth:
                dirs.clear()
                continue
            # Prune skip dirs in-place so os.walk won't recurse into them
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
            for fname in files:
                rel_path = str(rel_root / fname) if str(rel_root) != "." else fname
                tree.append(rel_path)
    except PermissionError as exc:
        return {"tree": tree, "count": len(tree), "error": str(exc)}

    return {"tree": sorted(tree), "count": len(tree), "error": None}


def read_file(repo_path: str, relative_path: str, max_bytes: int = 32_768) -> dict:
    """
    Read a file from the repository.

    Returns a dict with keys:
        content    - file contents as a string (truncated to *max_bytes*)
        truncated  - True if content was truncated
        size       - original file size in bytes
        error      - error message if read failed, else None
    """
    full_path = Path(repo_path) / relative_path
    if not full_path.exists():
        return {"content": None, "truncated": False, "size": 0,
                "error": f"File not found: {relative_path}"}
    if not full_path.is_file():
        return {"content": None, "truncated": False, "size": 0,
                "error": f"Path is not a file: {relative_path}"}

    size = full_path.stat().st_size
    try:
        raw = full_path.read_bytes()
        # Attempt UTF-8 decode; fall back to latin-1 for binary-ish files
        try:
            text = raw[:max_bytes].decode("utf-8")
        except UnicodeDecodeError:
            text = raw[:max_bytes].decode("latin-1")
        return {
            "content": text,
            "truncated": size > max_bytes,
            "size": size,
            "error": None,
        }
    except PermissionError as exc:
        return {"content": None, "truncated": False, "size": size, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"content": None, "truncated": False, "size": size, "error": str(exc)}


def search_code(repo_path: str, pattern: str, file_glob: str = "*",
                max_matches: int = 200) -> dict:
    """
    Search for a regex *pattern* across repository source files.

    Args:
        repo_path   - local path to the cloned repository
        pattern     - Python regex pattern to search for
        file_glob   - restrict search to files matching this glob (e.g. "*.py")
        max_matches - cap results to avoid flooding the context window

    Returns a dict with keys:
        matches  - list of {file, line, text} dicts
        total    - total number of matches found (may exceed max_matches)
        error    - error message or None
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        return {"matches": [], "total": 0, "error": f"Directory not found: {repo_path}"}

    SKIP_DIRS = {
        ".git", "__pycache__", ".mypy_cache", ".pytest_cache",
        "node_modules", ".venv", "venv", "env",
        "dist", "build", ".tox",
    }

    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return {"matches": [], "total": 0, "error": f"Invalid regex: {exc}"}

    matches: list[dict] = []
    total = 0

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if not fnmatch.fnmatch(fname, file_glob):
                continue
            fpath = Path(root) / fname
            rel = str(fpath.relative_to(repo_path))
            try:
                content = fpath.read_bytes()
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue  # skip binary files
                for lineno, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        total += 1
                        if len(matches) < max_matches:
                            matches.append({
                                "file": rel,
                                "line": lineno,
                                "text": line.rstrip(),
                            })
            except (PermissionError, OSError):
                continue

    return {"matches": matches, "total": total, "error": None}


def git_log(repo_path: str, max_entries: int = 50) -> dict:
    """
    Return the recent git commit history for the repository.

    Returns a dict with keys:
        log    - list of {hash, author, date, message} dicts
        count  - number of entries returned
        error  - error message or None
    """
    fmt = "%H|%an|%ae|%ad|%s"
    stdout, stderr, rc = _run(
        ["git", "log", f"--max-count={max_entries}", f"--pretty=format:{fmt}",
         "--date=short"],
        cwd=repo_path,
    )
    if rc != 0:
        return {"log": [], "count": 0, "error": stderr.strip() or "git log failed"}

    entries: list[dict] = []
    for line in stdout.strip().splitlines():
        parts = line.split("|", 4)
        if len(parts) == 5:
            entries.append({
                "hash": parts[0][:12],
                "author": parts[1],
                "email": parts[2],
                "date": parts[3],
                "message": parts[4],
            })
    return {"log": entries, "count": len(entries), "error": None}


# ---------------------------------------------------------------------------
# Optional / supplementary tools
# ---------------------------------------------------------------------------

def git_status(repo_path: str) -> dict:
    """
    Return the output of `git status --porcelain` to detect tracked generated
    files or uncommitted changes.
    """
    stdout, stderr, rc = _run(["git", "status", "--porcelain"], cwd=repo_path)
    if rc != 0:
        return {"status": [], "error": stderr.strip()}
    lines = [l for l in stdout.splitlines() if l.strip()]
    return {
        "status": [{"code": l[:2].strip(), "path": l[3:].strip()} for l in lines],
        "error": None,
    }


def list_github_actions(repo_path: str) -> dict:
    """
    Return all GitHub Actions workflow files found under .github/workflows/.
    """
    workflows_dir = Path(repo_path) / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return {"workflows": [], "error": "No .github/workflows directory found"}
    files = [str(f.relative_to(repo_path)) for f in workflows_dir.rglob("*.yml")]
    files += [str(f.relative_to(repo_path)) for f in workflows_dir.rglob("*.yaml")]
    return {"workflows": sorted(set(files)), "error": None}


def run_ruff(repo_path: str) -> dict:
    """
    Run the `ruff check` linter on the repository (if ruff is available).
    Returns a dict with linting results.
    """
    stdout, stderr, rc = _run(
        ["ruff", "check", "--output-format=json", "."],
        cwd=repo_path,
        timeout=60,
    )
    if rc not in (0, 1):  # ruff exits 1 when violations found
        return {"output": stdout, "error": stderr.strip() or "ruff failed"}
    return {"output": stdout, "error": None, "exit_code": rc}


def run_pytest(repo_path: str) -> dict:
    """
    Run pytest with a JSON report (if pytest is available in the environment).
    The repository's own test suite is executed read-only (no modifications).
    """
    stdout, stderr, rc = _run(
        ["python", "-m", "pytest", "--tb=short", "-q", "--no-header"],
        cwd=repo_path,
        timeout=120,
    )
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": rc,
        "error": None,
    }


def check_gitignore(repo_path: str) -> dict:
    """
    Read and summarise the .gitignore file, or report that it is missing.
    """
    result = read_file(repo_path, ".gitignore")
    if result["error"]:
        return {"present": False, "content": None, "error": result["error"]}
    return {"present": True, "content": result["content"], "error": None}


def get_directory_tree(repo_path: str) -> str:
    """Return a compact tree representation for display / debugging."""
    data = list_files(repo_path)
    if data["error"]:
        return f"Error: {data['error']}"
    lines = []
    for path in data["tree"]:
        depth = path.count(os.sep)
        indent = "  " * depth
        lines.append(f"{indent}{Path(path).name}")
    return "\n".join(lines)


def check_dockerfile(repo_path: str) -> dict:
    """
    Search for Dockerfiles and analyse them for common issues.
    Returns info about found Dockerfiles or reports their absence.
    """
    repo = Path(repo_path)
    dockerfiles = []

    # Common Dockerfile names and locations
    candidates = [
        "Dockerfile",
        "dockerfile",
        "Dockerfile.dev",
        "Dockerfile.prod",
        "Dockerfile.test",
        "docker/Dockerfile",
    ]

    for candidate in candidates:
        fpath = repo / candidate
        if fpath.is_file():
            dockerfiles.append(candidate)

    # Also search for any Dockerfile* recursively (up to 3 levels)
    for root, dirs, files in os.walk(repo_path):
        rel = Path(root).relative_to(repo_path)
        if len(rel.parts) > 3:
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", ".venv", "venv"}]
        for f in files:
            if f.lower().startswith("dockerfile"):
                full_rel = str(rel / f) if str(rel) != "." else f
                if full_rel not in dockerfiles:
                    dockerfiles.append(full_rel)

    if not dockerfiles:
        return {"found": False, "files": [], "error": None}

    results = []
    for df in dockerfiles:
        content = read_file(repo_path, df)
        results.append({
            "path": df,
            "content": content.get("content", ""),
            "size": content.get("size", 0),
        })

    return {"found": True, "files": results, "error": None}


def check_docker_compose(repo_path: str) -> dict:
    """
    Search for docker-compose files and return their contents.
    """
    repo = Path(repo_path)
    compose_names = [
        "docker-compose.yml", "docker-compose.yaml",
        "docker-compose.dev.yml", "docker-compose.prod.yml",
        "docker-compose.override.yml", "compose.yml", "compose.yaml",
    ]

    found = []
    for name in compose_names:
        fpath = repo / name
        if fpath.is_file():
            content = read_file(repo_path, name)
            found.append({
                "path": name,
                "content": content.get("content", ""),
            })

    if not found:
        return {"found": False, "files": [], "error": None}

    return {"found": True, "files": found, "error": None}


def check_pr_template(repo_path: str) -> dict:
    """
    Check for pull request templates and CODEOWNERS file.
    """
    repo = Path(repo_path)
    results = {
        "pr_template": False,
        "codeowners": False,
        "contributing": False,
        "files_found": [],
    }

    # PR template locations
    pr_locations = [
        ".github/pull_request_template.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "pull_request_template.md",
        "PULL_REQUEST_TEMPLATE.md",
        ".github/PULL_REQUEST_TEMPLATE/",
    ]

    for loc in pr_locations:
        if (repo / loc).exists():
            results["pr_template"] = True
            results["files_found"].append(loc)
            break

    # CODEOWNERS
    codeowners_locs = [
        "CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS",
    ]
    for loc in codeowners_locs:
        if (repo / loc).is_file():
            results["codeowners"] = True
            results["files_found"].append(loc)
            break

    # CONTRIBUTING
    for name in ["CONTRIBUTING.md", "CONTRIBUTING.rst", "contributing.md"]:
        if (repo / name).is_file():
            results["contributing"] = True
            results["files_found"].append(name)
            break

    return results
