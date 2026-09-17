"""Exercise native task transitions without importing native display libraries."""

import ast
from collections import UserString
from enum import Enum
import json
import math
from pathlib import Path
import random
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_pgl_digitalbrain_path import HeadlessTask, load_digitalbrain


KINDS = [
    "trial_loaded", "stimulus_started", "stimulus_finished",
    "response_started", "response_saved", "trial_completed",
]


@pytest.fixture
def native_digitalbrain(monkeypatch):
    """Compile upstream lifecycle/event classes; replace only dependency setup."""
    source = Path(__file__).resolve().parents[1] / "pgl" / "pglExperiment.py"
    tree = ast.parse(source.read_text())
    names = {"pglTask", "pglEventTrial", "pglEventSegment", "pglEventSubjectResponse"}
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in names]
    for node in classes:
        if node.name == "pglTask":
            node.body = [
                member for member in node.body
                if not isinstance(member, ast.FunctionDef) or member.name != "__init__"
            ]
    namespace = {
        "pglTaskBase": HeadlessTask, "pglEvent": SimpleNamespace,
        "Enum": Enum, "math": math, "random": random, "pglMessages": Mock(),
    }
    exec(compile(ast.Module(body=classes, type_ignores=[]), str(source), "exec"), namespace)
    return load_digitalbrain(monkeypatch, namespace["pglTask"])


@pytest.fixture
def make_task(native_digitalbrain, tmp_path):
    def create(**kwargs):
        display = Mock()
        display.movie.return_value = SimpleNamespace(
            movieNum=0, play=Mock(return_value=None), presentedTimes=[0.5, 0.6]
        )
        display.getSecs.return_value = 20.0
        task = native_digitalbrain.pglDigitalBrainMemoryTask(
            display, 2, 3, 4, descriptionLength=1, moviePath=tmp_path, **kwargs
        )
        task.settings.nSegments = 4
        task.settings.segmin = task.settings.seglen
        task.settings.segmax = task.settings.seglen
        task.settings.waitUntilVolumeTrigger = [False] * 4
        task.settings.saveEyeTracker = False
        task.settings.phaseNum = 2
        task.data.startTime = None
        task.data.endTime = None
        task.data.events = []
        task.data.params = []
        task.data.trialVariables = []
        task.waitUntilVolumeTrigger = False
        task.parameters = [Mock()]
        task.parameters[0].get.side_effect = [
            {"movieNum": 0, "description": "description"},
            {"movieNum": 1, "description": "description"},
        ]
        task.e = SimpleNamespace(flush=True, pgl=display, setEatAllKeys=Mock())
        task.keyBuffer.getText.return_value = "A café on a rainy street."
        task.keyBuffer.getWrappedText.return_value = "A café"
        return task
    return create


def advance(task, timestamp, responses=None):
    task.update(timestamp, responses or [], 2, [task], [])


def finish_trial(task, start=0):
    advance(task, start + 0.5)
    advance(task, start + 0.6)
    advance(task, start + 1.6)
    advance(task, start + 2.1)


def test_native_lifecycle_orders_zero_based_events_and_typed_payloads(make_task):
    events = []
    task = make_task(event_callback=lambda *args: events.append(args))
    task.pgl.movie.return_value.play.side_effect = lambda **kwargs: events.append(("playing",))

    task.start(0)
    advance(task, 0.5)
    advance(task, 0.6, [(3, 0.55)])
    advance(task, 1.6)
    advance(task, 2.1)
    finish_trial(task, 2.1)

    assert [event[0] for event in events] == [
        "trial_loaded", "stimulus_started", "playing", "stimulus_finished",
        "response_started", "response_saved", "trial_completed",
    ] * 2
    milestones = [event for event in events if event[0] != "playing"]
    assert [event[1] for event in milestones] == [0] * 6 + [1] * 6
    assert all(type(event[1]) is int for event in milestones)
    assert milestones[0][2] == {"filename": "second.mp4", "condition": "new"}
    saved = milestones[4][2]
    assert saved == {
        "filename": "second.mp4", "condition": "new",
        "description": "A café on a rainy street.",
        "responses": [{"response": 3, "response_type": 8, "timestamp": 0.55}],
    }
    assert milestones[5][2] == saved
    assert milestones[10][2]["responses"] == []
    assert task.data.params[0]["description"] == saved["description"]
    assert task.done()
    json.dumps(milestones, allow_nan=False)


@pytest.mark.parametrize("segment", [0, 1, 2, 3])
def test_abort_end_never_completes_a_trial(make_task, segment):
    events = []
    task = make_task(event_callback=lambda *args: events.append(args))
    task.start(0)
    for timestamp in [0.5, 0.6, 1.6][:segment]:
        advance(task, timestamp)

    task.end()
    task.end()

    assert "trial_completed" not in [event[0] for event in events]
    assert task.data.endTime == 20.0
    assert task.e.flush is True
    task.e.setEatAllKeys.assert_called_with(False)


@pytest.mark.parametrize("failed_kind", KINDS)
def test_callback_errors_stop_native_progress_immediately(make_task, failed_kind):
    events = []
    failure = RuntimeError("journal unavailable")

    def callback(kind, trial_index, payload):
        events.append(kind)
        if kind == failed_kind:
            raise failure

    task = make_task(event_callback=callback)
    with pytest.raises(RuntimeError) as caught:
        task.start(0)
        finish_trial(task)

    assert caught.value is failure
    assert events == KINDS[:KINDS.index(failed_kind) + 1]
    assert task.state.currentTrial == 0
    if failed_kind in {"trial_loaded", "stimulus_started"}:
        task.pgl.movie.return_value.play.assert_not_called()
    task.end()
    assert events[-1] == failed_kind


@pytest.mark.parametrize("invalid_movie", [None, SimpleNamespace(movieNum=None)])
def test_failed_native_load_emits_no_success(make_task, invalid_movie):
    callback = Mock()
    task = make_task(event_callback=callback)
    task.pgl.movie.return_value = invalid_movie
    with pytest.raises(RuntimeError, match="load"):
        task.start(0)
    callback.assert_not_called()


@pytest.mark.parametrize("play_failure", [False, RuntimeError("decoder failed")])
def test_failed_playback_never_emits_finished(make_task, play_failure):
    events = []
    task = make_task(event_callback=lambda *args: events.append(args))
    if play_failure is False:
        task.pgl.movie.return_value.play.return_value = False
    else:
        task.pgl.movie.return_value.play.side_effect = play_failure
    task.start(0)

    with pytest.raises(RuntimeError):
        advance(task, 0.5)

    assert [event[0] for event in events] == ["trial_loaded", "stimulus_started"]


def test_zero_frame_playback_stops_before_finished_or_next_segment(make_task):
    events = []
    task = make_task(event_callback=lambda *args: events.append(args))
    movie = task.pgl.movie.return_value

    def play(**kwargs):
        movie.presentedTimes = []

    movie.play.side_effect = play
    task.start(0)

    with pytest.raises(RuntimeError, match="frames"):
        advance(task, 0.5)

    assert [event[0] for event in events] == ["trial_loaded", "stimulus_started"]
    assert task.state.currentSegment == 1
    assert math.isinf(task._thisTrialSeglen[1])
    task.end()
    assert [event[0] for event in events] == ["trial_loaded", "stimulus_started"]


@pytest.mark.parametrize("presented_times", [[10.0], [10.0, 10.02, 10.04], [-1.0, 0.0]])
def test_finished_payload_counts_native_frame_records_without_arrays(make_task, presented_times):
    events = []
    task = make_task(event_callback=lambda *args: events.append(args))
    movie = task.pgl.movie.return_value

    def play(**kwargs):
        movie.presentedTimes = presented_times

    movie.play.side_effect = play
    task.start(0)
    advance(task, 0.5)

    assert events[-1] == (
        "stimulus_finished", 0,
        {"filename": "second.mp4", "condition": "new", "presented_frame_count": len(presented_times)},
    )
    assert type(events[-1][2]["presented_frame_count"]) is int
    assert all("presented_frame_count" not in event[2] for event in events[:-1])
    json.dumps(events, allow_nan=False)


def test_description_commit_failure_never_emits_saved_or_completed(make_task):
    events = []
    task = make_task(event_callback=lambda *args: events.append(args))
    task.keyBuffer.getText.side_effect = RuntimeError("buffer unavailable")
    task.start(0)

    with pytest.raises(RuntimeError, match="buffer unavailable"):
        finish_trial(task)

    assert [event[0] for event in events] == KINDS[:4]
    assert task.currentParams["description"] == "description"


def test_saved_callback_observes_commit_and_receives_detached_primitives(make_task):
    events = []

    def callback(kind, trial_index, payload):
        json.dumps(payload, allow_nan=False)
        events.append((kind, trial_index, payload))
        if kind == "response_saved":
            assert task.currentParams["description"] == payload["description"]
            payload["description"] = "observer changed its copy"
            payload["responses"].clear()

    task = make_task(event_callback=callback)
    task.mdb.stimuli[0].condition = UserString("new")
    task.start(0)
    advance(task, 0.5)
    advance(task, 0.6, [(0, 0.55)])
    advance(task, 1.6)
    advance(task, 2.1)

    completed = events[5][2]
    assert completed["description"] == "A café on a rainy street."
    assert completed["responses"] == [
        {"response": 0, "response_type": 0, "timestamp": 0.55},
    ]


@pytest.mark.parametrize("kwargs", [{}, {"event_callback": None}])
@pytest.mark.parametrize("play_result", [None, False])
@pytest.mark.parametrize("frame_state", ["empty", "missing"])
def test_no_callback_preserves_upstream_lifecycle_without_frames(
    make_task, kwargs, play_result, frame_state
):
    task = make_task(**kwargs)
    movie = task.pgl.movie.return_value
    movie.play.return_value = play_result
    if frame_state == "empty":
        movie.presentedTimes = []
    else:
        del movie.presentedTimes
    task.start(0)
    finish_trial(task)
    finish_trial(task, 2.1)

    assert task.done()
    assert task.state.currentTrial == 2
    assert [params["description"] for params in task.data.params] == [
        "A café on a rainy street.", "A café on a rainy street.",
    ]
