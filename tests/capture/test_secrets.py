import pytest

from session_log_extractors.capture import secrets

MARKER = "[tool result removed: possible secret]"


@pytest.mark.parametrize(
    "text",
    [
        "DATABASE_PASSWORD=hunter2hunter2",
        "password: correct-horse-battery",
        "export API_TOKEN=abc123def456",
        "client_secret = 'x9Zq-77-lmn'",
        "token: Zm9vYmFy",
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11ABCDEFG0abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-1234567890-abcdefghij",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Authorization: Bearer abcdefghijkl",
        "postgres://admin:s3cr3tpw@db.example.com/app",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "here is a blob q8Zx7LmP2vN4kR9tYw3BcD6fGhJ1sA5e0u",
    ],
)
def test_credential_shaped_text_is_flagged(text):
    assert secrets.looks_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "Ran 12 tests, 0 failures",
        "commit 8f14e45fceea167a5a36dedd4bea2543f4e8b3d2",
        "see https://example.com/docs/page.html for details",
        "def authenticate(user): return user.password_hash is not None",
        "src/app/models/user_token_service.py",
        "/Users/someone/projects/application/src/very_long_module_name_here.py",
        "def a_very_long_function_name_that_describes_what_it_does_in_detail():",
    ],
)
def test_ordinary_text_is_not_flagged(text):
    assert not secrets.looks_secret(text)


def test_env_file_references_are_detected():
    assert secrets.mentions_env_file("cat .env")
    assert secrets.mentions_env_file("/work/app/.env.local")
    assert secrets.mentions_env_file({"file_path": "/work/app/.env"})
    assert not secrets.mentions_env_file("cat environment.md")
    assert not secrets.mentions_env_file({"command": "ls"})


def test_clip_result_keeps_short_text():
    assert secrets.clip_result("ok", 1024) == "ok"


def test_clip_result_truncates_on_a_character_boundary_and_records_size():
    clipped = secrets.clip_result("é" * 2000, 1024)

    assert clipped.startswith("é" * 100)
    assert "[truncated: 4000 bytes total]" in clipped
    clipped.encode("utf-8")


def test_clip_result_drops_whole_result_on_a_secret_even_if_short():
    assert secrets.clip_result("PASSWORD=abcdefgh", 1024) == MARKER


def test_clip_result_checks_beyond_the_kept_prefix():
    text = ("x" * 2000) + "\nAPI_KEY=abcdefghijkl"

    assert secrets.clip_result(text, 1024) == MARKER


def test_redact_lines_replaces_only_offending_lines():
    text = "first\nDB_PASSWORD=hunter2hunter2\nlast"

    assert secrets.redact_lines(text) == "first\n[line removed: possible secret]\nlast"


def test_redact_lines_leaves_clean_text_alone():
    assert secrets.redact_lines("a\nb") == "a\nb"


def test_strip_injected_tags_removes_system_reminders():
    text = (
        "hello <system-reminder>userEmail: a@b.com global rules</system-reminder> world"
    )

    assert secrets.strip_injected_tags(text) == "hello  world"


def test_strip_injected_tags_handles_multiline_blocks_and_keeps_command_tags():
    text = "<system-reminder>\nline\n</system-reminder>\nkept <command-name>/model</command-name>"

    assert (
        secrets.strip_injected_tags(text).strip()
        == "kept <command-name>/model</command-name>"
    )


def test_mentions_env_file_looks_inside_lists():
    assert secrets.mentions_env_file(["ls", {"path": "/w/.env"}])
    assert not secrets.mentions_env_file(["ls", 5])
