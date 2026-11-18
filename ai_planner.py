"""PawPal+ AI Care Planner.

Turns an owner's natural-language context into priority recommendations, validates
them strictly, and hands them to the existing deterministic Scheduler.

Flow:
    Owner + owner text -> build_context() -> AI client -> parse_and_validate()
    -> apply_recommendation() on a deepcopy -> Scheduler -> AIPlanResult

The AI only recommends. It may raise or lower the priority of existing pending
tasks (never lowering a task the owner marked "high") and may shrink the time
budget. Any invalid response, or any client/API failure, falls back to the
unchanged Scheduler on the original Owner.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from pawpal_system import PRIORITY_RANKS, Owner, Pet, Scheduler, Task

logger = logging.getLogger("pawpal.ai_planner")

# An AI client is any callable taking (system_prompt, user_prompt) and returning
# the raw response text. Tests pass simple fakes; AnthropicCareClient is the real one.
AIClient = Callable[[str, str], str]

DEFAULT_MODEL = "claude-opus-5-5"

TOP_LEVEL_KEYS = {"available_minutes", "adjustments", "summary"}
ADJUSTMENT_KEYS = {"task_id", "pet", "new_priority", "reason"}

SYSTEM_PROMPT = """You are the planning assistant for PawPal+, a pet-care scheduler.
You receive the owner's pets, today's pending care tasks (each with an id like "t1"),
the owner's available minutes, and a short note from the owner.

Recommend priority changes that reflect the owner's note. Rules:
- Only refer to tasks by the ids you were given. Never invent tasks or pets.
- new_priority must be "low", "medium", or "high".
- Never lower a task whose current priority is "high".
- You cannot change durations, times, recurrence, or completion, and cannot add or remove tasks.
- Set available_minutes only if the note says the owner has LESS time than available_minutes;
  otherwise use null. Never exceed the given available_minutes.
- Give a short reason for each adjustment and a one-sentence summary.
- An empty adjustments list is fine if nothing should change.
A deterministic scheduler will build the final plan from your recommendation."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "available_minutes": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "adjustments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "pet": {"type": "string"},
                    "new_priority": {"type": "string", "enum": list(PRIORITY_RANKS)},
                    "reason": {"type": "string"},
                },
                "required": sorted(ADJUSTMENT_KEYS),
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": sorted(TOP_LEVEL_KEYS),
    "additionalProperties": False,
}


class RecommendationError(ValueError):
    """Raised when AI output does not satisfy the validation contract."""


class AIUnavailableError(RuntimeError):
    """Raised with a user-safe message when the AI cannot be used (e.g. no API key)."""


@dataclass
class Adjustment:
    """One validated priority change for an existing task."""

    task_id: str
    pet: str
    new_priority: str
    reason: str


@dataclass
class Recommendation:
    """A fully validated AI recommendation."""

    available_minutes: int | None
    adjustments: list[Adjustment]
    summary: str


@dataclass
class AIPlanResult:
    """The final plan plus how it was produced."""

    plan: list[tuple[Pet, Task]]
    skipped: list[tuple[Pet, Task]]
    explanation: str
    used_ai: bool
    effective_minutes: int
    summary: str = ""
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- Context building ---


def build_task_index(owner: Owner) -> dict[str, tuple[int, int]]:
    """Give each task the Scheduler could plan today a short id: "t1" -> (pet index, task index).

    Positions (not objects) are stored so the same ids also locate tasks in a deepcopy.
    """
    today = date.today()
    index: dict[str, tuple[int, int]] = {}
    for pet_pos, pet in enumerate(owner.pets):
        for task_pos, task in enumerate(pet.tasks):
            if not task.completed and task.due_date <= today:
                index[f"t{len(index) + 1}"] = (pet_pos, task_pos)
    return index


def build_context(owner: Owner, index: dict[str, tuple[int, int]]) -> dict[str, Any]:
    """Return the data sent to the AI: time budget, pets, and today's pending tasks."""
    pets = [{"name": pet.name, "species": pet.species, "tasks": []} for pet in owner.pets]
    for task_id, (pet_pos, task_pos) in index.items():
        task = owner.pets[pet_pos].tasks[task_pos]
        pets[pet_pos]["tasks"].append(
            {
                "id": task_id,
                "title": task.title,
                "duration_minutes": task.duration_minutes,
                "priority": task.priority,
                "time": task.time,
                "frequency": task.frequency,
            }
        )
    return {"available_minutes": owner.available_minutes, "pets": pets}


def build_user_prompt(context: dict[str, Any], owner_text: str) -> str:
    """Combine the structured context and the owner's note into one prompt."""
    return (
        "Pets and today's pending tasks:\n"
        f"{json.dumps(context, indent=2)}\n\n"
        f"Owner's note:\n{owner_text.strip()}"
    )


# --- Validation ---


def _require_keys(obj: dict, expected: set[str], where: str) -> None:
    missing = expected - obj.keys()
    extra = obj.keys() - expected
    if missing:
        raise RecommendationError(f"{where} is missing field(s): {sorted(missing)}")
    if extra:
        raise RecommendationError(f"{where} has unexpected field(s): {sorted(extra)}")


def parse_and_validate(
    raw: Any, owner: Owner, index: dict[str, tuple[int, int]]
) -> Recommendation:
    """Parse raw AI text and enforce the full contract, or raise RecommendationError."""
    if not isinstance(raw, str):
        raise RecommendationError(f"AI response must be text, got {type(raw).__name__}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecommendationError(f"AI response is not valid JSON ({exc.msg})") from None

    if not isinstance(data, dict):
        raise RecommendationError("AI response must be a JSON object")
    _require_keys(data, TOP_LEVEL_KEYS, "AI response")

    minutes = data["available_minutes"]
    if minutes is not None:
        if type(minutes) is not int:  # rejects bools, floats, and strings
            raise RecommendationError("available_minutes must be an integer or null")
        if not 1 <= minutes <= owner.available_minutes:
            raise RecommendationError(
                f"available_minutes {minutes} is outside 1..{owner.available_minutes} "
                "(the AI may only reduce the owner's time)"
            )

    if not isinstance(data["summary"], str):
        raise RecommendationError("summary must be a string")

    if not isinstance(data["adjustments"], list):
        raise RecommendationError("adjustments must be a list")

    adjustments: list[Adjustment] = []
    seen: set[str] = set()
    for number, item in enumerate(data["adjustments"], start=1):
        where = f"adjustment #{number}"
        if not isinstance(item, dict):
            raise RecommendationError(f"{where} must be an object")
        _require_keys(item, ADJUSTMENT_KEYS, where)
        for key in ADJUSTMENT_KEYS:
            if not isinstance(item[key], str):
                raise RecommendationError(f"{where}: {key} must be a string")

        task_id, pet_name, new_priority = item["task_id"], item["pet"], item["new_priority"]
        if task_id not in index:
            raise RecommendationError(f"{where}: unknown task_id '{task_id}'")
        if task_id in seen:
            raise RecommendationError(f"{where}: task_id '{task_id}' appears more than once")
        seen.add(task_id)

        pet_pos, task_pos = index[task_id]
        pet = owner.pets[pet_pos]
        task = pet.tasks[task_pos]
        if pet_name.lower() != pet.name.lower():
            raise RecommendationError(
                f"{where}: task_id '{task_id}' belongs to {pet.name}, not '{pet_name}'"
            )
        if new_priority not in PRIORITY_RANKS:
            raise RecommendationError(f"{where}: invalid priority '{new_priority}'")
        if task.priority.lower() == "high" and new_priority != "high":
            raise RecommendationError(
                f"{where}: cannot lower owner-set high priority of '{task.title}' ({pet.name})"
            )

        adjustments.append(Adjustment(task_id, pet.name, new_priority, item["reason"]))

    return Recommendation(minutes, adjustments, data["summary"])


# --- Applying and planning ---


def apply_recommendation(
    owner: Owner, index: dict[str, tuple[int, int]], rec: Recommendation
) -> Owner:
    """Return a deepcopy of the owner with the recommendation applied. The original is untouched."""
    planning_owner = copy.deepcopy(owner)
    if rec.available_minutes is not None:
        planning_owner.available_minutes = min(rec.available_minutes, owner.available_minutes)
    for adj in rec.adjustments:
        pet_pos, task_pos = index[adj.task_id]
        planning_owner.pets[pet_pos].tasks[task_pos].priority = adj.new_priority
    return planning_owner


def _fallback(owner: Owner, warning: str) -> AIPlanResult:
    """Run the unchanged Scheduler on the original owner."""
    logger.warning("Fallback used: deterministic scheduler without AI (%s)", warning)
    scheduler = Scheduler(owner)
    plan = scheduler.generate_plan()
    return AIPlanResult(
        plan=plan,
        skipped=list(scheduler.skipped_tasks),
        explanation=scheduler.explain_plan(),
        used_ai=False,
        effective_minutes=owner.available_minutes,
        warnings=[warning],
    )


def plan_with_ai(owner: Owner, owner_text: str, client: AIClient | None = None) -> AIPlanResult:
    """Build today's plan using AI recommendations, falling back safely on any problem."""
    if not owner_text or not owner_text.strip():
        return _fallback(owner, "No owner context was provided, so the standard plan was used.")

    index = build_task_index(owner)
    user_prompt = build_user_prompt(build_context(owner, index), owner_text)

    try:
        if client is None:
            client = AnthropicCareClient()
        logger.info("AI request attempted (%d pending task(s) sent)", len(index))
        raw = client(SYSTEM_PROMPT, user_prompt)
    except Exception as exc:  # any client/API/network failure must not break planning
        logger.error("AI request failed: %s: %s", type(exc).__name__, exc)
        reason = str(exc) if isinstance(exc, AIUnavailableError) else type(exc).__name__
        return _fallback(owner, f"AI was unavailable ({reason}), so the standard plan was used.")

    logger.debug("Raw AI response: %r", raw)
    try:
        rec = parse_and_validate(raw, owner, index)
    except RecommendationError as exc:
        logger.warning("AI recommendation rejected by validation: %s", exc)
        return _fallback(owner, f"AI output failed validation ({exc}), so the standard plan was used.")

    logger.info(
        "AI recommendation validated: %d adjustment(s), available_minutes=%s",
        len(rec.adjustments),
        rec.available_minutes,
    )

    planning_owner = apply_recommendation(owner, index, rec)
    scheduler = Scheduler(planning_owner)
    plan = scheduler.generate_plan()

    reasons = []
    for adj in rec.adjustments:
        pet_pos, task_pos = index[adj.task_id]
        old = owner.pets[pet_pos].tasks[task_pos].priority
        title = owner.pets[pet_pos].tasks[task_pos].title
        reasons.append(f"'{title}' ({adj.pet}): {old} -> {adj.new_priority}. {adj.reason}")
    if planning_owner.available_minutes < owner.available_minutes:
        reasons.append(
            f"Time budget reduced from {owner.available_minutes} to "
            f"{planning_owner.available_minutes} minutes based on your note."
        )

    header = ["AI recommendations (validated, then scheduled by the standard rules):"]
    header += [f"- {reason}" for reason in reasons] or ["- No changes recommended."]
    explanation = "\n".join(header) + "\n\n" + scheduler.explain_plan()

    return AIPlanResult(
        plan=plan,
        skipped=list(scheduler.skipped_tasks),
        explanation=explanation,
        used_ai=True,
        effective_minutes=planning_owner.available_minutes,
        summary=rec.summary,
        reasons=reasons,
    )


# --- Real AI client (the only code that touches the network) ---


class AnthropicCareClient:
    """Calls Claude with a JSON schema. The key is read only from the ANTHROPIC_API_KEY
    environment variable and is never logged or displayed."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise AIUnavailableError("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic  # imported lazily so the app and tests work without the SDK
        except ImportError:
            raise AIUnavailableError("the anthropic package is not installed") from None

        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        response = self._client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
            output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
            # If Claude Opus 5.5 declines, the API retries on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model refused the request")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("model response was truncated")
        return next(block.text for block in response.content if block.type == "text")
