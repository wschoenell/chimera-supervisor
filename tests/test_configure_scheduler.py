# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-supervisor authors

"""configure_scheduler must be able to load chimera's own sample queue
(that file exercises every action type, including point offsets — which is
how the Coord.AS/arcsec regression slipped through untested)."""

import os
import pathlib

import pytest


@pytest.fixture
def scheduler_session(tmp_path, monkeypatch):
    """Point chimera's scheduler model at a scratch database."""
    from chimera.controllers.scheduler import model
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path}/scheduler.db")
    model.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    monkeypatch.setattr(model, "Session", session)
    return session


def sample_sched_path() -> pathlib.Path:
    import chimera.controllers.scheduler as sched

    return pathlib.Path(os.path.dirname(sched.__file__)) / "sample-sched.yaml"


def test_loads_chimera_sample_queue(scheduler_session):
    from chimera.controllers.scheduler.model import Point, Program

    from chimera_supervisor.core.actions import _load_scheduler_programs

    count = _load_scheduler_programs(str(sample_sched_path()))
    assert count > 0

    session = scheduler_session()
    programs = session.query(Program).all()
    assert len(programs) == count
    assert any(program.actions for program in programs)

    # offsets (south/east are negated) must parse without touching old APIs
    points = session.query(Point).all()
    offsets = [p for p in points if p.offset_ns is not None or p.offset_ew is not None]
    if offsets:  # the sample file carries offset examples today
        assert all(
            p.offset_ns is None or hasattr(p.offset_ns, "arcsec") for p in offsets
        )


def test_reload_replaces_the_queue(scheduler_session):
    from chimera.controllers.scheduler.model import Program

    from chimera_supervisor.core.actions import _load_scheduler_programs

    first = _load_scheduler_programs(str(sample_sched_path()))
    second = _load_scheduler_programs(str(sample_sched_path()))
    assert first == second
    session = scheduler_session()
    assert session.query(Program).count() == second
