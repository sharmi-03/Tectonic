"""Deterministic build pipeline for the Builder Agent.

The LLM decides *what to say*; these tools decide *what happens next*:

  * build_status          — the current phase + exact next action (guided interview, then the build).
  * present_decisions     — publishes 1-2 use-case-specific checkbox questions for the current
                            interview step (session state ``pending_decisions`` → Studio select cards).
  * apply_decisions       — records the user's selections (UI JSON, ``- [x]`` replies, letters,
                            "defaults"), advances the interview, remembers preferences, audits them.
  * design_research       — Agentic-RAG over the design playbook + past project blueprints.
  * compile_specification — Phase 4: the Agent Architecture Specification (lead system prompt,
                            sub-agent roles, JSON config mapping modules to agents) + approval card.
  * finalize_build        — provisions the built project on the platform.

Interview steps live in ``interview.py``; each turn asks 1-2 questions for one step only.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from google.adk.tools import ToolContext

from ..shared_libraries.constants import (
    PROFILES_DIR,
    STATE_AGENT_CARDS,
    STATE_ARCH_SPEC,
    STATE_BUILD_PHASE,
    STATE_BUILD_RECORD,
    STATE_DECISION_ROUND,
    STATE_DECISIONS,
    STATE_GENERATED_FILES,
    STATE_GUARDRAIL_HISTORY,
    STATE_GUARDRAIL_REPORT,
    STATE_INTENT,
    STATE_INTERVIEW,
    STATE_PENDING_DECISIONS,
    STATE_PROJECT_NAME,
    STATE_REQUIREMENTS,
    STATE_RESEARCH,
    STATE_VERIFICATION,
)
from ..shared_libraries.types import DecisionQuestion, slugify_identifier
from ..tracing import logger
from .file_manager import read_registry
from .interview import (
    INTERVIEW_STEPS,
    MAX_QUESTIONS_PER_STEP,
    REVISE_TARGET,
    STEP_KEYS,
    TOTAL_STEPS,
    approval_question,
    compile_spec,
    merge_module_checklist,
    step_brief,
    step_index,
)

PLAYBOOK_TITLE = "Tectonic ADK Design Playbook"
DECISIONS_MARKER = "DECISIONS_JSON:"
_DEFAULT_WORDS = re.compile(r"\b(defaults?|recommended|go ahead with (the )?recommend\w*)\b", re.I)
# A typed answer to the cards: only option letters, optionally numbered — "A, C, B", "1A 2B", "a/b/a".
_LETTER_ANSWER = re.compile(r"^\s*(?:\d+\s*[).:-]?\s*)?[A-Ha-h](?:\s*[,;/ ]\s*(?:\d+\s*[).:-]?\s*)?[A-Ha-h])*\s*[.!]?\s*$")
_CHECKED_LINE = re.compile(r"^\s*[-*]?\s*\[\s*[xX✓✔]\s*\]\s*(.+)$")
_HEADING = re.compile(r"^\s*#{1,6}\s*(\d+)[.)]")


# --- platform access ------------------------------------------------------------------------------
def _runtime():
    try:
        from tectonic_runtime import get_runtime

        return get_runtime()
    except Exception:  # the Builder still works without the platform runtime
        return None


def _admin_ctx(tool_context: ToolContext | None):
    try:
        from tectonic_runtime import CallContext
        session = getattr(tool_context, "session", None) if tool_context else None
        return CallContext(agent_id="agent_builder", role="admin", team="agent_builder", session_id=getattr(session, "id", None))
    except Exception:
        return None


async def _invoke(module: str, op: str, payload: dict[str, Any], tool_context: ToolContext | None) -> dict[str, Any]:
    rt = _runtime()
    if rt is None:
        return {"status": "skipped", "error": "platform runtime unavailable"}
    return (await rt.invoke(module, op, payload, _admin_ctx(tool_context))).to_dict()


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}
    return {}


def _latest_user_text(tool_context: ToolContext) -> str:
    content = getattr(tool_context, "user_content", None)
    parts = getattr(content, "parts", None) or []
    return " ".join(p.text for p in parts if isinstance(getattr(p, "text", None), str)).strip()


def _interview(state: Any) -> dict:
    iv = _as_dict(state.get(STATE_INTERVIEW))
    return {"index": int(iv.get("index", 0)), "completed": list(iv.get("completed", [])),
            "approved": bool(iv.get("approved")), "return_to_compile": bool(iv.get("return_to_compile"))}


def _open_decisions(req: dict, answered: dict) -> list[dict]:
    out = []
    for raw in req.get("decisions") or []:
        try:
            q = DecisionQuestion.model_validate(raw)
        except Exception as exc:  # malformed question: skip it rather than break the step
            logger.warning("decision skipped: %s", exc)
            continue
        if q.id not in answered and len(q.options) >= 2:
            out.append(q.model_dump())
    return out


# --- decisions -----------------------------------------------------------------------------------
def _render_markdown(pending: dict) -> str:
    step = pending.get("step") or {}
    lines = [f"**Step {step['number']} of {step['total']} · {step['title']}**", ""] if step else []
    for i, q in enumerate(pending["questions"], 1):
        mode = " (select all that apply)" if q.get("multi_select") else " (select one)"
        lines.append(f"### {i}. {q['title']}{mode}")
        if q.get("why"):
            lines.append(f"_{q['why']}_")
        for o in q["options"]:
            rec = " (Recommended)" if o.get("recommended") else ""
            desc = f": {o['description']}" if o.get("description") else ""
            lines.append(f"- [ ] {o['id']}) {o['label']}{rec}{desc}")
        if q.get("allow_custom", True):
            lines.append("- [ ] Other: write your own")
        lines.append("")
    lines.append("Reply by putting an `x` in the brackets of your choices (e.g. `- [x]`), or pick them in the cards.")
    return "\n".join(lines).strip()


def _publish(tool_context: ToolContext, questions: list[dict], step_idx: int, project: str) -> dict:
    step = INTERVIEW_STEPS[step_idx]
    rnd = int(tool_context.state.get(STATE_DECISION_ROUND) or 0) + 1
    digest = hashlib.sha1(json.dumps([q["id"] for q in questions]).encode()).hexdigest()[:8]
    pending = {
        "id": f"decisions-{step['key']}-{digest}", "round": rnd, "title": f"Step {step_idx + 1} of {TOTAL_STEPS} · {step['title']}",
        "project": project, "questions": questions,
        "step": {"index": step_idx, "number": step_idx + 1, "total": TOTAL_STEPS, "key": step["key"], "title": step["title"], "phase": step["phase"]},
    }
    tool_context.state[STATE_PENDING_DECISIONS] = pending
    tool_context.state[STATE_DECISION_ROUND] = rnd
    return pending


async def present_decisions(tool_context: ToolContext) -> dict:
    """Publish the current interview step's questions (1-2, tailored to the use case) as checkbox cards.

    Call right after `requirement_understanding` returns decisions. Already-answered questions are
    skipped automatically. Then write a 1-2 line consultative recap, paste the returned `markdown`
    between <!--decisions--> and <!--/decisions-->, and STOP your turn to wait for the user.

    Args:
        tool_context: Injected by ADK.

    Returns:
        dict with status, step, question_count and markdown (also the fallback for non-UI clients).
    """
    req = _as_dict(tool_context.state.get(STATE_REQUIREMENTS))
    iv = _interview(tool_context.state)
    idx = min(iv["index"], TOTAL_STEPS - 1)
    questions = _open_decisions(req, dict(tool_context.state.get(STATE_DECISIONS) or {}))[:MAX_QUESTIONS_PER_STEP]
    if not questions:
        return {"status": "no_decisions", "message": "No open questions for this step: call build_status."}
    if STEP_KEYS[idx] == "modules":
        questions = [merge_module_checklist(q) if i == 0 else q for i, q in enumerate(questions)]
        questions = [DecisionQuestion.model_validate(q).model_dump() for q in questions]
    project = req.get("project_name") or tool_context.state.get(STATE_PROJECT_NAME, "")
    pending = _publish(tool_context, questions, idx, project)
    await _invoke("short_term_memory", "append",
                  {"content": f"Step {idx + 1} asked: " + "; ".join(q["title"] for q in questions), "role": "agent"}, tool_context)
    return {"status": "success", "step": pending["step"], "question_count": len(questions), "markdown": _render_markdown(pending),
            "instruction": "Show a short recap, include the markdown between <!--decisions--> markers, then stop and wait."}


def _match_option(text: str, q: dict) -> str | None:
    low = text.lower()
    m = re.match(r"^\s*([A-Na-n])[).:]\s*", text)
    if m and m.group(1).upper() in {o["id"] for o in q["options"]}:
        return m.group(1).upper()
    for o in q["options"]:
        if o["label"].lower() in low:
            return o["id"]
    return None


def _parse_checked(text: str, pending: dict) -> dict | None:
    """Parse a reply where the user ticked boxes in the markdown ("- [x] B) ...", "- [x] Other: ...")."""
    lines = text.splitlines()
    if not any(_CHECKED_LINE.match(line) for line in lines):
        return None
    questions = pending.get("questions", [])
    current = 0
    answers: dict[str, dict] = {}
    for line in lines:
        h = _HEADING.match(line)
        if h:
            current = max(0, min(len(questions) - 1, int(h.group(1)) - 1))
            continue
        m = _CHECKED_LINE.match(line)
        if not m or not questions:
            continue
        body = m.group(1).strip()
        q = questions[current]
        if body.lower().startswith("other"):
            custom = body.split(":", 1)[1].strip() if ":" in body else ""
            if custom and "write your own" not in custom.lower():
                answers[q["id"]] = {"custom": custom}
            continue
        oid = _match_option(body, q)
        if oid is None:  # the line may belong to another question (heading-less reply)
            for other in questions:
                oid = _match_option(body, other)
                if oid:
                    q = other
                    break
        if oid:
            entry = answers.setdefault(q["id"], {"options": []})
            if "options" in entry:
                entry["options"].append(oid)
    return {"answers": answers, "notes": ""} if answers else None


def _parse_answers(raw: str, pending: dict) -> dict:
    """Accept UI JSON, a DECISIONS_JSON message, ticked markdown, 'defaults', or letters like 'A, C, B' / '1A 2C'."""
    text = (raw or "").strip()
    if DECISIONS_MARKER in text:
        text = text.split(DECISIONS_MARKER, 1)[1].strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    checked = _parse_checked(text, pending)
    if checked:
        return checked
    if not text or _DEFAULT_WORDS.search(text):
        return {"defaults": True}
    if not _LETTER_ANSWER.match(text):
        return {"defaults": True, "notes": text}
    # Letter boundaries (not \b): "1A" has no word boundary between the digit and the letter.
    letters = re.findall(r"(?:(\d+)\s*[).:-]?\s*)?(?<![A-Za-z])([A-Ha-h])(?![A-Za-z])", text)
    answers: dict[str, Any] = {}
    questions = pending.get("questions", [])
    position = 0
    for num, letter in letters:
        idx = int(num) - 1 if num else position
        if 0 <= idx < len(questions):
            answers[questions[idx]["id"]] = {"option": letter.upper()}
            position = idx + 1
    if not answers:
        return {"defaults": True, "notes": text}
    return {"answers": answers, "notes": ""}


def _advance(state: Any, pending: dict, resolved: dict) -> str:
    """Move the interview forward after a step is answered. Returns a short status line."""
    iv = _interview(state)
    step = pending.get("step") or {"index": iv["index"], "key": STEP_KEYS[min(iv["index"], TOTAL_STEPS - 1)]}
    key = step["key"]
    if key == "compile":
        choice = resolved.get("spec_approval", {})
        ids = choice.get("option_ids") or []
        target = REVISE_TARGET.get(ids[0]) if ids else None
        if choice.get("custom"):
            target = "topology" if "topolog" in choice.get("choice", "").lower() else target
        if target is None and not choice.get("custom"):
            iv["approved"] = True
            msg = "Specification approved: proceed to the build."
        else:
            target = target or "agents"
            iv["index"] = step_index(target)
            iv["return_to_compile"] = True
            for qid in [q for q, e in resolved.items() if e.get("step") == target or q == "spec_approval"]:
                resolved.pop(qid, None)
            state[STATE_ARCH_SPEC] = {}
            msg = f"Revising '{INTERVIEW_STEPS[iv['index']]['title']}'" + (f" (note: {choice['choice']})" if choice.get("custom") else "")
    else:
        if key not in iv["completed"]:
            iv["completed"].append(key)
        if iv["return_to_compile"]:
            iv["index"], iv["return_to_compile"] = step_index("compile"), False
        else:
            iv["index"] = min(step_index(key) + 1, TOTAL_STEPS - 1)
        msg = f"Step {step['index'] + 1} complete; next: {INTERVIEW_STEPS[iv['index']]['title']}."
    state[STATE_INTERVIEW] = iv
    state[STATE_DECISIONS] = resolved
    # Questions beyond this step's cap were never shown: drop them so the next step asks fresh, tailored ones.
    req = _as_dict(state.get(STATE_REQUIREMENTS))
    if req.get("decisions"):
        state[STATE_REQUIREMENTS] = {**req, "decisions": []}
    return msg


async def apply_decisions(answers: str, tool_context: ToolContext) -> dict:
    """Record the user's choices for the pending checkbox cards and advance the interview.

    Pass the user's reply verbatim: the UI sends 'DECISIONS_JSON: {...}'; replies with ticked boxes
    ('- [x] B) ...'), letters ('A, C', '1A 2B'), 'use defaults' or a JSON object also work.
    Unanswered questions take their recommended option(s).

    Args:
        answers: The user's reply (verbatim) or a JSON object {"answers": {question_id: {"option": "A"} |
            {"options": ["A","C"]} | {"custom": "text"}}, "notes": "..."} or {"defaults": true}.
        tool_context: Injected by ADK.

    Returns:
        dict with status, the resolved choices, the interview position and what to do next.
    """
    pending = _as_dict(tool_context.state.get(STATE_PENDING_DECISIONS))
    if not pending.get("questions"):
        return {"status": "error", "error": "No pending decisions to apply. Call build_status."}
    parsed = _parse_answers(answers, pending)
    given = parsed.get("answers") or {}
    resolved: dict[str, dict] = dict(tool_context.state.get(STATE_DECISIONS) or {})
    step_key = (pending.get("step") or {}).get("key", "general")
    lines = []
    for q in pending["questions"]:
        a = given.get(q["id"]) if isinstance(given, dict) else None
        opts = {o["id"]: o for o in q["options"]}
        recommended = [o["id"] for o in q["options"] if o.get("recommended")] or [q["options"][0]["id"]]
        entry: dict[str, Any] = {"title": q["title"], "category": q.get("category", "general"), "step": step_key, "defaulted": False}
        if isinstance(a, dict) and a.get("custom"):
            entry.update(choice=str(a["custom"])[:500], option_ids=[], selected=[], custom=True)
        else:
            ids: list[str] = []
            if isinstance(a, dict):
                ids = [str(x).upper() for x in (a.get("options") or [a.get("option")]) if x]
            elif isinstance(a, str):
                ids = [a.upper()]
            ids = [i for i in dict.fromkeys(ids) if i in opts]
            if not q.get("multi_select"):
                ids = ids[:1]
            if not ids:  # unanswered (or "use defaults"): take the recommended option(s)
                ids = recommended if q.get("multi_select") else recommended[:1]
                entry["defaulted"] = True
            entry.update(choice=", ".join(opts[i]["label"] for i in ids), option_ids=ids,
                         selected=[{"id": i, "label": opts[i]["label"], "description": opts[i].get("description", ""),
                                    "module_id": opts[i].get("module_id", "")} for i in ids], custom=False)
        resolved[q["id"]] = entry
        lines.append(f"- {q['title']}: {entry['choice']}{' (recommended default)' if entry['defaulted'] else ''}")
    notes = str(parsed.get("notes") or "").strip()
    tool_context.state[STATE_PENDING_DECISIONS] = {}
    status_line = _advance(tool_context.state, pending, resolved)
    summary = f"Confirmed ({pending.get('title', 'decisions')}):\n" + "\n".join(lines) + (f"\nAdditional notes: {notes}" if notes else "")

    for q in pending["questions"]:
        e = resolved.get(q["id"])
        if e and not e["defaulted"] and step_key != "compile":
            await _invoke("long_term_memory", "remember",
                          {"content": f"User preference — {e['title']}: {e['choice']}", "memory_type": "fact"}, tool_context)
    await _invoke("auditability", "record_event", {"event_type": "DESIGN_DECISIONS", "payload": {
        "project": pending.get("project"), "step": step_key, "decisions": {q["id"]: resolved.get(q["id"]) for q in pending["questions"]},
        "notes": notes}}, tool_context)
    await _invoke("short_term_memory", "append", {"content": summary, "role": "user"}, tool_context)
    return {"status": "success", "applied": len(pending["questions"]), "summary": summary, "interview": status_line,
            "next": "Call build_status and follow next_action."}


# --- research ------------------------------------------------------------------------------------
async def _ensure_playbook(tool_context: ToolContext | None) -> None:
    rt = _runtime()
    if rt is None:
        return
    rag = rt.module("agentic_rag")
    await rag.boot()
    if any(d.get("title") == PLAYBOOK_TITLE for d in rag.documents.values()):
        return
    text = Path(PROFILES_DIR / "design_playbook.md").read_text(encoding="utf-8")
    await _invoke("agentic_rag", "ingest", {"title": PLAYBOOK_TITLE, "text": text, "namespace": "shared", "source": "builder"}, tool_context)


async def design_research(question: str, tool_context: ToolContext) -> dict:
    """Ground the design in the platform's knowledge base: the ADK design playbook plus blueprints of
    previously built projects (Agentic RAG with routing, self-correction and citations).

    The pipeline runs it after inputs & outputs are known, before proposing a topology. Pass a focused
    question that includes the use case, e.g. "Best topology, sub-agents and modules for <use case>".

    Args:
        question: The research question.
        tool_context: Injected by ADK.

    Returns:
        dict with status, answer (with [n] citations), citations and groundedness.
    """
    await _ensure_playbook(tool_context)
    result = await _invoke("agentic_rag", "ask", {"question": question, "namespaces": ["shared"], "max_hops": 2}, tool_context)
    if result.get("status") != "success":
        return {"status": "error", "error": result.get("error", "knowledge base unavailable")}
    data = result["data"]
    findings = {"question": question, "answer": data.get("answer", ""), "groundedness": data.get("groundedness"),
                "sources": sorted({c["source"] for c in data.get("citations", [])})}
    tool_context.state[STATE_RESEARCH] = findings
    return {"status": "success", **findings, "citations": data.get("citations", [])[:6]}


# --- Phase 4: compilation ----------------------------------------------------------------------
async def compile_specification(tool_context: ToolContext) -> dict:
    """Phase 4 — compile the Agent Architecture Specification from the completed requirements and every
    decision: the Lead Agent system prompt, sub-agent roles, and a JSON configuration mapping the selected
    modules and tools to each agent. Publishes an approval card (Approve & build / Revise a step).

    After calling it: write one consultative line, paste the returned `markdown` between <!--spec--> and
    <!--/spec-->, then paste `decision_markdown` between <!--decisions--> markers, and STOP.

    Args:
        tool_context: Injected by ADK.

    Returns:
        dict with status, markdown (the specification), decision_markdown and enabled_modules.
    """
    req = _as_dict(tool_context.state.get(STATE_REQUIREMENTS))
    if not req.get("is_complete"):
        return {"status": "error", "error": "Requirements are not complete yet: call requirement_understanding (COMPILE)."}
    spec = compile_spec(req, dict(tool_context.state.get(STATE_DECISIONS) or {}))
    tool_context.state[STATE_ARCH_SPEC] = spec
    pending = _publish(tool_context, [DecisionQuestion.model_validate(approval_question()).model_dump()], step_index("compile"), spec["project"])
    await _invoke("auditability", "record_event", {"event_type": "SPEC_COMPILED", "payload": {
        "project": spec["project"], "enabled_modules": spec["enabled_modules"],
        "spec_sha256": hashlib.sha256(spec["markdown"].encode()).hexdigest()}}, tool_context)
    return {"status": "success", "project": spec["project"], "enabled_modules": spec["enabled_modules"],
            "markdown": spec["markdown"], "decision_markdown": _render_markdown(pending)}


# --- status / state machine ----------------------------------------------------------------------
def _guardrail_approved(state: Any, stage: str) -> bool:
    history = state.get(STATE_GUARDRAIL_HISTORY) or []
    report = state.get(STATE_GUARDRAIL_REPORT) or {}
    entries = [*(history if isinstance(history, list) else []), report]
    return any(isinstance(e, dict) and e.get("stage") == stage and e.get("approved") for e in entries)


def _journey(iv: dict, phase: str) -> dict:
    """Compact progress model for the Studio's journey stepper."""
    build_phases = ["design_validation", "agent_cards", "code_generation", "verification", "finalize", "deliver"]
    return {"step_index": iv["index"], "total": TOTAL_STEPS, "completed": iv["completed"], "approved": iv["approved"],
            "phase": phase, "build_stage": build_phases.index(phase) if phase in build_phases else None,
            "steps": [{"key": s["key"], "title": s["title"], "phase": s["phase"]} for s in INTERVIEW_STEPS]}


def build_status(tool_context: ToolContext) -> dict:
    """Where is this build, and what EXACTLY should happen next? Call at the start of every turn and
    after each step; follow `next_action`. Never skip ahead of it.

    Args:
        tool_context: Injected by ADK.

    Returns:
        dict with phase, interview step, next_action and a checklist of completed stages.
    """
    s = tool_context.state
    req = _as_dict(s.get(STATE_REQUIREMENTS))
    project = req.get("project_name") or s.get(STATE_PROJECT_NAME) or ""
    pending = _as_dict(s.get(STATE_PENDING_DECISIONS))
    answered = dict(s.get(STATE_DECISIONS) or {})
    iv = _interview(s)
    cards = (_as_dict(s.get(STATE_AGENT_CARDS)).get(project) or {}) if project else {}
    if project and not cards:
        try:
            cards = read_registry(project) or {}
        except Exception:
            cards = {}
    verification = _as_dict(s.get(STATE_VERIFICATION))
    build = _as_dict(s.get(STATE_BUILD_RECORD))
    spec = _as_dict(s.get(STATE_ARCH_SPEC))
    user_text = _latest_user_text(tool_context)

    checklist = {
        "intent": bool(s.get(STATE_INTENT)),
        "request_safety": _guardrail_approved(s, "request"),
        "interview_step": f"{min(iv['index'], TOTAL_STEPS - 1) + 1}/{TOTAL_STEPS}",
        "interview_completed": iv["completed"],
        "design_research": bool(s.get(STATE_RESEARCH)),
        "spec_approved": iv["approved"],
        "design_approved": _guardrail_approved(s, "design"),
        "agent_cards": len(cards),
        "files_written": len(s.get(STATE_GENERATED_FILES) or []),
        "verified": bool(verification.get("ok")) and verification.get("project") == project,
        "platform_provisioned": bool(build.get("provisioned")) and build.get("project") == project,
    }

    def step(phase: str, action: str, **extra: Any) -> dict:
        s[STATE_BUILD_PHASE] = _journey(iv, phase)
        return {"status": "success", "phase": phase, "next_action": action, "project_name": project, "checklist": checklist, **extra}

    if pending.get("questions"):
        if (DECISIONS_MARKER in user_text or _DEFAULT_WORDS.search(user_text) or _LETTER_ANSWER.match(user_text)
                or any(_CHECKED_LINE.match(line) for line in user_text.splitlines())):
            return step("apply_decisions", "Call apply_decisions with the user's latest message verbatim, then build_status.")
        return step("awaiting_user", "The user has not answered the open cards. If they asked a question, answer it briefly "
                    "(you may use design_research), then re-show the cards with present_decisions and stop.",
                    pending_step=(pending.get("step") or {}).get("title"))
    if not checklist["intent"]:
        return step("classify", "Call intent_detection with the user's latest message.")
    if not checklist["request_safety"]:
        return step("request_safety", "Call guardrails with stage='request' and the user's request text.")

    if not iv["approved"]:
        idx = min(iv["index"], TOTAL_STEPS - 1)
        key = STEP_KEYS[idx]
        if key == "compile":
            if not req.get("is_complete") or _open_decisions(req, answered):
                return step("compile_requirements",
                            "Call requirement_understanding with request 'COMPILE: all interview steps are answered — produce the "
                            "complete specification now (is_complete true, decisions []), honouring every decision.'")
            return step("compile_specification", "Call compile_specification, show the spec and the approval card, then STOP.")
        if key == "topology" and not checklist["design_research"]:
            return step("design_research", "Call design_research with a question naming the use case, its inputs/outputs and "
                        "asking for the best topology, sub-agents and modules.")
        if _open_decisions(req, answered):
            return step("present_decisions", f"Call present_decisions for step {idx + 1} ({INTERVIEW_STEPS[idx]['title']}), show the "
                        "recap + markdown, then STOP and wait for the user.", interview_step=idx + 1)
        return step(f"interview:{key}", f"Call requirement_understanding with this request:\n{step_brief(idx)}\n"
                    "Include a one-line summary of the user's use case and confirmed decisions. Then call present_decisions.",
                    interview_step=idx + 1)

    if not checklist["agent_cards"]:
        if not _guardrail_approved(s, "design"):
            return step("design_validation", "Call guardrails with stage='design' to validate the requirements.")
        return step("agent_cards", f"Call agent_card_manager to create and link agent cards for project '{project}', following "
                    "the approved architecture_spec (agents, roles, tools) exactly.")
    if not checklist["files_written"]:
        return step("code_generation", f"Call code_generator for project '{project}' (scaffold, implement every tool, verify).")
    if not checklist["verified"]:
        return step("verification", f"Ask code_generator to run verify_generated_project for '{project}' and fix every problem it reports.",
                    problems=verification.get("problems", []))
    if not checklist["platform_provisioned"]:
        return step("finalize", f"Call finalize_build for '{project}' (budget, knowledge, managed prompts, audit).")
    return step("deliver", "Call persist_progress, then deliver the final summary (project path, agents, tools, platform setup, run guide).",
                enabled_modules=spec.get("enabled_modules"))


# --- finalize --------------------------------------------------------------------------------------
def _decision(decisions: dict, *needles: str) -> dict | None:
    for qid, e in decisions.items():
        hay = f"{qid} {e.get('title', '')} {e.get('category', '')}".lower()
        if any(n in hay for n in needles):
            return e
    return None


def _budget_from(decisions: dict) -> tuple[float, str]:
    e = _decision(decisions, "budget", "spend", "cost")
    if not e:
        return 5.0, "alert"
    text = e.get("choice", "")
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)", text)
    amount = float(m.group(1)) if m else 5.0
    enforcement = "block" if re.search(r"\b(block|hard|cap|stop)\b", text, re.I) else "alert"
    return max(0.5, amount), enforcement


def _spec_text(project: str, req: dict, decisions: dict, cards: dict) -> str:
    def bullets(items: Any) -> str:
        return "\n".join(f"- {i if isinstance(i, str) else json.dumps(i, default=str)[:300]}" for i in (items or [])) or "- (none)"

    agents = "\n".join(f"- {name}: {c.get('description', '')} (tools: {', '.join(t.get('name', '') for t in c.get('tools', []))})"
                       for name, c in cards.items()) or "- (see agent cards)"
    tools = "\n".join(f"- {t.get('name')}: {t.get('description', '')}" for t in req.get("tools", []) if isinstance(t, dict)) or "- (none)"
    chosen = "\n".join(f"- {e['title']}: {e['choice']}" for e in decisions.values()) or "- (defaults)"
    return (f"Project {project}\n\nGoal: {req.get('goal', '')}\n\nTarget users: {req.get('target_users', '')}\n\n"
            f"Architecture: {req.get('architecture', '')}\n\nAgents:\n{agents}\n\nTools:\n{tools}\n\n"
            f"Design decisions:\n{chosen}\n\nGuardrails:\n{bullets(req.get('guardrail_requirements'))}\n\n"
            f"Acceptance criteria:\n{bullets(req.get('acceptance_criteria'))}")


async def finalize_build(project_name: str, tool_context: ToolContext) -> dict:
    """Provision the generated project on the platform so its worker agents are governed and
    knowledge-grounded from their first run. Run after verification succeeds.

    Steps (each reported individually; one failure never aborts the rest):
      1. FinOps budget for the project team (from the budget decision, default $5/month alert).
      2. Knowledge: the final specification into the project's namespace (workers RAG over it).
      3. PromptOps: every agent's instruction registered as managed prompt '<project>.<agent>'.
      4. Blueprint into the shared knowledge base so future builds can learn from this design.
      5. Long-term memory of the build, and an audited BUILD_COMPLETED record (spec hash, files).

    Args:
        project_name: The generated project's snake_case name.
        tool_context: Injected by ADK.

    Returns:
        dict with status and per-step results.
    """
    project = slugify_identifier(project_name, default="generated_agent")
    s = tool_context.state
    req = _as_dict(s.get(STATE_REQUIREMENTS))
    decisions = dict(s.get(STATE_DECISIONS) or {})
    cards = (_as_dict(s.get(STATE_AGENT_CARDS)).get(project) or {})
    if not cards:
        try:
            cards = read_registry(project) or {}
        except Exception:
            cards = {}
    spec = _spec_text(project, req, decisions, cards)
    arch = _as_dict(s.get(STATE_ARCH_SPEC))
    if arch.get("lead_system_prompt"):
        spec += f"\n\nLead agent system prompt:\n{arch['lead_system_prompt']}"
    spec_hash = hashlib.sha256(spec.encode()).hexdigest()
    steps: dict[str, Any] = {}

    amount, enforcement = _budget_from(decisions)
    r = await _invoke("finops", "set_budget", {"team": project, "limit_usd": amount, "enforcement": enforcement}, tool_context)
    steps["budget"] = r.get("data") or r.get("error")

    r = await _invoke("agentic_rag", "ingest", {"title": f"Requirements: {project}", "text": spec, "namespace": project, "source": "builder"}, tool_context)
    steps["knowledge"] = r.get("data") or r.get("error")

    prompts = []
    rt = _runtime()
    if rt is not None:
        try:
            from tectonic_runtime.adk_tools import CORE_MODULE_INSTRUCTIONS

            po = rt.module("promptops")
            for name, card in cards.items():
                template = f"{str(card.get('instruction', '')).strip()}\n" + CORE_MODULE_INSTRUCTIONS
                v = await po.ensure_prompt(f"{project}.{name}", template)
                prompts.append({"prompt": f"{project}.{name}", "version": v.version})
        except Exception as exc:
            prompts.append({"error": str(exc)})
    steps["prompts"] = prompts

    r = await _invoke("agentic_rag", "ingest", {"title": f"Blueprint: {project}", "text": spec, "namespace": "shared", "source": "builder"}, tool_context)
    steps["blueprint"] = (r.get("data") or {}).get("doc_id") or r.get("error")

    await _invoke("long_term_memory", "remember",
                  {"content": f"Built project {project}: {req.get('goal', '')} ({req.get('architecture', '')}, {len(cards)} agents)",
                   "memory_type": "episodic"}, tool_context)
    r = await _invoke("auditability", "record_event", {"event_type": "BUILD_COMPLETED", "team": project, "payload": {
        "project": project, "spec_sha256": spec_hash, "agents": sorted(cards), "files": len(s.get(STATE_GENERATED_FILES) or []),
        "budget": {"limit_usd": amount, "enforcement": enforcement}, "enabled_modules": arch.get("enabled_modules"),
        "decisions": decisions}}, tool_context)
    steps["audit"] = (r.get("data") or {}).get("sequence") or r.get("error")

    s[STATE_BUILD_RECORD] = {"project": project, "provisioned": True, "spec_sha256": spec_hash,
                             "budget": {"limit_usd": amount, "enforcement": enforcement}, "prompts": prompts}
    return {"status": "success", "project": project, "steps": steps,
            "next": "Call persist_progress, then deliver the final summary including this platform setup."}
