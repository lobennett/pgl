"""Headless coverage for pglDigitalBrainConfigure(e, run, moviePath=prepared_dir).

An explicit directory is the block itself, not a root needing a subject suffix.
Omitting moviePath or passing None retains the original root and block suffix.
"""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


class HeadlessTask:
    def __init__(self, pgl, message=None):
        self.pgl = pgl
        self.settings = SimpleNamespace()
        self.state = SimpleNamespace()
        self.data = SimpleNamespace()
        self.parameters = []

    def addParameter(self, parameter):
        self.parameters.append(parameter)


@pytest.fixture
def digitalbrain(monkeypatch):
    return load_digitalbrain(monkeypatch)


def load_digitalbrain(monkeypatch, task_class=HeadlessTask):
    """Load the real module without PGL's native/display/device imports."""
    package_name = "_pgl_digitalbrain_path_test"
    package = ModuleType(package_name)
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package_name, package)
    dependencies = {
        "pglSettings": {"pglTraitSettings": object},
        "pglExperiment": {"pglTask": task_class},
        "pglKeyboardMouse": {"pglKeyBuffer": Mock()},
        "pglImage": {"pglMovieDatabase": Mock()},
        "pglParameter": {"pglParameter": Mock()},
        "pglMessages": {"pglMessages": Mock()},
        "pglTasks": {
            "pglMessageAckTask": HeadlessTask,
            "pglEyeTrackingCalibrationTask": HeadlessTask,
        },
    }
    for name, attributes in dependencies.items():
        module = ModuleType(f"{package_name}.{name}")
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    for name, attributes in {
        "traitlets": {name: Mock() for name in ("Unicode", "Int", "List", "Tuple")},
        "numpy": {"arange": range},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    source = Path(__file__).resolve().parents[1] / "pgl" / "pglDigitalBrain.py"
    spec = importlib.util.spec_from_file_location(
        f"{package_name}.pglDigitalBrain", source
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.pglMovieDatabase.return_value = Mock(
        nStimuli=2,
        stimuli=[
            SimpleNamespace(filename="second.mp4", condition="new"),
            SimpleNamespace(filename="first.mp4", condition="old"),
        ],
    )
    return module


@pytest.mark.parametrize("path_type", [str, Path])
def test_task_uses_prepared_block_without_appending_subject_suffix(
    digitalbrain, tmp_path, path_type
):
    task = digitalbrain.pglDigitalBrainMemoryTask(
        object(), 2, 3, 4, moviePath=path_type(tmp_path)
    )

    assert task.state.blockPath == tmp_path
    assert task.settings.fixedParameters["moviePath"] == str(tmp_path)
    digitalbrain.pglMovieDatabase.assert_called_once_with(tmp_path)
    task.mdb.useManifest.assert_called_once_with(
        filenameColumn="filename", indexColumn="trial_index", conditionColumn="condition"
    )
    assert task.settings.nTrials == 2
    movie_parameter = digitalbrain.pglParameter.call_args_list[0]
    assert movie_parameter.args[0] == "movieNum"
    assert list(movie_parameter.args[1]) == [0, 1]
    assert movie_parameter.kwargs == {"randomize": False}


@pytest.mark.parametrize("path_kwargs", [{}, {"moviePath": None}])
def test_task_preserves_default_root_suffix_and_positional_arguments(
    digitalbrain, path_kwargs
):
    task = digitalbrain.pglDigitalBrainMemoryTask(
        object(), 2, 3, 4, 9, 42, **path_kwargs
    )

    expected = Path("/Users/justin/Desktop/digitalbrain/digital/1234")
    assert task.state.blockPath == expected
    assert task.settings.fixedParameters["moviePath"] == str(expected.parent)
    digitalbrain.pglMovieDatabase.assert_called_once_with(expected)
    assert task.settings.seglen == [0.5, float("inf"), 9, 0.5]
    assert task.settings.fixedParameters["displayWidth"] == 42


def test_configure_forwards_callback_only_to_memory_task(digitalbrain, tmp_path):
    tasks = []
    experiment = SimpleNamespace(pgl=object(), addTask=tasks.append)
    current_run = SimpleNamespace(
        subjectNum=2, dayNum=3, blockNum=4, descriptionLength=9, displayWidth=42
    )
    callback = Mock()

    digitalbrain.pglDigitalBrainConfigure(
        experiment, current_run, moviePath=tmp_path, event_callback=callback
    )

    assert tasks[2].event_callback is callback
    assert all(not hasattr(task, "event_callback") for task in tasks[:2] + tasks[3:])
    callback.assert_not_called()


@pytest.mark.parametrize("path_mode", ["omitted", "none", "str", "path"])
def test_configure_passes_prepared_block_to_memory_task(
    digitalbrain, tmp_path, path_mode
):
    tasks = []
    experiment = SimpleNamespace(pgl=object(), addTask=tasks.append)
    current_run = SimpleNamespace(
        subjectNum=2, dayNum=3, blockNum=4, descriptionLength=9, displayWidth=42
    )
    path_kwargs = {
        "omitted": {},
        "none": {"moviePath": None},
        "str": {"moviePath": str(tmp_path)},
        "path": {"moviePath": tmp_path},
    }[path_mode]

    result = digitalbrain.pglDigitalBrainConfigure(
        experiment, current_run, **path_kwargs
    )

    expected = (
        tmp_path if path_mode in ("str", "path")
        else Path("/Users/justin/Desktop/digitalbrain/digital/1234")
    )
    assert result is experiment
    assert [task.settings.phaseNum for task in tasks] == [0, 1, 2, 3, 4]
    memory_task = tasks[2]
    assert isinstance(memory_task, digitalbrain.pglDigitalBrainMemoryTask)
    assert memory_task.pgl is experiment.pgl
    assert memory_task.state.blockPath == expected
    assert memory_task.settings.seglen == [0.5, float("inf"), 9, 0.5]
    assert memory_task.settings.fixedParameters["displayWidth"] == 42
    digitalbrain.pglMovieDatabase.assert_called_once_with(expected)
