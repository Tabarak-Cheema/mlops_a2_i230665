# DevOps Repository Auditing Agent

An AI-powered DevOps auditing agent that autonomously inspects GitHub repositories, identifies DevOps best-practice violations, and generates an evidence-based JSON report with actionable recommendations.

## Architecture

```
agent.py          ← CLI entry point & agent loop (Observe → Reason → Act)
tools.py          ← Inspection tool implementations
llm_client.py     ← LLM provider abstraction (Gemini / OpenAI / Anthropic)
tests/            ← Automated tests for tools and agent behaviour
```

### Agent Loop

```
┌──────────────────────────────────────────────────────────────┐
│                        Agent Loop                            │
│                                                              │
│  Clone Repo → [Observe → LLM Reason → Select Tool →         │
│                Execute Tool → Interpret Results] × N         │
│                          → Finish → Write Report             │
└──────────────────────────────────────────────────────────────┘
```

The LLM **dynamically selects** which inspection tool to call next based on previous observations. There is no fixed checklist – the model reasons about what to inspect and follows up with additional tool calls as needed.

### Available Tools

| Tool | Purpose |
|------|---------|
| `list_files()` | Discover repository files and structure |
| `read_file(path)` | Inspect source or configuration files |
| `search_code(pattern)` | Search for suspicious patterns via regex |
| `git_log()` | Inspect commit history |
| `git_status()` | Detect tracked generated files |
| `list_github_actions()` | Enumerate CI/CD workflow files |
| `run_ruff()` | Run the ruff linter (if available) |
| `check_gitignore()` | Inspect .gitignore configuration |
| `check_dockerfile()` | Find and analyse Dockerfiles |
| `check_docker_compose()` | Find and analyse docker-compose files |
| `check_pr_template()` | Check for PR templates, CODEOWNERS, CONTRIBUTING |

### Supported LLM Providers

- **Google Gemini** (default) – set `GEMINI_API_KEY`
- **OpenAI / compatible endpoint** – set `OPENAI_API_KEY`
- **Anthropic Claude** – set `ANTHROPIC_API_KEY`

---

## Installation

```bash
# 1. Clone this repository
git clone https://github.com/Tabarak-Cheema/mlops_a2_i230665.git
cd mlops_a2_i230665

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.example .env
# Edit .env and add your API key
```

---

## Configuration

Copy `.env.example` to `.env` and set at least one LLM API key:

```env
LLM_PROVIDER=gemini          # gemini | openai | anthropic
GEMINI_API_KEY=your-key-here
```

Optional overrides:

```env
LLM_MODEL=gemini-2.0-flash   # override the default model
OPENAI_BASE_URL=https://...  # use an OpenAI-compatible endpoint
```

---

## Usage

```bash
python agent.py --repo <github_repository_url> --output report.json
```

### Examples

```bash
# Audit a public repository (default Gemini provider)
python agent.py --repo https://github.com/example/demo-project --output report.json

# Use OpenAI instead
python agent.py --repo https://github.com/example/demo-project --output report.json \
  --provider openai --model gpt-4o-mini

# Keep the cloned repo for debugging
python agent.py --repo https://github.com/example/demo-project --output report.json \
  --keep-clone --verbose
```

### CLI Options

| Flag | Description |
|------|-------------|
| `--repo URL` | **Required.** Public GitHub repository URL |
| `--output FILE` | Output JSON path (default: `report.json`) |
| `--provider NAME` | LLM provider: `gemini`, `openai`, `anthropic` |
| `--model NAME` | LLM model name override |
| `--keep-clone` | Keep the cloned repo after the run |
| `--verbose` | Enable DEBUG logging |

---

## Output Format

The agent writes a JSON file conforming to this schema:

```json
{
  "repository": "https://github.com/example/demo-project",
  "violations": [
    {
      "rule_id": "SEC-001",
      "category": "security",
      "severity": "critical",
      "file": "config.py",
      "line": 17,
      "description": "Hardcoded API credential",
      "evidence": "Credential assigned directly in source code",
      "recommendation": "Use a secrets manager or environment variable"
    }
  ]
}
```

**Severity levels:** `critical` · `high` · `medium` · `low`  
**File / line:** `null` when not applicable

---

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage report
pytest tests/ -v --cov=. --cov-report=term-missing

# Run a specific test file
pytest tests/test_tools.py -v
```

---

## Inspection Areas

| Category | Examples |
|----------|---------|
| `repository_hygiene` | Missing README, bad .gitignore, tracked generated files |
| `dependency_management` | Missing lockfiles, unpinned versions, inconsistent specs |
| `code_quality` | No linting config, obvious maintainability issues |
| `testing` | No tests, failing tests, no CI test gate |
| `ci_cd` | Wrong triggers, ineffective gates, skipped validation |
| `security` | Exposed credentials, unsafe workflow permissions |
| `configuration` | Hardcoded paths, committed local settings |
| `collaboration` | Unsafe PR automation, missing review guidance |
| `build_release` | Missing build validation, risky deploy automation |

---

## Design Decisions

- **No orchestration frameworks** – The agent loop, tool dispatch, and LLM reasoning are implemented from scratch in Python as required by the assignment.
- **Read-only** – The agent never modifies the inspected repository. It clones to a temporary directory and discards it after the run.
- **Evidence-first** – Every violation is backed by literal evidence from tool outputs. The agent is instructed not to invent findings.
- **Multi-provider** – Supports Gemini, OpenAI, and Anthropic so the evaluator can use any available API key.

---

## Sample Report

See [`sample_report.json`](sample_report.json) for an example agent output on a real repository.
