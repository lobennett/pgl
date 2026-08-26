import importlib.util
import sys
import types
from pathlib import Path

import pytest
from PIL import Image


class FakePglTask:
    def __init__(self, pgl=None, phaseNum=0):
        self.pgl = pgl
        self.settings = types.SimpleNamespace()
        self.state = types.SimpleNamespace(currentTrial=0, currentSegment=0)
        self.data = types.SimpleNamespace()


def _load_things_task_with_fake_pgl():
    fake_pgl = types.ModuleType("pgl")
    fake_pgl.pgl = object
    fake_pgl.pglDataPixx = object
    fake_pgl.pglExperiment = object
    fake_pgl.pglTask = FakePglTask
    previous = sys.modules.get("pgl")
    sys.modules["pgl"] = fake_pgl
    try:
        module_path = Path("harness/things_task.py")
        spec = importlib.util.spec_from_file_location("task7_things_task", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            del sys.modules["pgl"]
        else:
            sys.modules["pgl"] = previous


THINGS_TASK_MODULE = _load_things_task_with_fake_pgl()
ThingsTask = THINGS_TASK_MODULE.ThingsTask


class FakeTexture:
    def __init__(self):
        self.display_heights = []

    def display(self, height=None):
        self.display_heights.append(height)


class FakeRenderer:
    def __init__(self):
        self.created_images = []
        self.textures = []
        self.rect_calls = []

    def imageCreate(self, image_data):
        self.created_images.append(image_data.copy())
        texture = FakeTexture()
        self.textures.append(texture)
        return texture

    def rect(self, x, y, width, height, color):
        self.rect_calls.append((x, y, width, height, color))


class FakeDataPixx:
    def __init__(self):
        self.conditions = []

    def writeCondition(self, condition):
        self.conditions.append(condition)


@pytest.fixture
def tiny_image_factory():
    def make(path, index):
        Image.new("RGB", (2, 2), color=(index % 256, 0, 0)).save(path)

    return make


@pytest.fixture
def image_directory(tmp_path, tiny_image_factory):
    for index in range(200):
        tiny_image_factory(tmp_path / f"image-{index:03d}.png", index)
    return tmp_path


def test_constructor_creates_all_200_textures(image_directory):
    renderer = FakeRenderer()

    task = ThingsTask(renderer, FakeDataPixx(), image_directory)

    assert len(task.textures) == 200
    assert len(renderer.created_images) == 200
    assert task.settings.seglen == [0.5, 1.0]
    assert task.settings.nTrials == 200


@pytest.fixture
def preloaded_task(image_directory):
    return ThingsTask(FakeRenderer(), FakeDataPixx(), image_directory)


def test_stimulus_segment_sends_one_based_trial_code(preloaded_task):
    preloaded_task.state.currentTrial = 0
    preloaded_task.state.currentSegment = 0

    preloaded_task.startSegment(12.5)

    assert preloaded_task.data_pixx.conditions == [1]


def test_fixation_segment_does_not_send_trigger(preloaded_task):
    preloaded_task.state.currentTrial = 0
    preloaded_task.state.currentSegment = 1

    preloaded_task.startSegment(13.0)

    assert preloaded_task.data_pixx.conditions == []


def test_update_screen_uses_preloaded_texture_without_decoding(
    preloaded_task, monkeypatch
):
    def fail_if_opened(*args, **kwargs):
        raise AssertionError("image decoding occurred during updateScreen")

    monkeypatch.setattr(Image, "open", fail_if_opened)
    preloaded_task.state.currentTrial = 0
    preloaded_task.state.currentSegment = 0
    preloaded_task.updateScreen()
    preloaded_task.state.currentSegment = 1
    preloaded_task.updateScreen()

    assert preloaded_task.textures[0].display_heights == [18]
    assert preloaded_task.pgl.rect_calls == [
        (0, 0, 0.25, 0.25, [1, 1, 1]),
        (0, 0, 0.25, 0.25, [1, 1, 1]),
    ]


def test_runner_opens_screen_and_configures_conditions_before_preloading(tmp_path):
    events = []

    class FakePgl:
        def devicesAdd(self, device):
            events.append("add_device")

    class FakeExperiment:
        def __init__(self, **kwargs):
            events.append("experiment")

        def initScreen(self):
            events.append("open_screen")

        def addTask(self, task):
            events.append("add_task")

        def run(self):
            events.append("run")

        def endScreen(self):
            events.append("close_screen")

    class FakeDataPixx:
        isActive = True

        def setupConditions(self, **kwargs):
            assert kwargs == {"numBits": 8, "pulseLen": 3}
            events.append("configure_conditions")

        def closeDPx(self):
            events.append("close_dpx")

    class FakeTask:
        def __init__(self, pgl_instance, data_pixx, image_dir):
            assert events == ["experiment", "open_screen", "configure_conditions"]
            events.append("preload_textures")

    THINGS_TASK_MODULE.run(
        tmp_path,
        pgl_factory=FakePgl,
        experiment_factory=FakeExperiment,
        datapixx_factory=FakeDataPixx,
        task_factory=FakeTask,
    )

    assert events == [
        "experiment",
        "open_screen",
        "configure_conditions",
        "preload_textures",
        "add_device",
        "add_task",
        "run",
        "close_dpx",
        "close_screen",
    ]
