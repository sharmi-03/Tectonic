"""FinOps module: LLM cost tracking, token optimisation and budget guardrails (modules/finops core).

Module core: budget policies, period-windowed cost reports, run-rate forecasting and the
cost-optimisation agent (tightens alert thresholds when the forecast overruns the budget).

This wrapper adds:
  * Metering — every agent LLM call (via the telemetry plugin) and every platform model call
    (Agentic-RAG, PromptOps, Auditability via tectonic_runtime.models) becomes usage events,
    attributed to team, agent, model and kind (input/output/embedding).
  * Rate card — per-model USD prices per 1M tokens. The module ships none, so defaults are
    seeded from published Gemini list prices and every rate is flagged ``estimate`` until an
    admin confirms it; unknown models fall back to a flagged default.
  * Guardrails — budgets carry an enforcement mode: "alert" (default) or "block". Blocking
    budgets stop further LLM calls for that team once the period limit is reached.
  * Breakdown + recommendations — spend by agent/model/day, and data-driven token-optimisation
    tips (prompt-heavy agents, model right-sizing, repeated identical calls).
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .._paths import ensure_module_paths
from ..base import BaseModule, CallContext, operation
from ..persist import from_dict, read_json, state_path, write_json

ensure_module_paths()

from finops.core.budget_policy_service import BudgetPolicyService  # noqa: E402
from finops.core.cost_optimisation_agent import CostOptimisationAgent  # noqa: E402
from finops.core.domain import BudgetPeriod, BudgetPolicyRecord, OptimisationActionRecord, UsageEventRecord, new_id, period_window  # noqa: E402
from finops.core.fakes import InMemoryFinOpsRepository  # noqa: E402
from finops.core.forecasting_service import ForecastingService  # noqa: E402
from finops.core.usage_aggregation_service import UsageAggregationService  # noqa: E402

PLATFORM_TEAM = "agent_builder"
MAX_EVENTS = 50_000

# USD per 1M tokens. Seeded from Google's published Gemini list prices; flagged as estimates
# because prices change and contracts differ — admins confirm or override with set_rate.
DEFAULT_RATES: dict[str, dict[str, Any]] = {
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "estimate": True, "note": "list price (≤200k context)"},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "estimate": True, "note": "list price"},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "estimate": True, "note": "list price"},
    "gemini-embedding-001": {"input": 0.15, "output": 0.0, "estimate": True, "note": "list price"},
    # Anthropic list prices (first-party API) for the Claude models the Builder can run on.
    "claude-fable-5-1": {"input": 10.00, "output": 50.00, "estimate": True, "note": "Anthropic list price"},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "estimate": True, "note": "Anthropic list price"},
    "claude-opus-5": {"input": 5.00, "output": 25.00, "estimate": True, "note": "Anthropic list price"},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "estimate": True, "note": "Anthropic list price"},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00, "estimate": True, "note": "Anthropic list price"},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "estimate": True, "note": "Anthropic list price (prompts ≤100K tokens)"},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "estimate": True, "note": "Anthropic list price"},
}
FALLBACK_RATE = {"input": 0.30, "output": 2.50, "estimate": True,
                 "note": "no rate on file — using gemini-2.5-flash list price as an estimate"}


def normalize_model(model: str) -> str:
    """'gemini/gemini-3.6-flash', 'models/gemini-3.6-flash' → 'gemini-3.6-flash'."""
    return (model or "unknown").split("/")[-1].strip().lower()


class PersistentFinOpsRepository(InMemoryFinOpsRepository):
    """The module's repository, snapshotted to JSON (usage events capped at MAX_EVENTS)."""

    def __init__(self, path) -> None:
        super().__init__()
        self._path = path
        raw = read_json(path) or {}
        self.usage_events = [from_dict(UsageEventRecord, r) for r in raw.get("usage_events", [])]
        self.budget_policies = {r["id"]: from_dict(BudgetPolicyRecord, r) for r in raw.get("budget_policies", [])}
        self.optimisation_actions = [from_dict(OptimisationActionRecord, r) for r in raw.get("optimisation_actions", [])]
        self.extras: dict[str, Any] = raw.get("extras", {"rates": {}, "enforcement": {}})
        self._dirty_since = 0.0

    def flush(self) -> None:
        if len(self.usage_events) > MAX_EVENTS:
            self.usage_events = self.usage_events[-MAX_EVENTS:]
        write_json(self._path, {
            "usage_events": self.usage_events, "budget_policies": list(self.budget_policies.values()),
            "optimisation_actions": self.optimisation_actions, "extras": self.extras,
        })
        self._dirty_since = 0.0

    def mark_dirty(self) -> None:
        # Usage events arrive per model call; batch disk writes to at most one every 2 s.
        now = time.monotonic()
        if not self._dirty_since:
            self._dirty_since = now
        if now - self._dirty_since > 2.0:
            self.flush()

    async def create_budget_policy(self, record):  # type: ignore[override]
        result = await super().create_budget_policy(record)
        self.flush()
        return result

    async def update_budget_policy(self, record):  # type: ignore[override]
        result = await super().update_budget_policy(record)
        self.flush()
        return result

    async def create_optimisation_action(self, record):  # type: ignore[override]
        result = await super().create_optimisation_action(record)
        self.flush()
        return result


class MeteredSpendClient:
    """LLMGatewaySpendClient port. LLM spend is metered here as usage events (the platform has no
    separate gateway ledger), so live gateway spend is 0.0 — avoiding double counting by design."""

    async def tenant_spend(self, tenant_id: str) -> float:
        return 0.0


class FinOpsModule(BaseModule):
    name = "finops"
    label = "FinOps"
    description = "Meters LLM tokens and cost per team/agent/model, forecasts spend and enforces budgets."
    category = "governance"

    async def setup(self) -> None:
        self.repository = PersistentFinOpsRepository(state_path("finops.json", "TECTONIC_FINOPS_PATH"))
        self.budgets = BudgetPolicyService(self.repository)
        self.aggregation = UsageAggregationService(self.repository, MeteredSpendClient())
        self.forecasting = ForecastingService()
        self.optimiser = CostOptimisationAgent(self.repository, self.aggregation, self.forecasting)
        self._call_fingerprints: Counter[str] = Counter()

    # --- rate card --------------------------------------------------------------------------
    def rate_for(self, model: str) -> dict[str, Any]:
        m = normalize_model(model)
        custom = self.repository.extras.setdefault("rates", {})
        if m in custom:
            return {**custom[m], "model": m, "source": "admin"}
        if m in DEFAULT_RATES:
            return {**DEFAULT_RATES[m], "model": m, "source": "default"}
        return {**FALLBACK_RATE, "model": m, "source": "fallback"}

    # --- metering (Python API: telemetry plugin + model hook) --------------------------------
    async def meter(self, *, team: str | None, agent_id: str, model: str, input_tokens: int, output_tokens: int,
                    kind: str = "agent", fingerprint: str | None = None) -> float:
        await self.boot()
        team = team or PLATFORM_TEAM
        rate = self.rate_for(model)
        m = rate["model"]
        cost = 0.0
        for direction, qty in (("in", input_tokens), ("out", output_tokens)):
            if qty <= 0:
                continue
            unit = rate["input" if direction == "in" else "output"] / 1_000_000
            ev = UsageEventRecord(id=new_id(), tenant_id=team, source_module=agent_id,
                                  resource_type=f"{kind}:{m}:{direction}", quantity=float(qty), unit_cost=unit, cost=qty * unit)
            await self.repository.create_usage_event(ev)
            cost += ev.cost
        if fingerprint:
            self._call_fingerprints[f"{team}|{agent_id}|{fingerprint}"] += 1
        self.repository.mark_dirty()
        return cost

    async def _period_total(self, team: str, period: BudgetPeriod) -> float:
        start, end = period_window(period, datetime.now(UTC))
        return await self.repository.sum_usage_cost(tenant_id=team, start=start, end=end)

    def _policy_for(self, team: str) -> BudgetPolicyRecord | None:
        policies = [p for p in self.repository.budget_policies.values() if p.tenant_id == team]
        return max(policies, key=lambda p: p.created_at) if policies else None

    async def is_blocked(self, team: str | None) -> tuple[bool, str]:
        """Guardrail check used before every agent LLM call."""
        await self.boot()
        team = team or PLATFORM_TEAM
        policy = self._policy_for(team)
        if policy is None or self.repository.extras.get("enforcement", {}).get(policy.id) != "block":
            return False, ""
        spent = await self._period_total(team, policy.period)
        if spent >= policy.limit_amount:
            return True, (f"FinOps budget exhausted for team '{team}': ${spent:.4f} of ${policy.limit_amount:.2f} "
                          f"({policy.period.value}). Ask an admin to raise the budget or switch it to alert mode.")
        return False, ""

    # --- reporting --------------------------------------------------------------------------------
    async def _team_report(self, team: str, period: BudgetPeriod) -> dict[str, Any]:
        policy = self._policy_for(team)
        report = await self.aggregation.cost_report(tenant_id=team, period=period, budget_policy=policy)
        forecast = self.forecasting.forecast(period=period, total_cost_so_far=report.total_cost)
        start, end = period_window(period, datetime.now(UTC))
        events = [e for e in self.repository.usage_events if e.tenant_id == team and start <= e.occurred_at < end]
        by_agent: dict[str, float] = defaultdict(float)
        by_model: dict[str, dict[str, float]] = defaultdict(lambda: {"cost": 0.0, "input_tokens": 0.0, "output_tokens": 0.0})
        for e in events:
            kind, model, direction = (e.resource_type.split(":") + ["", "", ""])[:3]
            by_agent[e.source_module] += e.cost
            row = by_model[model]
            row["cost"] += e.cost
            row["input_tokens" if direction == "in" else "output_tokens"] += e.quantity
        enforcement = self.repository.extras.get("enforcement", {}).get(policy.id, "alert") if policy else None
        return {
            "team": team,
            "period": period.value,
            "total_cost": round(report.total_cost, 6),
            "forecast": round(forecast, 6) if forecast is not None else None,
            "budget": None if policy is None else {
                "policy_id": policy.id, "limit": policy.limit_amount, "alert_threshold": round(policy.alert_threshold_pct, 3),
                "enforcement": enforcement, "utilisation": round(report.utilisation_ratio or 0.0, 4),
                "alert": report.alert, "blocked": (await self.is_blocked(team))[0],
            },
            "tokens": {"input": int(sum(r["input_tokens"] for r in by_model.values())),
                       "output": int(sum(r["output_tokens"] for r in by_model.values()))},
            "by_agent": sorted(({"agent": a, "cost": round(c, 6)} for a, c in by_agent.items()), key=lambda r: -r["cost"]),
            "by_model": sorted(({"model": m, **{k: round(v, 6) for k, v in r.items()}, "rate": self.rate_for(m)} for m, r in by_model.items()),
                               key=lambda r: -r["cost"]),
            "events": len(events),
        }

    def _timeseries(self, teams: set[str], days: int = 14) -> list[dict[str, Any]]:
        today = datetime.now(UTC).date()
        buckets = {today - timedelta(days=i): 0.0 for i in range(days)}
        for e in self.repository.usage_events:
            d = e.occurred_at.date()
            if e.tenant_id in teams and d in buckets:
                buckets[d] += e.cost
        return [{"day": d.isoformat(), "cost": round(c, 6)} for d, c in sorted(buckets.items())]

    def _teams(self) -> set[str]:
        return {e.tenant_id for e in self.repository.usage_events} | {p.tenant_id for p in self.repository.budget_policies.values()}

    @operation("Spend report: totals, forecast, budget status and breakdown by agent/model (workers: own team).")
    async def report(self, ctx: CallContext, team: str | None = None, period: str = "monthly") -> dict[str, Any]:
        if not ctx.is_admin:
            if team and team != ctx.team:
                raise PermissionError(f"workers can only read their own team's spend ({ctx.team!r})")
            team = ctx.team or PLATFORM_TEAM
        if self.repository._dirty_since:
            self.repository.flush()
        per = BudgetPeriod(period)
        teams = [team] if team else sorted(self._teams()) or [PLATFORM_TEAM]
        reports = [await self._team_report(t, per) for t in teams]
        return {
            "period": per.value,
            "total_cost": round(sum(r["total_cost"] for r in reports), 6),
            "forecast": round(sum(r["forecast"] or 0.0 for r in reports), 6),
            "teams": reports,
            "timeseries": self._timeseries(set(teams)),
            "estimated_rates": any(r2["rate"].get("estimate") for r in reports for r2 in r["by_model"]),
        }

    @operation("Is this team allowed to make LLM calls right now? (budget guardrail)")
    async def check(self, ctx: CallContext, team: str | None = None) -> dict[str, Any]:
        target = team if ctx.is_admin and team else (ctx.team or PLATFORM_TEAM)
        blocked, reason = await self.is_blocked(target)
        return {"team": target, "allowed": not blocked, "reason": reason}

    @operation("Create/replace a team budget: limit in USD, period, alert threshold, enforcement alert|block.", permission="admin")
    async def set_budget(self, ctx: CallContext, team: str, limit_usd: float, period: str = "monthly",
                         alert_threshold: float = 0.8, enforcement: str = "alert") -> dict[str, Any]:
        if enforcement not in ("alert", "block"):
            raise ValueError("enforcement must be 'alert' or 'block'")
        if limit_usd <= 0:
            raise ValueError("limit_usd must be positive")
        policy = await self.budgets.create(tenant_id=team, period=BudgetPeriod(period), limit_amount=float(limit_usd),
                                           alert_threshold_pct=float(alert_threshold))
        self.repository.extras.setdefault("enforcement", {})[policy.id] = enforcement
        self.repository.flush()
        return {"policy_id": policy.id, "team": team, "limit": policy.limit_amount, "period": policy.period.value,
                "alert_threshold": policy.alert_threshold_pct, "enforcement": enforcement}

    @operation("Rate card (USD per 1M tokens) with estimate flags.")
    async def rates(self, ctx: CallContext) -> dict[str, Any]:
        models_seen = {e.resource_type.split(":")[1] for e in self.repository.usage_events if e.resource_type.count(":") >= 2}
        names = sorted(set(DEFAULT_RATES) | set(self.repository.extras.get("rates", {})) | models_seen)
        return {"rates": [self.rate_for(m) for m in names], "currency": "USD", "unit": "per 1M tokens"}

    @operation("Set a confirmed per-model rate (USD per 1M input/output tokens).", permission="admin")
    async def set_rate(self, ctx: CallContext, model: str, input_per_mtok: float, output_per_mtok: float) -> dict[str, Any]:
        m = normalize_model(model)
        self.repository.extras.setdefault("rates", {})[m] = {"input": float(input_per_mtok), "output": float(output_per_mtok),
                                                              "estimate": False, "note": f"set by {ctx.agent_id}"}
        self.repository.flush()
        return self.rate_for(m)

    @operation("Run the cost-optimisation agent for a team (tightens alerting when the forecast overruns).", permission="admin")
    async def optimise(self, ctx: CallContext, team: str) -> dict[str, Any]:
        policy = self._policy_for(team)
        if policy is None:
            return {"team": team, "action": None, "reason": "no budget policy"}
        action = await self.optimiser.evaluate(policy)
        actions, total = await self.repository.list_optimisation_actions(budget_policy_id=policy.id)
        return {"team": team, "action": action, "history": actions[-10:], "total_actions": total}

    @operation("Token-optimisation recommendations derived from metered usage.")
    async def recommendations(self, ctx: CallContext, team: str | None = None) -> dict[str, Any]:
        target = team if ctx.is_admin else (ctx.team or PLATFORM_TEAM)
        teams = [target] if target else sorted(self._teams())
        recs: list[dict[str, Any]] = []
        for t in teams:
            rep = await self._team_report(t, BudgetPeriod.MONTHLY)
            for m in rep["by_model"]:
                if m["input_tokens"] > 8 * max(1.0, m["output_tokens"]) and m["input_tokens"] > 20_000:
                    recs.append({"team": t, "severity": "medium", "kind": "context_compression",
                                 "message": f"{m['model']}: prompts are {m['input_tokens'] / max(1, m['output_tokens']):.0f}× larger than outputs — "
                                            "trim history/context (short-term memory summaries, RAG token budget)."})
                if "lite" not in m["model"] and m["model"].startswith("gemini") and m["cost"] > 0.05:
                    lite = self.rate_for(m["model"].split("-flash")[0] + "-flash-lite") if "-flash" in m["model"] else None
                    if lite and lite["input"] < m["rate"]["input"]:
                        recs.append({"team": t, "severity": "low", "kind": "right_size_model",
                                     "message": f"Route classification/extraction calls from {m['model']} to a flash-lite tier "
                                                f"(≈{lite['input'] / m['rate']['input']:.0%} of the input price)."})
            if rep["budget"] and rep["forecast"] and rep["forecast"] > rep["budget"]["limit"]:
                recs.append({"team": t, "severity": "high", "kind": "forecast_overrun",
                             "message": f"Forecast ${rep['forecast']:.2f} exceeds the ${rep['budget']['limit']:.2f} budget — "
                                        "run optimise or switch enforcement to block."})
        dupes = [(k, n) for k, n in self._call_fingerprints.items() if n >= 3 and (not target or k.startswith(f"{target}|"))]
        for key, n in sorted(dupes, key=lambda kv: -kv[1])[:5]:
            t, agent, _ = key.split("|", 2)
            recs.append({"team": t, "severity": "low", "kind": "cache",
                         "message": f"{agent} sent the same request {n}× — cache responses (semantic cache) for repeated calls."})
        return {"recommendations": recs}

    def summarize(self, op_name: str, data: Any) -> str:
        if op_name == "report" and isinstance(data, dict):
            return f"${data['total_cost']:.4f} spent, forecast ${data['forecast']:.4f}"
        if op_name == "check" and isinstance(data, dict):
            return "allowed" if data["allowed"] else "BLOCKED"
        return super().summarize(op_name, data)
