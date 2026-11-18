"""Reliability tests for the AI Care Planner. All AI clients here are fakes: no network, no API key."""

import json
import logging

import pytest

import ai_planner
from ai_planner import build_task_index, plan_with_ai
from pawpal_system import Owner, Pet, Scheduler, Task

NOTE = "Biscuit has a lot of energy today and Mochi needs her regular medication. I only have 45 minutes tonight."


def make_owner(minutes=45):
    """Two pets with tasks due today (the same data as main.py, minus the extras)."""
    owner = Owner(name="Jordan", available_minutes=minutes)
    biscuit = Pet(name="Biscuit", species="Dog")
    mochi = Pet(name="Mochi", species="Cat")
    owner.add_pet(biscuit)
    owner.add_pet(mochi)
    biscuit.add_task(Task("Morning walk", 30, "high", time="07:00", frequency="daily"))
    biscuit.add_task(Task("Feeding", 10, "high", time="12:30"))
    biscuit.add_task(Task("Fetch in the yard", 40, "low", time="17:00"))
    mochi.add_task(Task("Feeding", 5, "high", time="06:45"))
    mochi.add_task(Task("Give medication", 5, "medium", time="12:30"))
    return owner


def task_id(owner, pet_name, title):
    """Look up the short id the planner gives a task."""
    for tid, (pet_pos, task_pos) in build_task_index(owner).items():
        pet = owner.pets[pet_pos]
        if pet.name == pet_name and pet.tasks[task_pos].title == title:
            return tid
    raise LookupError(f"{pet_name}/{title}")


def fake_client(payload):
    """A fake AI client that returns the payload (dicts are JSON-encoded, strings sent as-is)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return lambda system_prompt, user_prompt: text


def response(adjustments=(), minutes=None, summary="ok"):
    return {"available_minutes": minutes, "adjustments": list(adjustments), "summary": summary}


def adjustment(tid, pet, priority, reason="because"):
    return {"task_id": tid, "pet": pet, "new_priority": priority, "reason": reason}


def titles(pairs):
    return [(pet.name, task.title) for pet, task in pairs]


def baseline_titles(owner):
    return titles(Scheduler(owner).generate_plan())


def assert_fell_back(result, owner):
    """The result must be exactly the unchanged deterministic plan on the original owner."""
    assert result.used_ai is False
    assert result.warnings
    assert result.effective_minutes == owner.available_minutes
    assert titles(result.plan) == baseline_titles(owner)


# --- Valid recommendations ---


def test_valid_recommendation_changes_scheduling_behavior():
    owner = make_owner(45)
    # Without AI, the 45 minutes go to the three high-priority tasks and medication is skipped.
    assert ("Mochi", "Give medication") not in baseline_titles(owner)

    med = task_id(owner, "Mochi", "Give medication")
    result = plan_with_ai(owner, NOTE, fake_client(response([adjustment(med, "Mochi", "high")])))

    assert result.used_ai is True
    assert result.warnings == []
    assert ("Mochi", "Give medication") in titles(result.plan)
    assert ("Biscuit", "Morning walk") in titles(result.skipped)  # Scheduler still enforces time
    assert sum(task.duration_minutes for _, task in result.plan) <= 45
    assert "medium -> high" in result.reasons[0]
    assert "AI recommendations" in result.explanation


def test_original_owner_and_tasks_are_unchanged_after_ai_planning():
    owner = make_owner(60)
    before = [
        (pet.name, task.title, task.priority, task.duration_minutes, task.time, task.completed)
        for pet, task in owner.get_all_tasks()
    ]
    med = task_id(owner, "Mochi", "Give medication")
    fetch = task_id(owner, "Biscuit", "Fetch in the yard")
    client = fake_client(
        response([adjustment(med, "Mochi", "high"), adjustment(fetch, "Biscuit", "high")], minutes=30)
    )

    result = plan_with_ai(owner, NOTE, client)

    after = [
        (pet.name, task.title, task.priority, task.duration_minutes, task.time, task.completed)
        for pet, task in owner.get_all_tasks()
    ]
    assert result.used_ai is True
    assert after == before
    assert owner.available_minutes == 60
    # Planned tasks are copies, not the user's real Task objects.
    real_tasks = {id(task) for _, task in owner.get_all_tasks()}
    assert all(id(task) not in real_tasks for _, task in result.plan)


def test_valid_empty_adjustments_are_accepted():
    owner = make_owner(45)

    result = plan_with_ai(owner, "Normal day.", fake_client(response([], summary="Nothing to change.")))

    assert result.used_ai is True
    assert result.warnings == []
    assert result.summary == "Nothing to change."
    assert titles(result.plan) == baseline_titles(owner)


def test_ai_may_reduce_available_time():
    owner = make_owner(60)

    result = plan_with_ai(owner, "I only have 30 minutes.", fake_client(response(minutes=30)))

    assert result.used_ai is True
    assert result.effective_minutes == 30
    assert sum(task.duration_minutes for _, task in result.plan) <= 30
    assert ("Biscuit", "Morning walk") in titles(result.skipped)
    assert "30 minutes available" in result.explanation
    assert owner.available_minutes == 60


def test_ai_cannot_increase_available_time_beyond_owner_setting():
    owner = make_owner(45)

    result = plan_with_ai(owner, "I have two hours.", fake_client(response(minutes=120)))

    assert_fell_back(result, owner)
    assert result.effective_minutes == 45
    assert "available_minutes" in result.warnings[0]


# --- Invalid AI output: whole recommendation rejected ---


def test_malformed_json_falls_back():
    owner = make_owner()

    result = plan_with_ai(owner, NOTE, fake_client("Sure! Here's the plan: {not json"))

    assert_fell_back(result, owner)
    assert "not valid JSON" in result.warnings[0]


def test_nonexistent_task_id_falls_back():
    owner = make_owner()

    result = plan_with_ai(owner, NOTE, fake_client(response([adjustment("t99", "Mochi", "high")])))

    assert_fell_back(result, owner)
    assert "unknown task_id" in result.warnings[0]


def test_invalid_priority_falls_back():
    owner = make_owner()
    med = task_id(owner, "Mochi", "Give medication")

    result = plan_with_ai(owner, NOTE, fake_client(response([adjustment(med, "Mochi", "urgent")])))

    assert_fell_back(result, owner)
    assert "invalid priority" in result.warnings[0]


def test_lowering_owner_set_high_priority_falls_back():
    owner = make_owner()
    walk = task_id(owner, "Biscuit", "Morning walk")

    result = plan_with_ai(owner, NOTE, fake_client(response([adjustment(walk, "Biscuit", "low")])))

    assert_fell_back(result, owner)
    assert "cannot lower" in result.warnings[0]


def test_one_bad_adjustment_rejects_the_whole_recommendation():
    owner = make_owner()
    med = task_id(owner, "Mochi", "Give medication")
    good = adjustment(med, "Mochi", "high")
    bad = adjustment("t42", "Biscuit", "high")

    result = plan_with_ai(owner, NOTE, fake_client(response([good, bad])))

    assert_fell_back(result, owner)
    assert ("Mochi", "Give medication") not in titles(result.plan)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ("[]", "JSON object"),
        ({"adjustments": [], "summary": "x"}, "missing"),  # missing available_minutes
        ({**response(), "notes": "extra"}, "unexpected"),
        (response(minutes="30"), "integer"),
        (response(minutes=True), "integer"),
        (response(minutes=0), "outside"),
        ({**response(), "adjustments": "none"}, "must be a list"),
        ({**response(), "summary": 5}, "summary"),
        (response([{"task_id": "t1", "pet": "Biscuit", "new_priority": "high"}]), "missing"),
        (response([{**adjustment("t1", "Biscuit", "high"), "duration_minutes": 1}]), "unexpected"),
        (response([adjustment("t1", "Rex", "high")]), "belongs to"),  # nonexistent pet
        (response([adjustment("t5", "Biscuit", "high")]), "belongs to"),  # t5 is Mochi's
        (response([adjustment(5, "Mochi", "high")]), "must be a string"),
        (response([adjustment("t5", "Mochi", "high")] * 2), "more than once"),
    ],
)
def test_other_schema_violations_fall_back(payload, expected):
    owner = make_owner()

    result = plan_with_ai(owner, NOTE, fake_client(payload))

    assert_fell_back(result, owner)
    assert expected in result.warnings[0]


# --- AI unavailable ---


def test_api_client_failure_falls_back():
    owner = make_owner()

    def broken_client(system_prompt, user_prompt):
        raise ConnectionError("network down")

    result = plan_with_ai(owner, NOTE, broken_client)

    assert_fell_back(result, owner)
    assert "unavailable" in result.warnings[0]


def test_missing_ai_sdk_or_credentials_falls_back(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ImportError("No module named 'anthropic'")

    monkeypatch.setattr(ai_planner, "AnthropicCareClient", unavailable)
    owner = make_owner()

    result = plan_with_ai(owner, NOTE)  # no client injected -> default client cannot be built

    assert_fell_back(result, owner)


def test_missing_api_key_falls_back_without_calling_the_api(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    owner = make_owner()

    result = plan_with_ai(owner, NOTE)  # real client path; it must stop before any network call

    assert_fell_back(result, owner)
    assert "ANTHROPIC_API_KEY is not set" in result.warnings[0]


def test_deterministic_behavior_unchanged_when_ai_unavailable():
    owner = make_owner(45)
    expected = Scheduler(owner)
    expected_plan = titles(expected.generate_plan())

    def broken_client(system_prompt, user_prompt):
        raise TimeoutError

    result = plan_with_ai(owner, NOTE, broken_client)

    assert titles(result.plan) == expected_plan
    assert titles(result.skipped) == titles(expected.skipped_tasks)
    assert result.explanation == expected.explain_plan()


def test_blank_owner_text_skips_ai_call():
    owner = make_owner()

    def must_not_be_called(system_prompt, user_prompt):
        raise AssertionError("AI should not be called without owner context")

    result = plan_with_ai(owner, "   ", must_not_be_called)

    assert_fell_back(result, owner)


# --- Context and logging ---


def test_context_only_lists_pending_tasks_due_today_with_ids():
    owner = make_owner()
    done = owner.pets[0].tasks[1]
    owner.pets[0].complete_task(done)  # Biscuit's Feeding is done
    owner.pets[0].complete_task(owner.pets[0].tasks[0])  # walk done; tomorrow's copy added
    sent = {}

    def capture(system_prompt, user_prompt):
        sent["prompt"] = user_prompt
        return json.dumps(response())

    plan_with_ai(owner, NOTE, capture)

    assert '"id": "t1"' in sent["prompt"]
    assert "Fetch in the yard" in sent["prompt"]
    assert "Morning walk" not in sent["prompt"]  # done today; tomorrow's copy not due yet
    assert '"duration_minutes": 10' not in sent["prompt"]  # Biscuit's completed Feeding
    assert NOTE in sent["prompt"]


def test_validation_rejection_and_fallback_are_logged(caplog):
    owner = make_owner()

    with caplog.at_level(logging.INFO, logger="pawpal.ai_planner"):
        plan_with_ai(owner, NOTE, fake_client("not json"))

    messages = [record.getMessage() for record in caplog.records]
    assert any("AI request attempted" in m for m in messages)
    assert any("rejected by validation" in m for m in messages)
    assert any("Fallback used" in m for m in messages)
