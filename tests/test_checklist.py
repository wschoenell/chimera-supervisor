# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

import pytest
import yaml

from chimera_supervisor.core import checklist
from chimera_supervisor.core.exceptions import ConfigError

GOOD = """
checklist:
  close_on_dew:
    description: Close on dew risk
    conditions:
      - condition: dome
        slit: open
      - condition: dew_gap
        below: 4
    responses:
      - action: stop_all
      - action: dome
        do: close_slit

  park_telescope:
    active: false
    responses:
      - action: telescope
        do: park
"""


def test_parse_good_document():
    items = checklist.parse_document(yaml.safe_load(GOOD), "test")
    assert [item.name for item in items] == ["close_on_dew", "park_telescope"]
    first, second = items
    assert first.automatic and len(first.conditions) == 2
    assert first.run == "on_change" and first.on_error == "continue"
    assert not second.automatic  # manual: no conditions
    assert not second.active


def test_checklist_wrapper_is_optional():
    doc = yaml.safe_load(GOOD)["checklist"]
    items = checklist.parse_document(doc, "test")
    assert len(items) == 2


def test_round_trip_dump_and_parse():
    items = checklist.parse_document(yaml.safe_load(GOOD), "test")
    dumped = checklist.dump_items(items)
    reparsed = checklist.parse_document(yaml.safe_load(dumped), "test")
    assert [item.name for item in reparsed] == [item.name for item in items]
    for old, new in zip(items, reparsed):
        assert old.conditions == new.conditions
        assert old.responses == new.responses
        assert (old.active, old.run, old.on_error) == (new.active, new.run, new.on_error)


def test_legacy_document_is_rejected_with_hint():
    doc = {"checklist": [{"name": "X", "check": [], "responses": []}]}
    with pytest.raises(ConfigError, match="migrate"):
        checklist.parse_document(doc, "test")


def test_item_requires_responses():
    with pytest.raises(ConfigError, match="responses"):
        checklist.parse_document({"x": {"conditions": []}}, "test")


def test_unknown_item_key_rejected():
    with pytest.raises(ConfigError, match="unknown key"):
        checklist.parse_document(
            {"x": {"responses": [{"action": "stop_all"}], "eager": True}}, "test"
        )


def test_bad_run_value_rejected():
    with pytest.raises(ConfigError):
        checklist.parse_document(
            {"x": {"run": "sometimes", "responses": [{"action": "stop_all"}]}}, "test"
        )


def test_load_directory_rejects_duplicates(tmp_path):
    (tmp_path / "a.yaml").write_text("x:\n  responses:\n    - action: stop_all\n")
    (tmp_path / "b.yaml").write_text("x:\n  responses:\n    - action: stop_all\n")
    with pytest.raises(ConfigError, match="duplicate"):
        checklist.load_directory(tmp_path)


def test_load_directory_skips_hidden_files(tmp_path):
    (tmp_path / "a.yaml").write_text("x:\n  responses:\n    - action: stop_all\n")
    (tmp_path / ".#a.yaml").write_text("garbage: [")
    items = checklist.load_directory(tmp_path)
    assert [item.name for item in items] == ["x"]


def test_invalid_yaml_reports_source(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("checklist: [")
    with pytest.raises(ConfigError, match="bad.yaml"):
        checklist.load_file(bad)
