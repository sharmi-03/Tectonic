"""Tools to scaffold, format, validate and write runnable Google ADK Python projects to disk."""

import ast
import importlib
import json
import os
from pathlib import Path
from typing import Optional

from google.adk.tools import ToolContext

from ...shared_libraries.constants import (
    STATE_VERIFICATION,
    STATE_ARCH_SPEC,
    OUTPUT_DIR,
    STATE_AGENT_CARDS,
    STATE_GENERATED_FILES,
    STATE_GUARDRAIL_REPORT,
)
from ...shared_libraries.types import parse_json_loose, slugify_identifier
from ...tools.file_manager import forbidden_reason, project_dir, read_registry, write_file
from ..guardrails.tools import scan_python_source

# --- Templates for the generated project --------------------------------------
_GEN_CONSTANTS = '''"""Shared constants for this generated agent project (single source of truth for the model and paths)."""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.adk.models.lite_llm import LiteLlm

os.environ["ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS"] = "true"

# Define project layout and dedicated local data storage
PACKAGE_DIR = Path(__file__).resolve().parent.parent # The inner <project> package directory
PROJECT_DIR = PACKAGE_DIR.parent                     # The agent root directory (generated_agents/<project>)
DATA_DIR = PROJECT_DIR / "data"                      # Dedicated local data directory for databases/files
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / f"{PROJECT_DIR.name}.db"        # Default SQLite database path strictly inside this agent's folder

load_dotenv(PROJECT_DIR / ".env")
load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
if GOOGLE_API_KEY:
    os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY
    os.environ["GEMINI_API_KEY"] = GOOGLE_API_KEY

import litellm

# Resilience against temporary high-demand spikes
litellm.num_retries = 5
litellm.request_timeout = 90

RAW_MODEL = os.getenv("GEMINI_MODEL", os.getenv("MODEL_NAME", "gemini-3.6-flash"))


def resolve_model(name: str) -> str:
    clean = (name or "gemini-3.6-flash").strip()
    if "/" in clean:
        return clean
    if clean.startswith("gemini"):
        return f"gemini/{clean}"
    return clean


MODEL_NAME = resolve_model(RAW_MODEL)
_kwargs = {"model": MODEL_NAME, "num_retries": 5}
if GOOGLE_API_KEY and "gemini" in MODEL_NAME:
    _kwargs["api_key"] = GOOGLE_API_KEY

MODEL = LiteLlm(**_kwargs)


def render_instruction(template, state_keys=()):
    """Return an instruction provider that substitutes {key} placeholders from session state."""

    def provider(context):
        text = template
        for key in state_keys:
            value = context.state.get(key)
            if value is None:
                value = ""
            elif not isinstance(value, str):
                value = str(value)
            text = text.replace("{" + key + "}", value)
        return text

    return provider
'''

_GEN_CORE_MODULES = '''"""Shared Tectonic platform modules for this project's agents.

Inside the Tectonic agent server every agent gets the platform as ADK tools (guardrails, intent,
short/long-term memory, tool orchestration, Agentic RAG knowledge, FinOps spend, PromptOps feedback,
audit trail) and its instruction is served by PromptOps as '<team>.<agent>' (versioned, A/B-testable).
Run standalone (plain `adk run`), the import fails and the agents work with their static prompts.
"""

TEAM = "{project}"
# Platform modules enabled in the approved Agent Architecture Specification (None: all modules).
ENABLED_MODULES = {enabled_modules}


def _fill(text, context, state_keys):
    for key in state_keys:
        value = context.state.get(key)
        text = text.replace("{" + key + "}", "" if value is None else (value if isinstance(value, str) else str(value)))
    return text


try:
    from tectonic_runtime.adk_tools import CORE_MODULE_INSTRUCTIONS, worker_tools

    CORE_MODULE_TOOLS = worker_tools(team=TEAM, modules=ENABLED_MODULES)

    def managed_instruction(agent_name, template, state_keys=()):
        """Instruction provider served by PromptOps ('<team>.<agent>'), falling back to the template."""

        async def provider(context):
            text = template
            try:
                from tectonic_runtime import get_runtime
                from tectonic_runtime.identity import session_id_of

                text, _ = await get_runtime().module("promptops").resolve_template(
                    f"{TEAM}.{agent_name}", session_id_of(context), template
                )
            except Exception:
                pass
            return _fill(text, context, state_keys)

        return provider

except ImportError:
    CORE_MODULE_TOOLS = []
    CORE_MODULE_INSTRUCTIONS = ""

    def managed_instruction(agent_name, template, state_keys=()):
        def provider(context):
            return _fill(template, context, state_keys)

        return provider
'''

_GEN_ENV = """# Configure Google Gemini API Key
GOOGLE_API_KEY=
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.6-flash
"""

_BASE_REQUIREMENTS = ["google-adk>=1.0.0", "google-genai>=0.1.0", "litellm>=1.50.0", "python-dotenv>=1.0.0"]


# --- Helpers ------------------------------------------------------------------
def _gate(tool_context: ToolContext, stages: tuple = ("design", "code")) -> Optional[dict]:
    """Refuse to write code unless the latest guardrail verdict approved the design or code."""
    report = tool_context.state.get(STATE_GUARDRAIL_REPORT)
    if not isinstance(report, dict) or not report.get("approved") or report.get("stage") not in stages:
        return {
            "status": "error",
            "error": "Blocked by guardrails: run the guardrails agent at stage 'design' and get an approval before writing code.",
        }
    return None


def _load_cards(project: str, tool_context: ToolContext) -> dict:
    store = tool_context.state.get(STATE_AGENT_CARDS) or {}
    return store.get(project) or read_registry(project)


def _record(tool_context: ToolContext, rel_path: str) -> None:
    files = list(tool_context.state.get(STATE_GENERATED_FILES) or [])
    if rel_path not in files:
        files.append(rel_path)
    tool_context.state[STATE_GENERATED_FILES] = files


def _docstring_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _prompt_py(card: dict) -> str:
    reads = [str(k) for k in card.get("state_reads", [])]
    return (
        f'"""Instruction for the {card["name"]} agent."""\n\n'
        f"STATE_READS = {json.dumps(reads)}\n\n"
        f'INSTRUCTION = """{_docstring_text(card.get("instruction", "").strip())}\n"""\n'
    )


def _tool_import_lines(card: dict, prefix: str) -> tuple:
    by_module: dict = {}
    for tool in card.get("tools", []):
        by_module.setdefault(tool["module"], []).append(tool["name"])
    lines = [f"from {prefix}tools.{module} import {', '.join(sorted(set(names)))}" for module, names in sorted(by_module.items())]
    tool_names = [t["name"] for t in card.get("tools", [])]
    return lines, tool_names


def _agent_py(card: dict, cards: dict, is_root: bool) -> str:
    name = card["name"]
    var = "root_agent" if is_root else name
    prefix = "." if is_root else "..."
    kind = card.get("agent_type", "llm")
    children = card.get("sub_agents", [])
    imports = []
    classes = {"llm": "LlmAgent", "sequential": "SequentialAgent", "parallel": "ParallelAgent", "loop": "LoopAgent"}
    imports.append(f"from google.adk.agents import {classes[kind]}")
    if kind == "llm":
        imports.append(f"from {prefix}shared_libraries.constants import MODEL, render_instruction" if is_root else "from ...shared_libraries.constants import MODEL, render_instruction")
        imports.append(f"from {prefix}shared_libraries.core_modules import CORE_MODULE_INSTRUCTIONS, CORE_MODULE_TOOLS, managed_instruction")
        imports.append("from . import prompt" if not is_root else "from . import prompt")
    tool_lines, tool_names = _tool_import_lines(card, prefix)
    if kind == "llm":
        imports.extend(tool_lines)
    for child in children:
        if is_root:
            imports.append(f"from .sub_agents.{child}.agent import {child}")
        else:
            imports.append(f"from ..{child}.agent import {child}")
    sub_list = ", ".join(children)
    description = json.dumps(card.get("description", ""))
    body = [f"{var} = {classes[kind]}(", f'    name="{name}",', f"    description={description},"]
    if kind == "llm":
        body.append("    model=MODEL,")
        body.append(f'    instruction=managed_instruction("{name}", prompt.INSTRUCTION + CORE_MODULE_INSTRUCTIONS, prompt.STATE_READS),')
        body.append(f"    tools=[{''.join(n + ', ' for n in tool_names)}*CORE_MODULE_TOOLS],")
        if card.get("output_key"):
            body.append(f"    output_key={json.dumps(card['output_key'])},")
    if kind == "loop":
        body.append(f"    max_iterations={int(card.get('max_iterations', 3))},")
    if children:
        body.append(f"    sub_agents=[{sub_list}],")
    body.append(")")
    header = f'"""{kind.capitalize()} agent definition: {name}."""\n\n'
    return header + "\n".join(imports) + "\n\n\n" + "\n".join(body) + "\n"


def _format(code: str) -> str:
    try:
        black = importlib.import_module("black")
        return black.format_str(code, mode=black.Mode(line_length=100))
    except Exception:  # black missing or the code is not parseable
        return "\n".join(line.rstrip() for line in code.splitlines()).rstrip() + "\n"


def canonicalize_project_path(project: str, raw_path: str) -> str:
    """Normalize any relative path inside a project to be strictly relative to project_dir(project).

    Standard generated project layout:
      <project>/requirements.txt
      <project>/.env
      <project>/.env.example
      <project>/data/
      <project>/<project>/__init__.py
      <project>/<project>/agent.py
      <project>/<project>/prompt.py
      <project>/<project>/shared_libraries/...
      <project>/<project>/tools/<module>.py
      <project>/<project>/sub_agents/<sub_agent>/...
    """
    clean = raw_path.replace("\\", "/").strip().lstrip("/")
    parts = [p for p in clean.split("/") if p]

    # Strip any redundant leading project name repetitions
    while parts and parts[0] == project:
        parts.pop(0)

    if not parts:
        return f"{project}/__init__.py"

    first = parts[0]
    # Root-level files and folders of the generated project directory:
    if first in ("requirements.txt", ".env", ".env.example", "README.md", "data") or (len(parts) == 1 and first.startswith(".env")):
        return "/".join(parts)

    # All Python code modules belong strictly inside the inner package folder: <project>/...
    return f"{project}/" + "/".join(parts)


def _write(project: str, rel_path: str, content: str, tool_context: ToolContext) -> dict:
    canonical_rel = canonicalize_project_path(project, rel_path)
    result = write_file(f"{project}/{canonical_rel}", content)
    if result.get("status") == "success":
        _record(tool_context, f"{project}/{canonical_rel}")
    return result


# --- ADK tools ------------------------------------------------------------------
def scaffold_project(project_name: str, extra_requirements_json: str, tool_context: ToolContext) -> dict:
    """Generate the ADK package skeleton from the approved Agent Cards.

    Writes requirements.txt, .env.example, the package __init__.py, root agent.py/prompt.py,
    shared_libraries/constants.py (with DATA_DIR and DB_PATH), every sub_agents/<name>/ package,
    the dedicated data/ storage folder, and tools/ package. Tool implementations are written
    with write_project_file.

    Args:
        project_name: snake_case project name (must have agent cards).
        extra_requirements_json: JSON list of additional pip requirements, e.g. ["requests>=2.31"]. Use "[]" for none.
        tool_context: Injected by ADK.

    Returns:
        dict with status, the files written, and the tool modules that still need implementations.
    """
    blocked = _gate(tool_context)
    if blocked:
        return blocked
    project = slugify_identifier(project_name, default="generated_agent")
    cards = _load_cards(project, tool_context)
    roots = [c for c in cards.values() if c.get("role") == "root"]
    if len(roots) != 1:
        return {"status": "error", "error": f"Expected exactly one root card, found {len(roots)}."}
    try:
        extras = parse_json_loose(extra_requirements_json or "[]")
        if not isinstance(extras, list):
            raise ValueError("must be a JSON list")
    except (ValueError, json.JSONDecodeError) as exc:
        return {"status": "error", "error": f"extra_requirements_json invalid: {exc}"}

    written, failures = [], []

    def emit(rel_path: str, content: str) -> None:
        result = _write(project, rel_path, content, tool_context)
        (written if result["status"] == "success" else failures).append(rel_path if result["status"] == "success" else result)

    root = roots[0]
    configured_model = root.get("model", "gemini-3.6-flash") or "gemini-3.6-flash"
    master_key = os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")

    env_example = f"""# Google Gemini Configuration
GOOGLE_API_KEY=
GEMINI_API_KEY=
GEMINI_MODEL={configured_model}
"""
    env_actual = f"""# Google Gemini Configuration for {project}
GOOGLE_API_KEY={master_key}
GEMINI_API_KEY={master_key}
GEMINI_MODEL={configured_model}
"""
    requirements = sorted(set(_BASE_REQUIREMENTS + [str(e) for e in extras]))
    emit("requirements.txt", "\n".join(requirements) + "\n")
    emit(".env.example", env_example)
    emit(".env", env_actual)
    emit("data/.gitkeep", "")
    emit(f"{project}/__init__.py", '"""Generated Google ADK agent package."""\n\nfrom . import agent\n')
    emit(f"{project}/shared_libraries/__init__.py", "")
    emit(f"{project}/shared_libraries/constants.py", _GEN_CONSTANTS)
    arch = tool_context.state.get(STATE_ARCH_SPEC) or {}
    enabled = arch.get("enabled_modules") if isinstance(arch, dict) and arch.get("project") == project else None
    emit(f"{project}/shared_libraries/core_modules.py",
         _GEN_CORE_MODULES.replace("{project}", project).replace("{enabled_modules}", repr(sorted(enabled)) if enabled else "None"))
    emit(f"{project}/tools/__init__.py", '"""Tool implementations for the generated agents."""\n')
    emit(f"{project}/sub_agents/__init__.py", "")
    root = roots[0]
    emit(f"{project}/agent.py", _format(_agent_py(root, cards, True)))
    if root.get("agent_type", "llm") == "llm":
        emit(f"{project}/prompt.py", _format(_prompt_py(root)))
    pending_modules: dict = {}
    for name, card in sorted(cards.items()):
        if card.get("role") == "root":
            for tool in card.get("tools", []):
                pending_modules.setdefault(tool["module"], set()).add(tool["name"])
            continue
        base = f"{project}/sub_agents/{name}"
        emit(f"{base}/__init__.py", "")
        emit(f"{base}/agent.py", _format(_agent_py(card, cards, False)))
        if card.get("agent_type", "llm") == "llm":
            emit(f"{base}/prompt.py", _format(_prompt_py(card)))
        for tool in card.get("tools", []):
            pending_modules.setdefault(tool["module"], set()).add(tool["name"])
    specs = {}
    for card in cards.values():
        for tool in card.get("tools", []):
            specs[(tool["module"], tool["name"])] = tool
    return {
        "status": "success" if not failures else "partial",
        "project_dir": f"{project}/",
        "run_from": str(project_dir(project)),
        "files_written": written,
        "failures": failures,
        "tool_modules_to_implement": {
            f"{project}/tools/{module}.py": [specs[(module, n)] for n in sorted(names)] for module, names in sorted(pending_modules.items())
        },
    }


def format_python_code(code: str) -> dict:
    """Format Python source with black (falls back to trimming whitespace if black is unavailable).

    Args:
        code: Python source text.

    Returns:
        dict with status and the formatted code.
    """
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return {"status": "error", "error": f"Syntax error line {exc.lineno}: {exc.msg}"}
    return {"status": "success", "code": _format(code)}


def validate_python_syntax(code: str, expect_root_agent: bool = False) -> dict:
    """Check that Python source parses and passes the static safety scan.

    Args:
        code: Python source text.
        expect_root_agent: True for an agent.py that must define root_agent.

    Returns:
        dict with "valid" and the list of blocking findings.
    """
    findings = scan_python_source(code, "candidate.py", expect_root_agent)
    blockers = [f for f in findings if f["severity"] == "blocker"]
    return {"status": "success", "valid": not blockers, "blockers": blockers, "warnings": [f for f in findings if f["severity"] == "warning"]}


def write_project_file(project_name: str, relative_path: str, content: str, tool_context: ToolContext) -> dict:
    """Validate, format and write one file inside a generated project.

    Python files must parse and pass the safety scan. Deployment, container, CI/CD and notebook
    files are rejected. Requires an approved guardrail verdict. Automatically canonicalizes
    paths into the proper project directory structure.

    Args:
        project_name: snake_case project name.
        relative_path: Path inside the project, e.g. "<project>/tools/<module>.py" or "tools/<module>.py".
        content: Full file content.
        tool_context: Injected by ADK.

    Returns:
        dict with status and the written path, or the blocking findings.
    """
    blocked = _gate(tool_context)
    if blocked:
        return blocked
    project = slugify_identifier(project_name, default="generated_agent")
    canonical_rel = canonicalize_project_path(project, relative_path)
    reason = forbidden_reason(canonical_rel)
    if reason:
        return {"status": "error", "error": reason}
    final = content
    if canonical_rel.endswith(".py"):
        findings = scan_python_source(content, canonical_rel, expect_root_agent=canonical_rel.endswith("/agent.py") and canonical_rel.count("/") == 1)
        blockers = [f for f in findings if f["severity"] == "blocker"]
        if blockers:
            return {"status": "error", "error": "Code rejected by guardrail scan.", "blockers": blockers}
        final = _format(content)
    elif canonical_rel.endswith(".json"):
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            return {"status": "error", "error": f"Invalid JSON: {exc}"}
    return _write(project, canonical_rel, final, tool_context)


def list_project_files(project_name: str) -> dict:
    """List all files of a generated project.

    Args:
        project_name: snake_case project name.

    Returns:
        dict with status and relative file paths.
    """
    root = project_dir(project_name)
    if not root.is_dir():
        return {"status": "error", "error": "Project directory does not exist yet."}
    files = sorted(str(p.relative_to(OUTPUT_DIR.resolve())).replace("\\", "/") for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    return {"status": "success", "files": files}


def verify_generated_project(project_name: str, tool_context: ToolContext) -> dict:
    """Verify the generated project is complete, follows folder structure, and is importable-by-inspection.

    Checks:
    - Root agent.py exists in the inner package folder.
    - requirements.txt exists at project root.
    - Dedicated local data/ directory exists inside the agent folder.
    - Each tool function declared on the cards exists in its module and is documented.
    - No placeholder markers (TODO, NotImplementedError, pass-only bodies) remain.
    - No bare SQLite database connection strings ('*.db') without DB_PATH or DATA_DIR.

    Args:
        project_name: snake_case project name.
        tool_context: Injected by ADK.

    Returns:
        dict with "ok" and a list of problems to fix.
    """
    project = slugify_identifier(project_name, default="generated_agent")
    root = project_dir(project)
    pkg = root / project
    problems = []
    if not (pkg / "agent.py").is_file():
        problems.append("Missing agent.py in the package.")
    if not (root / "requirements.txt").is_file():
        problems.append("Missing requirements.txt at the project root.")
    if not (root / "data").is_dir():
        problems.append("Missing dedicated local 'data/' directory inside the agent folder.")

    parsed = {}
    for path in sorted(pkg.rglob("*.py")) if pkg.is_dir() else []:
        rel = str(path.relative_to(root)).replace("\\", "/")
        text = path.read_text(encoding="utf-8")
        try:
            parsed[rel] = ast.parse(text)
        except SyntaxError as exc:
            problems.append(f"{rel}: syntax error line {exc.lineno}: {exc.msg}")
            continue
        if "TODO" in text or "NotImplementedError" in text:
            problems.append(f"{rel}: contains a TODO/NotImplementedError placeholder.")

        # Enforce that SQLite databases stay inside data/ via DB_PATH or DATA_DIR
        for call_marker in ("sqlite3.connect('", 'sqlite3.connect("'):
            if call_marker in text:
                idx = text.find(call_marker) + len(call_marker)
                arg = text[idx : idx + 60].split("'", 1)[0].split('"', 1)[0]
                if arg.endswith((".db", ".sqlite", ".sqlite3")) and "/" not in arg and "\\" not in arg:
                    problems.append(
                        f"{rel}: bare database path '{arg}' in sqlite3.connect(). "
                        f"Must use DB_PATH or DATA_DIR / '{arg}' from ..shared_libraries.constants to keep databases inside the agent's folder."
                    )

    agent_tree = parsed.get(f"{project}/agent.py")
    if agent_tree is not None and not any(
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "root_agent" for t in n.targets) for n in agent_tree.body
    ):
        problems.append("agent.py does not define root_agent.")

    cards = _load_cards(project, tool_context)
    for card in cards.values():
        for tool in card.get("tools", []):
            rel = f"{project}/tools/{tool['module']}.py"
            tree = parsed.get(rel)
            if tree is None:
                problems.append(f"{rel}: tool module is missing (needed for '{tool['name']}').")
                continue
            funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
            func = funcs.get(tool["name"])
            if func is None:
                problems.append(f"{rel}: function '{tool['name']}' is not defined.")
                continue
            if not ast.get_docstring(func):
                problems.append(f"{rel}: function '{tool['name']}' needs a docstring (ADK uses it as the tool description).")
            body = [n for n in func.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))]
            if all(isinstance(n, ast.Pass) for n in body) or not body:
                problems.append(f"{rel}: function '{tool['name']}' has no real implementation.")

    tool_context.state[STATE_VERIFICATION] = {"project": project, "ok": not problems, "problems": problems[:20]}
    return {"status": "success", "ok": not problems, "problems": problems, "files_checked": sorted(parsed)}

