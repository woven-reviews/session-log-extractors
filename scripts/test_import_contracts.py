"""Exercise the configured Import Linter contracts against dependency regressions."""

import tomllib
from pathlib import Path

import grimp
import pytest
from importlinter.configuration import configure
from importlinter.contracts.forbidden import ForbiddenContract
from importlinter.contracts.protected import ProtectedContract

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "session_log_extractors"
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["importlinter"]
CONTRACTS = {item["id"]: item for item in CONFIG["contracts"]}


def _check(contract_id, graph):
    options = {
        key: str(value) if isinstance(value, bool) else value
        for key, value in CONTRACTS[contract_id].items()
    }
    contract_type = (
        ForbiddenContract if options["type"] == "forbidden" else ProtectedContract
    )
    contract = contract_type(
        name=options["name"], session_options=CONFIG, contract_options=options
    )
    return contract.check(graph, verbose=False)


@pytest.fixture
def graph():
    configure()
    return grimp.build_graph(PACKAGE, cache_dir=None)


@pytest.mark.parametrize("contract_id", CONTRACTS)
def test_current_dependencies_satisfy_contracts(contract_id, graph):
    assert _check(contract_id, graph).kept


@pytest.mark.parametrize(
    ("contract_id", "importer", "imported"),
    [
        ("model-boundary", "session_model", target)
        for target in [
            "session_log_envelope",
            "claude_sources",
            "codex_sources",
            "copilot_sources",
            "extract_claude_session_log",
            "extract_codex_session_log",
            "extract_copilot_session_log",
        ]
    ]
    + [
        ("export-boundary", "session_log_envelope", target)
        for target in [
            "claude_sources",
            "codex_sources",
            "copilot_sources",
            "extract_claude_session_log",
            "extract_codex_session_log",
            "extract_copilot_session_log",
        ]
    ]
    + [
        ("claude-schemas", "extract_copilot_session_log", "tool_inputs.claude"),
        ("copilot-schemas", "extract_claude_session_log", "tool_inputs.copilot"),
        ("claude-schemas", "session_model", "tool_inputs.claude"),
        ("copilot-schemas", "session_log_envelope", "tool_inputs.copilot"),
    ],
)
def test_prohibited_dependency_breaks_contract(contract_id, importer, imported, graph):
    graph.add_import(importer=f"{PACKAGE}.{importer}", imported=f"{PACKAGE}.{imported}")
    assert not _check(contract_id, graph).kept


@pytest.mark.parametrize("contract_id", ["model-boundary", "export-boundary"])
def test_indirect_prohibited_dependencies_are_rejected(contract_id, graph):
    graph.add_import(
        importer=f"{PACKAGE}.json_types", imported=f"{PACKAGE}.codex_sources"
    )
    assert not _check(contract_id, graph).kept


@pytest.mark.parametrize("harness", ["claude", "copilot"])
def test_shared_union_and_own_extractor_can_import_harness_schema(harness, graph):
    for importer in ["tool_inputs", f"extract_{harness}_session_log"]:
        graph.add_import(
            importer=f"{PACKAGE}.{importer}",
            imported=f"{PACKAGE}.tool_inputs.{harness}",
        )
    assert _check(f"{harness}-schemas", graph).kept
