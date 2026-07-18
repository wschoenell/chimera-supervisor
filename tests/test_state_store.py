# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

import datetime

import pytest

from chimera_supervisor.core.exceptions import StatusUpdateError
from chimera_supervisor.core.flags import InstrumentOperationFlag as Flag
from chimera_supervisor.persistence.state import StateStore


@pytest.fixture
def store():
    return StateStore(":memory:")


def test_unknown_instrument_is_unset(store):
    assert store.get_flag("dome") == Flag.UNSET
    assert "dome" in store.instruments()


def test_set_and_get_flag(store):
    store.set_flag("dome", Flag.READY)
    assert store.get_flag("dome") == Flag.READY


def test_lock_blocks_plain_flag_changes(store):
    store.lock("dome", "dew")
    assert store.get_flag("dome") == Flag.LOCK
    with pytest.raises(StatusUpdateError):
        store.set_flag("dome", Flag.READY)


def test_unlock_needs_every_key_released(store):
    store.lock("dome", "dew")
    store.lock("dome", "operator")
    assert sorted(store.active_keys("dome")) == ["dew", "operator"]

    # the partial-release notice must name both the released key and the
    # keys still holding the lock
    with pytest.raises(StatusUpdateError, match=r"released key 'dew'.*\['operator'\]"):
        store.unlock("dome", "dew")
    assert store.get_flag("dome") == Flag.LOCK

    assert store.unlock("dome", "operator") is True
    # fully unlocked -> CLOSE (legacy behavior: unlocked instruments must be
    # explicitly reopened)
    assert store.get_flag("dome") == Flag.CLOSE
    assert store.active_keys("dome") == []


def test_unlock_when_not_locked_returns_false(store):
    store.set_flag("dome", Flag.READY)
    assert store.unlock("dome", "dew") is False


def test_relock_with_same_key(store):
    store.lock("dome", "dew")
    assert store.unlock("dome", "dew") is True
    store.lock("dome", "dew")
    assert store.has_key("dome", "dew")


def test_has_key(store):
    store.lock("dome", "dew")
    assert store.has_key("dome", "dew")
    assert not store.has_key("dome", "operator")
    assert not store.has_key("telescope", "dew")


def test_can_open_all_instruments(store):
    store.set_flag("site", Flag.READY)
    store.set_flag("dome", Flag.OPERATING)
    assert store.can_open()
    store.set_flag("dome", Flag.CLOSE)
    assert not store.can_open()


def test_can_open_single_instrument_requires_site(store):
    store.set_flag("dome", Flag.READY)
    store.set_flag("site", Flag.CLOSE)
    assert not store.can_open("dome")
    store.set_flag("site", Flag.READY)
    assert store.can_open("dome")


def test_item_state_round_trip(store):
    now = datetime.datetime(2026, 7, 6, 3, 0, 0)
    assert store.item_status("x") is None
    store.update_item("x", True, changed=True, now=now)
    assert store.item_status("x") is True
    update, change = store.item_times("x")
    assert update == now and change == now

    later = now + datetime.timedelta(minutes=5)
    store.update_item("x", False, changed=False, now=later)
    assert store.item_status("x") is False
    update, change = store.item_times("x")
    assert update == later and change == now  # last_change untouched


def test_condition_memory(store):
    now = datetime.datetime(2026, 7, 6, 3, 0, 0)
    assert store.get_since("item", 0) is None
    store.set_since("item", 0, now)
    assert store.get_since("item", 0) == now
    store.set_since("item", 0, None)
    assert store.get_since("item", 0) is None


def test_prune_items(store):
    now = datetime.datetime(2026, 7, 6)
    store.update_item("keep", True, changed=False, now=now)
    store.update_item("drop", True, changed=False, now=now)
    store.set_since("drop", 0, now)
    store.prune_items(["keep"])
    assert store.item_status("drop") is None
    assert store.get_since("drop", 0) is None
    assert store.item_status("keep") is True


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "state.db"
    store = StateStore(path)
    store.lock("dome", "dew")
    store.update_item("x", True, changed=True, now=datetime.datetime(2026, 7, 6))
    store.close()

    reopened = StateStore(path)
    assert reopened.get_flag("dome") == Flag.LOCK
    assert reopened.has_key("dome", "dew")
    assert reopened.item_status("x") is True


def test_unlock_with_key_not_held_is_a_quiet_noop(store):
    # a periodic unlock item must not raise (and so not broadcast) when the
    # instrument is locked with a different key
    store.lock("dome", "sunup")
    assert store.unlock("dome", "dew") is False
    assert store.get_flag("dome") == Flag.LOCK
    assert store.active_keys("dome") == ["sunup"]
