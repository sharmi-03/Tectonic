"""Single source of truth for the model, filesystem paths and environment lookups."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .studio_model import StudioModel, build_claude, is_claude

# Suppress ADK advisory notice for LiteLLM
os.environ["ADK_SUPPRESS_GEMINI_LITELLM_WARNINGS"] = "true"

PACKAGE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PACKAGE_DIR.parent

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv()

# --- Model & Environment --------------------------------------------------
# Support Google Agent Builder Platform API Key (AQ.* format) and standard Gemini API keys
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
if GOOGLE_API_KEY:
    os.environ["GOOGLE_API_KEY"] = GOOGLE_API_KEY
    os.environ["GEMINI_API_KEY"] = GOOGLE_API_KEY

import litellm

# LiteLLM global config — only used as fallback when native Gemini SDK fails.
# Keep retries low: native SDK handles retries itself, and we don't want LiteLLM
# to silently retry 5x before surfacing an error to the user.
litellm.num_retries = 2
litellm.request_timeout = 60

# Default model - gemini-3.6-flash is the active production model
RAW_MODEL_NAME = os.getenv("GEMINI_MODEL", os.getenv("MODEL_NAME", "gemini-3.6-flash"))


def resolve_model_name(raw_name: str) -> str:
    """Normalize model string to provider format for LiteLLM generalization."""
    if not raw_name:
        return "gemini-3.6-flash"
    clean = raw_name.strip()
    # For native Gemini model, don't add gemini/ prefix
    if "/" in clean:
        # Extract just the model name for native Gemini
        return clean.split("/")[-1]
    return clean


def resolve_litellm_model_name(raw_name: str) -> str:
    """Normalize model string to provider format for LiteLLM."""
    if not raw_name:
        return "gemini/gemini-3.6-flash"
    clean = raw_name.strip()
    if "/" in clean:
        return clean
    if clean.startswith("gemini"):
        return f"gemini/{clean}"
    return clean


MODEL_NAME = resolve_model_name(RAW_MODEL_NAME)
MODEL_ID = MODEL_NAME


def _is_aq_key(api_key: str) -> bool:
    """Check if the API key is an AQ. (Authentication Key) format from Google AI Studio."""
    return api_key and api_key.startswith("AQ.")


def build_model(model_name: str = None):
    """Create the model wrapper for a model name: Claude or Gemini.

    For Gemini we ALWAYS use the native google-genai SDK (Gemini class) regardless
    of key format. This calls generativelanguage.googleapis.com directly and avoids
    LiteLLM routing through vertex_ai which causes frequent 500/503 errors.
    LiteLLM is only kept as a last-resort fallback.
    """
    if model_name and is_claude(model_name):
        anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not anthropic_key:
            raise ValueError(f"{model_name} needs an Anthropic API key (ANTHROPIC_API_KEY).")
        return build_claude(model_name, anthropic_key)

    target_model = resolve_model_name(model_name or MODEL_NAME)

    # Native Gemini SDK — works for both AIza* (standard) and AQ.* (auth) keys.
    # Direct call to Google AI Studio API, no LiteLLM proxy, far more reliable.
    try:
        from google.adk.models.google_llm import Gemini
        return Gemini(model=target_model)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Native Gemini SDK unavailable (%s); falling back to LiteLLM.", exc
        )

    # Fallback: LiteLLM (use fewer retries to fail fast and surface real errors)
    api_key = os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
    from google.adk.models.lite_llm import LiteLlm
    litellm_model = resolve_litellm_model_name(model_name or RAW_MODEL_NAME)
    kwargs: dict = {"model": litellm_model, "num_retries": 2}
    if api_key and "gemini" in litellm_model.lower():
        kwargs["api_key"] = api_key
    return LiteLlm(**kwargs)


def _initial_builder_model() -> StudioModel:
    """The Builder's model from BUILDER_MODEL (set in the Studio), else GEMINI_MODEL."""
    name = (os.getenv("BUILDER_MODEL") or RAW_MODEL_NAME).strip()
    try:
        delegate = build_model(name)
    except ValueError as exc:  # e.g. Claude chosen but its key was removed: keep the Builder usable
        logging.getLogger(__name__).warning("Builder model %s unavailable (%s); using %s", name, exc, RAW_MODEL_NAME)
        name, delegate = RAW_MODEL_NAME, build_model()
    model = StudioModel(model=name)
    model.use(delegate, name)
    return model


# Shared by every Builder agent; switching it in the Studio applies to the next call (see studio_model.py).
MODEL = _initial_builder_model()


# --- Paths ---------------------------------------------------------------
def _resolve_dir(env_name: str, default: Path) -> Path:
    raw = os.getenv(env_name)
    if not raw:
        return default.resolve()
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


PROFILES_DIR = PACKAGE_DIR / "profiles"
OUTPUT_DIR = _resolve_dir("AGENT_BUILDER_OUTPUT_DIR", PROJECT_ROOT / "generated_agents")
LOG_DIR = _resolve_dir("AGENT_BUILDER_LOG_DIR", PROJECT_ROOT / "logs")
REGISTRY_DIR_NAME = "_registry"
SESSIONS_DIR_NAME = "_sessions"
EDITS_DIR_NAME = "_edits"  # per-project history of conversational edits (what undo restores)

# --- Session state keys --------------------------------------------------
STATE_INTENT = "intent"
STATE_REQUIREMENTS = "requirements"
STATE_GUARDRAIL_REPORT = "guardrail_report"
STATE_GUARDRAIL_HISTORY = "guardrail_history"
STATE_AGENT_CARDS = "agent_cards"
STATE_PROJECT_NAME = "project_name"
STATE_GENERATED_FILES = "generated_files"
STATE_NOTES = "notes"
STATE_VERIFICATION = "verification"
STATE_PENDING_DECISIONS = "pending_decisions"  # structured select-cards awaiting the user (read by the Studio UI)
STATE_DECISIONS = "decisions_answered"  # every decision the user has made, keyed by question id
STATE_DECISION_ROUND = "decision_round"
STATE_RESEARCH = "design_research"
STATE_BUILD_RECORD = "build_record"
STATE_INTERVIEW = "interview"  # {"index", "completed", "approved", "return_to_compile"}: the guided interview position
STATE_ARCH_SPEC = "architecture_spec"  # compiled Agent Architecture Specification (lead prompt, roles, JSON config)
STATE_BUILD_PHASE = "build_phase"  # current pipeline phase, mirrored to the Studio's journey stepper

# Keys the orchestrator may write through save_progress (the guardrail verdict
# is deliberately excluded: only the guardrails sub-agent can record it).
PROGRESS_KEYS = (
    STATE_INTENT,
    STATE_REQUIREMENTS,
    STATE_PROJECT_NAME,
    STATE_NOTES,
)

# --- Local-only enforcement ---------------------------------------------
FORBIDDEN_FILENAMES = (
    "dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".dockerignore",
    "cloudbuild.yaml",
    "app.yaml",
    "procfile",
    ".gitlab-ci.yml",
    "deploy.py",
)
FORBIDDEN_PATH_PARTS = (
    ".github",
    ".ipynb_checkpoints",
    "__pycache__",
    ".pytest_cache",
    "terraform",
    "helm",
    "k8s",
    "kubernetes",
)
FORBIDDEN_SUFFIXES = (".tf", ".tfvars", ".ipynb")
ALLOWED_WRITE_SUFFIXES = (".py", ".json", ".txt", ".md", ".example", ".env", ".gitkeep")
