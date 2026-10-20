import json

from session_log_extractors.capture import filtering

MARKER = "[tool result removed: possible secret]"


def jl(*entries):
    return "\n".join(json.dumps(e) for e in entries) + "\n"


def parse(text):
    return [json.loads(line) for line in text.splitlines()]


def claude_user(content, **extra):
    message = {"role": "user", "content": content}
    return {
        "type": "user",
        "timestamp": "t",
        "sessionId": "s",
        "message": message,
        **extra,
    }


def claude_assistant(blocks):
    message = {"role": "assistant", "content": blocks}
    return {"type": "assistant", "timestamp": "t", "sessionId": "s", "message": message}


def tool_use(tid, name, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid, content, **extra):
    return {"type": "tool_result", "tool_use_id": tid, "content": content, **extra}


def result_content(text, index=1, block=0):
    return parse(filtering.filter_claude(text))[index]["message"]["content"][block][
        "content"
    ]


# ---- Claude -------------------------------------------------------------


def test_claude_keeps_only_graded_entry_types():
    text = jl(
        {"type": "attachment", "attachment": {"content": "userEmail a@b.com"}},
        {"type": "api-request-blob", "blob": "x"},
        {"type": "file-history-snapshot"},
        claude_user("hello"),
        claude_assistant([{"type": "text", "text": "hi"}]),
    )

    out = parse(filtering.filter_claude(text))

    assert [e["type"] for e in out] == ["user", "assistant"]


def test_claude_drops_meta_sidechain_and_unparseable_lines():
    text = jl(claude_user("meta", isMeta=True), claude_user("side", isSidechain=True))

    assert filtering.filter_claude(text + "not json\n[1]\n") == ""


def test_claude_strips_system_reminders_with_personal_config_and_email():
    reminder = "<system-reminder>userEmail: me@x.com\nGlobal CLAUDE.md: personal rules</system-reminder>"

    out = filtering.filter_claude(jl(claude_user(f"real prompt {reminder}")))

    assert "me@x.com" not in out
    assert "personal rules" not in out
    assert "real prompt" in out


def test_claude_drops_entry_that_is_only_a_reminder():
    reminder = "<system-reminder>userEmail: me@x.com</system-reminder>"

    assert filtering.filter_claude(jl(claude_user(reminder))) == ""
    assert (
        filtering.filter_claude(jl(claude_user([{"type": "text", "text": reminder}])))
        == ""
    )


def test_claude_drops_thinking_and_unknown_blocks():
    blocks = [
        {"type": "thinking", "thinking": "private thoughts"},
        {"type": "mystery"},
        {"type": "text", "text": "ok"},
    ]

    out = parse(filtering.filter_claude(jl(claude_assistant(blocks))))

    assert out[0]["message"]["content"] == [{"type": "text", "text": "ok"}]


def test_claude_redacts_pasted_secret_lines_in_user_text():
    text = jl(
        claude_user("deploy with\nAWS_SECRET_ACCESS_KEY=abcd1234efgh5678\nplease")
    )

    out = parse(filtering.filter_claude(text))

    assert (
        out[0]["message"]["content"]
        == "deploy with\n[line removed: possible secret]\nplease"
    )


def test_claude_redacts_secrets_in_tool_input_but_keeps_other_fields():
    command = "curl -H 'Authorization: Bearer abcdefghijkl' x"
    use = tool_use("t1", "Bash", command=command, description="call api")

    out = parse(filtering.filter_claude(jl(claude_assistant([use]))))

    block = out[0]["message"]["content"][0]
    assert block["input"]["description"] == "call api"
    assert "abcdefghijkl" not in block["input"]["command"]


def test_claude_clips_tool_results_to_one_kilobyte():
    text = jl(
        claude_assistant([tool_use("t1", "Bash", command="ls")]),
        claude_user([tool_result("t1", "x" * 5000)]),
    )

    result = result_content(text)

    assert result.startswith("x" * 1024)
    assert "[truncated: 5000 bytes total]" in result


def test_claude_drops_whole_tool_result_on_odd_password():
    text = jl(
        claude_assistant([tool_use("t1", "Bash", command="cat config.yml")]),
        claude_user([tool_result("t1", "ok\n  password: Tr0ub4dor&3\nmore")]),
    )

    assert result_content(text) == MARKER


def test_claude_drops_results_of_env_file_reads():
    text = jl(
        claude_assistant([tool_use("t1", "Read", file_path="/work/app/.env")]),
        claude_user([tool_result("t1", "HOST=localhost")]),
    )

    assert result_content(text) == MARKER


def test_claude_handles_list_results_with_text_and_images():
    huge = {"type": "base64", "data": "A" * 2_000_000, "media_type": "image/png"}
    small = {"type": "base64", "data": "AAAA", "media_type": "image/png"}
    parts = [
        {"type": "text", "text": "shot"},
        {"type": "image", "source": small},
        {"type": "image", "source": huge},
        {"type": "weird"},
    ]
    text = jl(
        claude_assistant([tool_use("t1", "Read", file_path="a.png")]),
        claude_user([tool_result("t1", parts)]),
    )

    content = result_content(text)

    assert content[0] == {"type": "text", "text": "shot"}
    assert content[1]["type"] == "image"
    assert content[2] == {"type": "text", "text": "[image removed: too large]"}
    assert len(content) == 3


def test_claude_keeps_longer_results_for_plan_and_question_tools():
    plan = "p" * 3000
    text = jl(
        claude_assistant([tool_use("t1", "ExitPlanMode", plan=plan)]),
        claude_user([tool_result("t1", "User has approved your plan\n" + plan)]),
    )

    assert len(result_content(text)) > 3000


def test_claude_tool_use_result_follows_the_same_rules():
    text = jl(
        claude_assistant(
            [
                tool_use("t1", "Bash", command="ls"),
                tool_use("t2", "AskUserQuestion", questions=[]),
            ]
        ),
        {
            **claude_user([tool_result("t1", "a"), tool_result("t2", "answered")]),
            "toolUseResult": {"stdout": "big" * 5000},
        },
        {
            **claude_user([tool_result("t2", "answered")]),
            "toolUseResult": {"answers": {"q": "a"}},
        },
        {
            **claude_user([tool_result("t1", "x")]),
            "toolUseResult": "PASSWORD=abcdefghi",
        },
        {**claude_user("plain"), "toolUseResult": {"k": "v"}},
    )

    out = parse(filtering.filter_claude(text))

    assert "toolUseResult" not in out[1]
    assert out[2]["toolUseResult"] == {"answers": {"q": "a"}}
    assert "toolUseResult" not in out[3]
    assert "toolUseResult" not in out[4]


def test_claude_keeps_error_flag_and_plain_string_content():
    text = jl(
        claude_assistant([tool_use("t1", "Bash", command="false")]),
        claude_user([tool_result("t1", "boom", is_error=True)]),
    )

    block = parse(filtering.filter_claude(text))[1]["message"]["content"][0]

    assert block["is_error"] is True
    assert block["content"] == "boom"


def test_claude_keeps_only_allowlisted_top_level_keys():
    entry = claude_user(
        "hi",
        cwd="/w",
        version="2.1.0",
        gitBranch="b",
        requestId="r",
        userType="external",
        extra="x",
    )

    out = parse(filtering.filter_claude(jl(entry)))[0]

    assert set(out) == {"type", "timestamp", "sessionId", "message", "cwd", "version"}


def test_claude_tolerates_odd_message_shapes():
    text = jl(
        {"type": "user", "message": "not a dict"},
        {"type": "user", "message": {"content": 5}},
        claude_assistant(
            [
                "bare string",
                {"type": "tool_result", "tool_use_id": "zz", "content": None},
            ]
        ),
    )

    out = parse(filtering.filter_claude(text))

    assert len(out) == 1
    assert out[0]["message"]["content"][0] == {"type": "text", "text": "bare string"}
    assert out[0]["message"]["content"][1]["content"] == ""


# ---- Codex --------------------------------------------------------------


def codex(etype, payload):
    return {"timestamp": "t", "type": etype, "payload": payload}


def codex_msg(role, text):
    return codex(
        "response_item",
        {
            "type": "message",
            "role": role,
            "content": [{"type": "input_text", "text": text}],
        },
    )


def codex_call(cid, **kw):
    return codex(
        "response_item",
        {"type": "function_call", "name": "shell", "call_id": cid, **kw},
    )


def codex_output(cid, output, kind="function_call_output"):
    return codex("response_item", {"type": kind, "call_id": cid, "output": output})


def test_codex_keeps_graded_events_and_strips_instructions():
    meta = {
        "id": "s1",
        "cwd": "/w",
        "cli_version": "0.1",
        "base_instructions": {"text": "personal"},
    }
    context = {
        "cwd": "/w",
        "model": "m",
        "collaboration_mode": {"mode": "plan"},
        "user_instructions": "x",
    }
    text = jl(
        codex("session_meta", meta),
        codex("turn_context", context),
        codex("response_item", {"type": "reasoning", "summary": []}),
        codex("compacted", {"x": 1}),
        codex("event_msg", {"type": "token_count", "info": {}}),
    )

    out = parse(filtering.filter_codex(text))

    assert out[0]["payload"] == {"id": "s1", "cwd": "/w", "cli_version": "0.1"}
    assert out[1]["payload"] == {
        "cwd": "/w",
        "model": "m",
        "collaboration_mode": {"mode": "plan"},
    }
    assert len(out) == 2


def test_codex_messages_keep_user_and_assistant_text_only():
    text = jl(
        codex_msg("developer", "permissions instructions"),
        codex_msg("user", "<environment_context>cwd</environment_context>"),
        codex_msg("user", "# AGENTS.md instructions for /w"),
        codex_msg("user", "do the thing\nTOKEN=abcdef123456"),
        codex_msg("assistant", "done"),
    )

    out = parse(filtering.filter_codex(text))

    assert [e["payload"]["role"] for e in out] == ["user", "assistant"]
    assert (
        out[0]["payload"]["content"][0]["text"]
        == "do the thing\n[line removed: possible secret]"
    )


def test_codex_clips_tool_outputs_and_drops_secrets_and_env_reads():
    text = jl(
        codex_call("c1", arguments='{"command": "ls"}'),
        codex_output("c1", "y" * 4000),
        codex_call("c2", arguments='{"command": "cat .env"}'),
        codex_output("c2", "HOST=x"),
        codex_call("c3", arguments='{"command": "cat cfg"}'),
        codex_output("c3", "api_key: zzzzzzzz1234"),
        codex(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "c4",
                "input": "run",
            },
        ),
        codex_output(
            "c4",
            [{"type": "input_text", "text": "listing"}, {"type": "x"}],
            "custom_tool_call_output",
        ),
        codex_output("c5", {"output": "dict form", "metadata": {"a": 1}}),
        codex_output("c6", None),
    )

    out = parse(filtering.filter_codex(text))

    assert "[truncated: 4000 bytes total]" in out[1]["payload"]["output"]
    assert out[3]["payload"]["output"] == MARKER
    assert out[5]["payload"]["output"] == MARKER
    assert out[7]["payload"]["output"] == [{"type": "input_text", "text": "listing"}]
    assert out[8]["payload"]["output"] == "dict form"
    assert out[9]["payload"]["output"] == ""


def test_codex_event_messages_are_allowlisted_and_clipped():
    changes = {"/w/a": {"type": "add", "content": "c" * 3000}}
    text = jl(
        codex(
            "event_msg",
            {
                "type": "user_message",
                "message": "hi\nSECRET=abcdefgh1234",
                "images": ["data:x"],
            },
        ),
        codex(
            "event_msg",
            {"type": "task_started", "collaboration_mode_kind": "plan", "junk": 1},
        ),
        codex(
            "event_msg",
            {
                "type": "patch_apply_end",
                "stdout": "o" * 3000,
                "stderr": "TOKEN=abcdef12345",
            },
        ),
        codex(
            "event_msg",
            {
                "type": "item_completed",
                "item": {"type": "FileChange", "changes": changes},
            },
        ),
        codex(
            "event_msg",
            {"type": "item_completed", "item": {"type": "Other", "text": "dropme"}},
        ),
        codex("event_msg", {"type": "agent_message", "message": "ok"}),
    )

    out = parse(filtering.filter_codex(text))

    assert out[0]["payload"]["message"] == "hi\n[line removed: possible secret]"
    assert out[0]["payload"]["images"] == ["data:x"]
    assert out[1]["payload"] == {
        "type": "task_started",
        "collaboration_mode_kind": "plan",
    }
    assert "[truncated: 3000 bytes total]" in out[2]["payload"]["stdout"]
    assert out[2]["payload"]["stderr"] == MARKER
    assert "[truncated" in out[3]["payload"]["item"]["changes"]["/w/a"]["content"]
    assert out[4]["payload"]["item"] == {"type": "Other"}
    assert out[5]["payload"]["message"] == "ok"


def test_codex_drops_oversized_images_and_bad_lines():
    big = "data:image/png;base64," + "A" * 2_000_000
    text = (
        jl(
            codex(
                "event_msg", {"type": "user_message", "message": "m", "images": [big]}
            )
        )
        + "garbage\n"
    )

    out = parse(filtering.filter_codex(text))

    assert out[0]["payload"]["images"] == ["[image removed: too large]"]
    assert len(out) == 1


# ---- Copilot ------------------------------------------------------------


def cop(etype, **data):
    return {"type": etype, "data": data, "id": "i", "timestamp": "t", "parentId": "p"}


def test_copilot_keeps_allowlisted_events_and_drops_the_rest():
    text = jl(
        cop(
            "session.start",
            sessionId="s",
            copilotVersion="1.0",
            startTime="t",
            context={"cwd": "/w", "x": 1},
            producer="p",
        ),
        cop("session.shutdown", totalPremiumRequests=1),
        cop("session.warning", message="w"),
        cop("skill.context_delivered_ref", prefix="big"),
        cop("assistant.turn_start", turnId="0"),
        cop("assistant.turn_end", turnId="0"),
        cop("session.model_change", newModel="m", source="startup"),
    )

    out = parse(filtering.filter_copilot_events(text))

    kinds = [e["type"] for e in out]
    assert kinds == [
        "session.start",
        "assistant.turn_start",
        "assistant.turn_end",
        "session.model_change",
    ]
    assert out[0]["data"] == {
        "sessionId": "s",
        "copilotVersion": "1.0",
        "startTime": "t",
        "context": {"cwd": "/w"},
    }


def test_copilot_user_message_drops_transformed_content_and_redacts():
    attachment = {"displayName": "a.png", "path": "/w/a.png", "type": "image"}
    text = jl(
        cop(
            "user.message",
            content="use key\nAPI_KEY=abcd1234efgh",
            transformedContent="<system_reminder>personal</system_reminder>",
            attachments=[attachment],
        )
    )

    data = parse(filtering.filter_copilot_events(text))[0]["data"]

    assert "transformedContent" not in data
    assert data["content"] == "use key\n[line removed: possible secret]"
    assert data["attachments"][0]["displayName"] == "a.png"


def test_copilot_tools_clip_results_and_redact_arguments():
    request = {
        "toolCallId": "c1",
        "name": "bash",
        "arguments": {"command": "echo TOKEN=abcdefg12345"},
    }
    text = jl(
        cop(
            "assistant.message",
            messageId="m",
            content="running",
            toolRequests=[request],
            reasoningText="thoughts",
        ),
        cop(
            "tool.execution_start",
            toolCallId="c1",
            toolName="bash",
            arguments={"command": "cat .env"},
        ),
        cop(
            "tool.execution_complete",
            toolCallId="c1",
            success=True,
            result={"content": "HOST=x"},
        ),
        cop(
            "tool.execution_start",
            toolCallId="c2",
            toolName="bash",
            arguments={"command": "ls"},
        ),
        cop(
            "tool.execution_complete",
            toolCallId="c2",
            success=True,
            result={"content": "z" * 3000},
        ),
        cop("tool.execution_complete", toolCallId="c3", success=False, result="plain"),
        cop("tool.execution_complete", toolCallId="c4", success=True),
    )

    out = parse(filtering.filter_copilot_events(text))

    assert "reasoningText" not in out[0]["data"]
    assert "abcdefg12345" not in json.dumps(out[0])
    assert out[2]["data"]["result"]["content"] == MARKER
    assert "[truncated: 3000 bytes total]" in out[4]["data"]["result"]["content"]
    assert out[5]["data"]["result"] == {"content": "plain"}
    assert "result" not in out[6]["data"]


def test_copilot_permissions_skills_and_assets():
    asset = {"assetId": "a", "type": "image", "mimeType": "image/png", "byteLength": 4}
    text = jl(
        cop(
            "permission.requested",
            requestId="r",
            toolName="bash",
            fullCommandText="rm x",
            extra="no",
        ),
        cop("permission.completed", requestId="r", result={"kind": "approved"}),
        cop(
            "skill.invoked",
            name="s",
            path="/w/.github/skills/s/SKILL.md",
            content="body",
            source="project",
        ),
        cop("session.binary_asset", **asset, data="AAAA"),
        cop("session.binary_asset", **asset, data="A" * 2_000_000),
    )

    out = parse(filtering.filter_copilot_events(text))

    assert "extra" not in out[0]["data"]
    assert out[1]["data"]["result"] == {"kind": "approved"}
    assert "content" not in out[2]["data"]
    assert out[3]["data"]["data"] == "AAAA"
    assert out[4]["data"]["data"] == "[image removed: too large]"


def test_copilot_tolerates_bad_lines_and_missing_data():
    text = "nope\n" + jl(
        {"type": "user.message"}, {"type": "assistant.message", "data": "str"}
    )

    out = parse(filtering.filter_copilot_events(text))

    assert out[0]["data"] == {}
    assert out[1]["data"] == {"toolRequests": []}


def test_copilot_db_dump_keeps_only_allowlisted_columns_and_redacts():
    session = {
        "id": "s",
        "cwd": "/w",
        "repository": "r",
        "branch": "b",
        "summary": "x",
        "secret_col": "x",
    }
    turn = {
        "session_id": "s",
        "turn_index": 0,
        "user_message": "go\nPASSWORD=abcdefgh1",
        "assistant_response": "ok",
    }
    dump = {
        "sessions": [session],
        "turns": [turn],
        "unknown_table": [{"a": 1}],
        "bad": "notalist",
    }

    out = filtering.filter_copilot_dump(dump)

    assert set(out) == {"sessions", "turns"}
    assert "secret_col" not in out["sessions"][0]
    assert out["turns"][0]["user_message"] == "go\n[line removed: possible secret]"


def test_drop_line_replaces_one_line_with_a_stub():
    text = jl({"a": 1}, {"b": 2}, {"c": 3})

    out = parse(filtering.drop_line(text, 2))

    assert out == [
        {"a": 1},
        {"type": "capture_removed", "reason": "possible secret"},
        {"c": 3},
    ]
    assert filtering.drop_line(text, 99) == text


# ---- Edge shapes --------------------------------------------------------


def test_claude_ignores_tool_calls_without_an_id_and_non_dict_result_parts():
    text = jl(
        claude_assistant(
            [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
        ),
        claude_user([tool_result("t1", [5, {"type": "text", "text": "ok"}])]),
    )

    out = parse(filtering.filter_claude(text))

    assert out[1]["message"]["content"][0]["content"] == [
        {"type": "text", "text": "ok"}
    ]


def test_claude_keeps_top_level_image_blocks_and_drops_non_dict_blocks():
    image = {"type": "image", "source": {"type": "base64", "data": "AAAA"}}
    text = jl(claude_user([image, 5]))

    content = parse(filtering.filter_claude(text))[0]["message"]["content"]

    assert content == [image]


def test_claude_tool_use_result_is_dropped_when_the_entry_has_no_tool_results():
    entry = {
        **claude_assistant([{"type": "text", "text": "x"}]),
        "toolUseResult": {"a": 1},
    }

    assert "toolUseResult" not in parse(filtering.filter_claude(jl(entry)))[0]


def test_codex_secret_in_a_list_output_is_replaced_whole():
    text = jl(
        codex_call("c1", arguments='{"command": "cat .env"}'),
        codex_output(
            "c1", [{"type": "input_text", "text": "HOST=x"}], "custom_tool_call_output"
        ),
    )

    out = parse(filtering.filter_codex(text))

    assert out[1]["payload"]["output"] == [{"type": "input_text", "text": MARKER}]


def test_codex_tolerates_non_list_images_and_non_dict_changes_and_items():
    text = jl(
        codex("event_msg", {"type": "user_message", "message": "m", "images": "nope"}),
        codex("event_msg", {"type": "item_completed", "item": "str"}),
        codex(
            "event_msg",
            {
                "type": "item_completed",
                "item": {"type": "FileChange", "changes": {"/a": 5}},
            },
        ),
    )

    out = parse(filtering.filter_codex(text))

    assert out[0]["payload"]["images"] == []
    assert out[1]["payload"]["item"] == {}
    assert out[2]["payload"]["item"]["changes"] == {"/a": {}}


def test_copilot_tolerates_non_dict_tool_requests():
    text = jl(cop("assistant.message", content="x", toolRequests=["bad"]))

    assert parse(filtering.filter_copilot_events(text))[0]["data"]["toolRequests"] == [
        {}
    ]


def test_codex_message_images_are_kept_when_small_and_replaced_when_large():
    small = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
    large = {
        "type": "input_image",
        "image_url": "data:image/png;base64," + "A" * 2_000_000,
    }
    payload = {
        "type": "message",
        "role": "user",
        "content": [small, large, {"type": "input_text", "text": "look"}, 5],
    }

    out = parse(filtering.filter_codex(jl(codex("response_item", payload))))

    assert out[0]["payload"]["content"] == [
        small,
        {"type": "input_text", "text": "[image removed: too large]"},
        {"type": "input_text", "text": "look"},
    ]
