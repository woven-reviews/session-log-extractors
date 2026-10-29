"""Contract tests for complete interpreted sessions and lossy JSON export."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from session_log_extractors.claude_sources import read_source
from session_log_extractors.session_log_envelope import (
    build_envelope,
    export_session,
    image_from_bytes,
    unavailable_image,
)
from session_log_extractors.session_model import (
    AuthorshipMarker,
    AuthorshipRange,
    Event,
    Image,
    Question,
    QuestionActivityReference,
    QuestionAssociation,
    QuestionChoice,
    QuestionEvidence,
    QuestionRefusal,
    QuestionResponse,
    QuestionSelection,
    Session,
    ToolCall,
    UserMessage,
)

ROOT = Path(__file__).parent / "fixtures" / "claude" / "2.1.312"
(TRANSCRIPT,) = (ROOT / "native").glob("*.jsonl")


def test_question_occurrences_and_response_states_are_independent():
    # Illustrative boundaries absent from native captures: repeated identical
    # questions, a recommendation, a refusal, and an ambiguous orphan response.
    option = QuestionChoice(
        ref="o1",
        label="A (Recommended)",
        description="First choice",
        recommendation_markers=[
            QuestionEvidence(field="label", value="A (Recommended)")
        ],
    )
    questions = [
        Question(ref=ref, text="Choose?", options=[option])
        for ref in ("q1", "q2", "q3")
    ]
    questions[0]["native_id"] = "native-q1"
    responses = [
        QuestionResponse(
            ref="r1",
            outcome="answered",
            question=QuestionAssociation(
                question_ref="q1",
                evidence=[
                    QuestionEvidence(field="answer.question_id", value="native-q1"),
                    QuestionEvidence(field="questions[0].id", value="native-q1"),
                ],
            ),
            selections=[QuestionSelection(label="B"), QuestionSelection(label="C")],
            free_text="Also keep this exact text",
        ),
        QuestionResponse(
            ref="r2",
            outcome="refused",
            question=None,
            refusal=QuestionRefusal(by="candidate", explanation="Prompt dismissed"),
            candidate_feedback="Please ask later",
        ),
        QuestionResponse(ref="r3", outcome="unknown", question=None),
    ]
    session = Session(
        harness="test",
        session_id="s",
        events=[],
        interaction_questions=questions,
        question_responses=responses,
    )
    decoded = json.loads(json.dumps(session))
    assert len({q["ref"] for q in decoded["interaction_questions"]}) == 3
    assert all(q["text"] == "Choose?" for q in questions)
    assert responses[0]["selections"] == [{"label": "B"}, {"label": "C"}]
    assert responses[0]["free_text"] == "Also keep this exact text"
    assert "selections" not in responses[1]
    assert responses[1]["candidate_feedback"] != responses[1]["refusal"]["explanation"]
    assert responses[1]["question"] is None and responses[2]["question"] is None
    assert {
        r["question"]["question_ref"] for r in responses if r["question"] is not None
    } == {"q1"}
    assert "selected" not in option and "native_id" not in questions[1]


def _codex_question_contract():
    records = _native_records("codex/question.jsonl")
    request = next(
        r["payload"] for r in records if r["payload"].get("type") == "function_call"
    )
    result = next(
        r["payload"]
        for r in records
        if r["payload"].get("type") == "function_call_output"
    )
    native = json.loads(request["arguments"])["questions"][0]
    answers = json.loads(result["output"])["answers"]
    evidence = [
        QuestionEvidence(field="function_call.call_id", value=request["call_id"]),
        QuestionEvidence(field="function_call_output.call_id", value=result["call_id"]),
        QuestionEvidence(field="questions[0].id", value=native["id"]),
        QuestionEvidence(field="answers key", value=next(iter(answers))),
    ]
    question = Question(
        ref="q1",
        native_id=native["id"],
        text=native["question"],
        header=native["header"],
        options=[
            QuestionChoice(ref=f"o{i}", **o) for i, o in enumerate(native["options"])
        ],
        activity=QuestionActivityReference(
            message_i=0, tool_call_index=0, evidence=evidence[:1]
        ),
    )
    response = QuestionResponse(
        ref="r1",
        outcome="answered",
        question=QuestionAssociation(question_ref=question["ref"], evidence=evidence),
        selections=[
            QuestionSelection(label=a) for a in answers[native["id"]]["answers"]
        ],
        activity=QuestionActivityReference(
            message_i=0, tool_call_index=0, evidence=evidence[:2]
        ),
    )
    call = ToolCall(
        name=request["name"],
        id=request["call_id"],
        input=json.loads(request["arguments"]),
        result=result["output"],
    )
    return question, response, call


@pytest.mark.parametrize("limit", [0, 8, -1])
def test_native_question_facts_survive_export_policy(limit):
    question, response, call = _codex_question_contract()
    session = Session(
        harness="codex",
        session_id="s",
        events=[Event(i=0, role="assistant", tool_calls=[call])],
        interaction_questions=[question],
        question_responses=[response],
    )
    original = deepcopy(session)
    exported = export_session(session, limit)
    actual = exported["events"][0]["tool_calls"][0]
    assert actual["input"] == call["input"]
    assert actual["result_truncated"] is (limit >= 0)
    assert actual["result"] == (call["result"] if limit < 0 else call["result"][:limit])
    assert response["selections"] == [{"label": "QUAL"}]
    assert question["native_id"] == "fixture_owner"
    assert question["activity"]["message_i"] == session["events"][0]["i"]
    assert "interaction_questions" not in exported
    assert "question_responses" not in exported
    assert session == original
    actual["input"]["questions"].clear()
    assert session == original


def test_claude_fixture_supports_call_and_explicit_question_text_relationship():
    records = _native_records("claude/questions.jsonl")
    request = records[1]["message"]["content"][0]
    result = records[2]["message"]["content"][0]
    native = request["input"]["questions"][0]
    assert result["tool_use_id"] == request["id"]
    assert (
        f'"{native["question"]}"="section, manufacturer_part_no"' in result["content"]
    )
    question = Question(
        ref="q1",
        text=native["question"],
        header=native["header"],
        multiple_selections=native["multiSelect"],
        options=[
            QuestionChoice(ref=f"o{i}", **o) for i, o in enumerate(native["options"])
        ],
    )
    response = QuestionResponse(
        ref="r1",
        outcome="answered",
        question=QuestionAssociation(
            question_ref="q1",
            evidence=[
                QuestionEvidence(
                    field="tool_result.tool_use_id", value=result["tool_use_id"]
                ),
                QuestionEvidence(field="tool_use.id", value=request["id"]),
                QuestionEvidence(
                    field="result question text", value=native["question"]
                ),
            ],
        ),
        selections=[
            QuestionSelection(option_ref=o["ref"], label=o["label"])
            for o in question["options"]
        ],
    )
    assert len(response["selections"]) == 2
    assert "native_id" not in question
    assert json.loads(json.dumps(response)) == response


def test_copilot_shell_failure_does_not_supply_question_response_evidence():
    records = _native_records("copilot/tool_failure/events.jsonl")
    result = next(r["data"] for r in records if r["type"] == "tool.execution_complete")
    assert result["shellExecution"]["exitCode"] == 1
    assert result["success"] is True
    call = ToolCall(
        name="bash",
        result=result["result"],
        result_record={
            "tool_call_id": result["toolCallId"],
            "ts": None,
            "content": result["result"],
            "is_error": False,
        },
    )
    session = Session(
        harness="copilot",
        session_id="s",
        events=[Event(i=0, role="assistant", tool_calls=[call])],
        question_responses=[],
    )
    original = deepcopy(session)
    exported = export_session(session, 0)
    assert exported["events"][0]["tool_calls"][0]["result"] == result["result"]
    assert session["question_responses"] == []
    assert session == original


@pytest.mark.parametrize("by", ["candidate", "settings", "harness", "unknown"])
def test_question_refusal_preserves_source_without_fabricating_feedback(by):
    # Illustrative only: committed fixtures do not capture question refusals.
    response = QuestionResponse(
        ref="r1",
        question=None,
        outcome="refused",
        refusal=QuestionRefusal(by=by, explanation="Recorded refusal explanation"),
    )
    assert json.loads(json.dumps(response))["refusal"]["by"] == by
    assert "candidate_feedback" not in response
    assert "selections" not in response and "free_text" not in response


def test_documented_question_examples_preserve_distinct_states():
    documentation = (FIXTURES / "README.md").read_text()
    section = documentation.split("## Shared questions and responses contract", 1)[1]
    example = json.loads(section.split("```json\n", 1)[1].split("```", 1)[0])
    first, repeated, unanswered = example["interaction_questions"]
    answer, refusal, orphan, unknown = example["question_responses"]
    assert first["text"] == repeated["text"] and first["ref"] != repeated["ref"]
    assert (
        first["options"][0]["recommendation_markers"][0]["value"] == "A (Recommended)"
    )
    assert [s["label"] for s in answer["selections"]] == ["B", "C"]
    assert answer["free_text"] == "Also keep this text"
    assert refusal["outcome"] == "refused"
    assert refusal["refusal"]["by"] == "candidate"
    assert refusal["candidate_feedback"] != refusal["refusal"]["explanation"]
    assert orphan["question"] is None and orphan["selections"] == []
    assert unknown == {"ref": "r4", "outcome": "unknown", "question": None}
    associated = [r["question"] for r in example["question_responses"] if r["question"]]
    assert all(a["evidence"] for a in associated)
    assert unanswered["ref"] not in [a["question_ref"] for a in associated]
    assert example["events"][0]["tool_calls"][0]["is_error"] is True
    assert all("activity" not in r for r in example["question_responses"])
    assert json.loads(json.dumps(example)) == example


@pytest.mark.parametrize("harness", ["claude", "codex", "copilot"])
def test_shared_session_preserves_complete_content(harness):
    images = [
        Image(**image_from_bytes(b"image", "image/png")),
        Image(**unavailable_image("missing.png")),
    ]
    call = ToolCall(
        name="Read",
        input={"paths": ["a", "b"], "options": {"limit": None}},
        result={"content": [None, "", {"text": "original"}]},
        is_error=True,
    )
    call["error"] = {"code": 42, "details": [None, {}]}
    events = [
        Event(i=0, ts=None, role="user", text="**hello**", images=images),
        Event(i=1, ts="later", role="assistant", text="", tool_calls=[call]),
    ]
    session = Session(
        harness=harness,
        session_id="s1",
        cwd="/work",
        started_at="start",
        models=["model-a", "model-b", "model-a"],
        events=events,
    )
    expected = {
        "schema": "raw-session-log/1",
        **session,
        "truncation": {"tool_result_max_bytes": 4096},
    }
    assert build_envelope(session) == expected
    assert json.loads(json.dumps(export_session(session))) == expected


@pytest.mark.parametrize("limit", [4096, 0, 4, -1])
def test_export_caps_only_the_copy_and_retains_original_size(limit):
    result = "€" * 3000
    session = Session(
        harness="test",
        session_id="full",
        events=[
            Event(
                i=0,
                ts=None,
                role="assistant",
                text="",
                tool_calls=[
                    ToolCall(
                        name="Read",
                        input={"full": result},
                        result=result,
                        is_error=False,
                    )
                ],
            )
        ],
    )
    original = deepcopy(session)
    exported = export_session(session, limit)
    call = exported["events"][0]["tool_calls"][0]
    assert call["result_bytes"] == 9000
    assert call["result_truncated"] is (limit >= 0)
    expected = result if limit < 0 else result.encode()[:limit].decode(errors="ignore")
    assert call["result"] == expected
    assert call["input"] == {"full": result}
    assert call["is_error"] is False
    assert exported["truncation"] == {"tool_result_max_bytes": limit}
    assert session == original
    assert export_session(session, -1)["events"][0]["tool_calls"][0]["result"] == result
    call["input"]["full"] = "changed"
    assert session == original


def test_export_default_and_missing_null_empty_distinctions():
    calls = [
        ToolCall(name="missing"),
        ToolCall(name="null", input=None, result=None),
        ToolCall(name="empty", input={}, result=""),
        ToolCall(name="long", result="x" * 5000),
    ]
    events = [
        Event(i=0, role="user"),
        Event(i=1, role="user", ts=None, text=None, images=None),
        Event(i=2, role="user", ts="", text="", images=[]),
        Event(i=3, role="assistant", tool_calls=[]),
        Event(i=4, role="assistant", tool_calls=calls),
    ]
    exported = export_session(Session(harness="test", session_id="s", events=events))
    assert exported["cwd"] is None
    assert exported["started_at"] is None
    assert exported["models"] == []
    assert exported["events"][:4] == events[:4]
    actual = exported["events"][4]["tool_calls"]
    assert actual[0] == {"name": "missing"}
    assert actual[1] == {
        "name": "null",
        "input": None,
        "result": None,
        "result_bytes": 0,
        "result_truncated": False,
    }
    assert actual[2] == {
        "name": "empty",
        "input": {},
        "result": "",
        "result_bytes": 0,
        "result_truncated": False,
    }
    assert actual[3]["result"] == "x" * 4096
    assert actual[3]["result_bytes"] == 5000
    assert actual[3]["result_truncated"] is True


def test_export_preserves_explicit_null_and_empty_metadata():
    session = Session(
        harness="test", session_id="", cwd="", started_at="", models=None, events=[]
    )
    exported = export_session(session)
    assert exported["cwd"] == ""
    assert exported["started_at"] == ""
    assert exported["models"] is None
    assert exported["events"] == []


@pytest.mark.parametrize("limit", [2, -1])
def test_export_preserves_recorded_result_provenance(limit):
    call = ToolCall(
        name="Read", result="retained", result_bytes=9000, result_truncated=True
    )
    session = Session(
        harness="test",
        session_id="s",
        events=[
            Event(i=0, role="assistant", tool_calls=[call]),
            Event(i=1, role="assistant", tool_calls=None),
        ],
    )
    exported = export_session(session, limit)
    actual = exported["events"][0]["tool_calls"][0]
    assert actual["result_bytes"] == 9000
    assert actual["result_truncated"] is True
    assert actual["result"] == ("re" if limit == 2 else "retained")
    assert exported["events"][1]["tool_calls"] is None
    assert call["result"] == "retained"


def test_export_reads_json_fields_without_mutating_session_semantics():
    session = Session(
        harness="claude",
        session_id="s",
        events=[],
        skills_used=["fixture-skill"],
        subagent_count=1,
        unmatched_tool_results=[],
        files_changed=["/workspace/app.py"],
        permission_decisions=[
            {
                "tool": "read app.py",
                "request_id": "request-1",
                "status": "completed",
                "native_decision": "source-specific-label",
                "feedback": "",
                "ts": "recorded time",
            }
        ],
        file_activity=[
            {
                "file_path": "/workspace/app.py",
                "tool_name": "Read",
                "provenance": {
                    "record_type": "claude.tool_use",
                    "turn_index": None,
                    "first_seen_at": None,
                },
            }
        ],
    )
    original = deepcopy(session)
    assert export_session(session) == {
        "schema": "raw-session-log/1",
        "harness": "claude",
        "session_id": "s",
        "cwd": None,
        "started_at": None,
        "models": [],
        "events": [],
        "truncation": {"tool_result_max_bytes": 4096},
    }
    assert session == original


def test_export_preserves_payloads_but_leaves_model_semantics_unused():
    call = ToolCall(
        name="Read",
        id="call-1",
        input={"file_path": "/app.py", "extension": {"unknown": [1, None]}},
        result="full",
        result_record={
            "tool_call_id": "call-1",
            "ts": "later",
            "content": "full",
            "is_error": False,
        },
        permission_denial={"tool": "Read", "message": "no", "by": "user"},
    )
    event = Event(
        i=0,
        role="assistant",
        id="native-message",
        parent_id="parent",
        tool_calls=[call],
    )
    session = Session(harness="claude", session_id="s", events=[event])
    original = deepcopy(session)
    exported = export_session(session)
    assert exported["events"] == [
        {
            "i": 0,
            "role": "assistant",
            "tool_calls": [
                {
                    "name": "Read",
                    "input": call["input"],
                    "result": "full",
                    "result_bytes": 4,
                    "result_truncated": False,
                }
            ],
        }
    ]
    assert session == original


FIXTURES = Path(__file__).parent / "fixtures"


def _native_records(relative_path):
    return [
        json.loads(line) for line in (FIXTURES / relative_path).read_text().splitlines()
    ]


@pytest.mark.parametrize(
    "source", ["candidate", "harness", "ide", "other_agent", "unknown"]
)
def test_authorship_is_independent_of_role_and_preserves_unicode(source):
    # Deliberately synthetic: exercise code points, including a combining mark,
    # rather than claiming these source classifications have native evidence.
    text = "é🙂e\u0301\ncontext"
    span = AuthorshipRange(
        start=0,
        end=4,
        source=source,
        markers=[
            AuthorshipMarker(field="native.source", value={"kind": [source, None]})
        ],
        ts="recorded-span-time",
    )
    event = UserMessage(
        i=0, role="user", text=text, ts="message-time", authorship=[span]
    )
    decoded = json.loads(json.dumps(event))
    assert decoded["text"][span["start"] : span["end"]] == "é🙂e\u0301"
    assert decoded["authorship"] == [span]
    assert decoded["text"] == text
    assert decoded["ts"] == "message-time"
    assert span["ts"] == "recorded-span-time"
    assert len(text.encode()) != len(text)


def test_authorship_examples_define_coverage_without_role_inference():
    documentation = (FIXTURES / "README.md").read_text()
    examples = json.loads(documentation.split("```json\n", 1)[1].split("```", 1)[0])
    candidate, mixed, injected, unknown, missing, partial = examples
    assert candidate["text"] == "Hi 🙂"
    assert candidate["authorship"] == [{"start": 0, "end": 4, "source": "candidate"}]
    assert [mixed["text"][r["start"] : r["end"]] for r in mixed["authorship"]] == [
        "Hi 🙂",
        "<notice>context</notice>",
    ]
    assert [r["source"] for r in mixed["authorship"]] == ["candidate", "harness"]
    assert injected["authorship"][0]["end"] == len(injected["text"])
    assert injected["authorship"][0]["source"] == "ide"
    assert unknown["authorship"][0]["source"] == "unknown"
    assert missing["role"] == "user" and "authorship" not in missing
    assert partial["text"][partial["authorship"][0]["end"] :] == " unknown"
    for event in examples:
        previous_end = 0
        for span in event.get("authorship", []):
            assert previous_end <= span["start"] < span["end"] <= len(event["text"])
            previous_end = span["end"]
        assert json.loads(json.dumps(event)) == event


def _recorded_skill_expansions():
    records = read_source(TRANSCRIPT).entries
    expansions = [
        (entry, block["text"])
        for entry in records
        if entry.get("type") == "user" and entry.get("isMeta")
        for block in entry.get("message", {}).get("content", [])
        if isinstance(block, dict)
        and block.get("type") == "text"
        and block["text"].startswith("Base directory for this skill:")
    ]
    assert expansions, "Missing native skill expansion messages"
    return expansions


def test_claude_native_injected_message_fits_authorship_contract():
    expansions = _recorded_skill_expansions()
    assert expansions
    for native, text in expansions:
        span = AuthorshipRange(
            start=0,
            end=len(text),
            source="harness",
            ts=native["timestamp"],
            markers=[AuthorshipMarker(field="isMeta", value=native["isMeta"])],
        )
        event = UserMessage(
            i=0,
            role="user",
            id=native["uuid"],
            ts=native["timestamp"],
            text=text,
            authorship=[span],
        )
        assert json.loads(json.dumps(event))["text"] == text
        assert span["markers"] == [{"field": "isMeta", "value": True}]
        assert text.startswith("Base directory for this skill:")
        assert event["id"] == native["uuid"]


def test_codex_native_prompt_can_remain_unknown_without_evidence():
    records = _native_records("codex/slash.jsonl")
    native = next(
        r
        for r in records
        if r["type"] == "response_item" and r["payload"].get("role") == "user"
    )
    text = native["payload"]["content"][0]["text"]
    event = UserMessage(i=0, role="user", text=text, ts=native["timestamp"])
    assert json.loads(json.dumps(event))["text"] == "/review fixture"
    assert "authorship" not in event
    assert event["ts"] == native["timestamp"]


def test_copilot_native_transformed_prompt_fits_mixed_ranges():
    records = _native_records("copilot/baseline/events.jsonl")
    native = next(r for r in records if r["type"] == "user.message")
    data = native["data"]
    text = data["transformedContent"]
    boundary = len(text) - len(data["content"])
    # The suffix equality is evidence from this capture, not a classifier.
    assert text[boundary:] == data["content"]
    event = UserMessage(
        i=0,
        role="user",
        id=native["id"],
        message_id=data["messageId"],
        ts=native["timestamp"],
        text=text,
        authorship=[
            AuthorshipRange(
                start=0,
                end=boundary,
                source="harness",
                markers=[AuthorshipMarker(field="type", value=native["type"])],
            ),
            AuthorshipRange(start=boundary, end=len(text), source="candidate"),
        ],
    )
    decoded = json.loads(json.dumps(event))
    assert decoded["text"][:boundary].startswith("<current_datetime>")
    assert decoded["text"][boundary:] == data["content"]
    assert decoded["authorship"] == event["authorship"]
    assert decoded["message_id"] == data["messageId"]


def test_authorship_retains_message_structure_without_changing_export():
    events = [
        Event(
            i=0,
            role="user",
            text=None,
            images=[Image(unavailable=True, ref="image.png")],
            authorship=[],
        ),
        Event(
            i=1,
            role="assistant",
            text="reply",
            tool_calls=[ToolCall(name="Read", id="call-1")],
            authorship=[AuthorshipRange(start=0, end=5, source="unknown", ts=None)],
        ),
        Event(i=2, role="user", text="", authorship=[]),
    ]
    session = Session(harness="test", session_id="s", events=events)
    original = deepcopy(session)
    decoded = json.loads(json.dumps(session))
    assert decoded == session
    assert decoded["events"][0]["images"] == events[0]["images"]
    assert decoded["events"][1]["tool_calls"][0]["id"] == "call-1"
    assert decoded["events"][1]["authorship"][0]["ts"] is None
    exported = export_session(session)
    assert all("authorship" not in event for event in exported["events"])
    assert [event["i"] for event in exported["events"]] == [0, 1, 2]
    assert session == original
