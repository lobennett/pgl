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
        self.data = types.SimpleNamespace(endTime=None)
        self.base_end_calls = 0

    def end(self):
        if self.data.endTime is not None:
            return
        self.base_end_calls += 1
        self.data.endTime = 1.0


def _load_things_task_with_fake_pgl():
    fake_pgl = types.ModuleType("pgl")
    fake_pgl.pgl = object
    fake_pgl.pglDataPixx = object
    fake_pgl.pglExperiment = object
    fake_pgl.pglTask = FakePglTask
    previous = sys.modules.get("pgl")
    sys.modules["pgl"] = fake_pgl
    try:
        module_path = Path(__file__).resolve().parents[1] / "things_task.py"
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


def _load_minimal_experiment_with_fake_pgl():
    fake_pgl = types.ModuleType("pgl")
    fake_pgl.pgl = object
    fake_pgl.pglDataPixx = object
    fake_pgl.pglExperiment = object
    previous = sys.modules.get("pgl")
    sys.modules["pgl"] = fake_pgl
    try:
        module_path = Path(__file__).resolve().parents[1] / "minimal_experiment.py"
        spec = importlib.util.spec_from_file_location("task7_minimal_experiment", module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            del sys.modules["pgl"]
        else:
            sys.modules["pgl"] = previous


MINIMAL_EXPERIMENT_MODULE = _load_minimal_experiment_with_fake_pgl()


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
        self.close_calls = 0

    def writeCondition(self, condition):
        self.conditions.append(condition)

    def closeDPx(self):
        self.close_calls += 1


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
    def fail_if_created(*args, **kwargs):
        raise AssertionError("image decoding or creation occurred during updateScreen")

    monkeypatch.setattr(Image, "open", fail_if_created)
    monkeypatch.setattr(THINGS_TASK_MODULE.np, "asarray", fail_if_created)
    monkeypatch.setattr(preloaded_task.pgl, "imageCreate", fail_if_created)
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
    assert not hasattr(preloaded_task, "database")


def test_task_end_closes_dpx_once_and_preserves_base_end(preloaded_task):
    preloaded_task.end()
    preloaded_task.end()

    assert preloaded_task.data_pixx.close_calls == 1
    assert preloaded_task.base_end_calls == 1


def test_task_end_runs_base_end_when_dpx_close_raises(preloaded_task):
    def fail_close():
        raise RuntimeError("DPx close failed")

    preloaded_task.data_pixx.closeDPx = fail_close

    with pytest.raises(RuntimeError, match="DPx close failed"):
        preloaded_task.end()

    assert preloaded_task.base_end_calls == 1


@pytest.mark.parametrize("initial_close_screen", [True, False])
def test_runner_task_end_closes_dpx_before_save_and_screen(
    tmp_path, initial_close_screen
):
    events = []
    experiment_instances = []

    class FakePgl:
        def devicesAdd(self, device):
            events.append("add_device")

    class FakeExperiment:
        def __init__(self, **kwargs):
            events.append("experiment")
            self.settings = types.SimpleNamespace(closeScreenOnEnd=initial_close_screen)
            self.task = None
            experiment_instances.append(self)

        def initScreen(self):
            events.append("open_screen")

        def addTask(self, task):
            events.append("add_task")
            self.task = task

        def run(self):
            events.append("run")
            self.task.end()
            events.append("save")
            self.endScreen()

        def endScreen(self):
            events.append(f"end_screen:{self.settings.closeScreenOnEnd}")
            if self.settings.closeScreenOnEnd:
                events.append("physical_screen_close")

    class FakeDataPixx:
        isActive = True

        def setupConditions(self, **kwargs):
            assert kwargs == {"numBits": 8, "pulseLen": 3}
            events.append("configure_conditions")

        def closeDPx(self):
            events.append("close_dpx")

    class LifecycleTask:
        def __init__(self, pgl_instance, data_pixx, image_dir):
            assert events == ["experiment", "open_screen", "configure_conditions"]
            events.append("preload_textures")
            self.data_pixx = data_pixx
            self.closed = False
            self.ended = False

        def closeDataPixxOnce(self):
            if not self.closed:
                self.data_pixx.closeDPx()
                self.closed = True

        def end(self):
            if self.ended:
                return
            events.append("task_end")
            try:
                self.closeDataPixxOnce()
            finally:
                self.ended = True

    THINGS_TASK_MODULE.run(
        tmp_path,
        pgl_factory=FakePgl,
        experiment_factory=FakeExperiment,
        datapixx_factory=FakeDataPixx,
        task_factory=LifecycleTask,
    )

    expected = [
        "experiment",
        "open_screen",
        "configure_conditions",
        "preload_textures",
        "add_device",
        "add_task",
        "run",
        "task_end",
        "close_dpx",
        "save",
        f"end_screen:{initial_close_screen}",
    ]
    if initial_close_screen:
        expected.append("physical_screen_close")
    assert events == expected
    assert experiment_instances[0].settings.closeScreenOnEnd is initial_close_screen


def test_runner_construction_failure_uses_dpx_close_fallback_before_screen(tmp_path):
    events = []

    class FakePgl:
        def devicesAdd(self, device):
            events.append("add_device")

    class FakeExperiment:
        def __init__(self, **kwargs):
            events.append("experiment")
            self.settings = types.SimpleNamespace(closeScreenOnEnd=True)

        def initScreen(self):
            events.append("open_screen")

        def addTask(self, task):
            events.append("add_task")

        def run(self):
            raise AssertionError("run should not be reached")

        def endScreen(self):
            events.append(f"end_screen:{self.settings.closeScreenOnEnd}")
            if self.settings.closeScreenOnEnd:
                events.append("physical_screen_close")

    class FakeDataPixx:
        isActive = True

        def setupConditions(self, **kwargs):
            events.append("configure_conditions")

        def closeDPx(self):
            events.append("close_dpx")

    class FailingTask:
        def __init__(self, pgl_instance, data_pixx, image_dir):
            events.append("preload_textures")
            raise RuntimeError("texture preload failed")

    with pytest.raises(RuntimeError, match="texture preload failed"):
        THINGS_TASK_MODULE.run(
            tmp_path,
            pgl_factory=FakePgl,
            experiment_factory=FakeExperiment,
            datapixx_factory=FakeDataPixx,
            task_factory=FailingTask,
        )

    assert events == [
        "experiment",
        "open_screen",
        "configure_conditions",
        "preload_textures",
        "close_dpx",
        "end_screen:True",
        "physical_screen_close",
    ]


@pytest.mark.parametrize("end_before_raise", [False, True])
def test_runner_exception_ends_created_task_before_screen(
    tmp_path, end_before_raise
):
    events = []

    class FakePgl:
        def devicesAdd(self, device):
            events.append("add_device")

    class FakeExperiment:
        def __init__(self, **kwargs):
            events.append("experiment")
            self.settings = types.SimpleNamespace(closeScreenOnEnd=True)
            self.task = None

        def initScreen(self):
            events.append("open_screen")

        def addTask(self, task):
            events.append("add_task")
            self.task = task

        def run(self):
            events.append("run")
            if end_before_raise:
                self.task.end()
            events.append("raise")
            raise RuntimeError("run failed")

        def endScreen(self):
            events.append("physical_screen_close")

    class FakeDataPixx:
        isActive = True

        def setupConditions(self, **kwargs):
            events.append("configure_conditions")

        def closeDPx(self):
            events.append("close_dpx")

    class LifecycleTask:
        def __init__(self, pgl_instance, data_pixx, image_dir):
            events.append("preload_textures")
            self.data_pixx = data_pixx
            self.closed = False
            self.ended = False

        def closeDataPixxOnce(self):
            if not self.closed:
                events.append("task_close_dpx")
                self.data_pixx.closeDPx()
                self.closed = True

        def end(self):
            if self.ended:
                return
            events.append("task_end")
            try:
                self.closeDataPixxOnce()
            finally:
                events.append("base_end")
                self.ended = True

    with pytest.raises(RuntimeError, match="run failed"):
        THINGS_TASK_MODULE.run(
            tmp_path,
            pgl_factory=FakePgl,
            experiment_factory=FakeExperiment,
            datapixx_factory=FakeDataPixx,
            task_factory=LifecycleTask,
        )

    assert events.count("task_end") == 1
    assert events.count("base_end") == 1
    assert events.count("task_close_dpx") == 1
    assert events.count("close_dpx") == 1
    assert events[-1] == "physical_screen_close"


def test_minimal_runner_orders_pre_image_code_before_texture_creation(tmp_path):
    events = []

    class FakeTexture:
        def display(self, **kwargs):
            events.append(("display", kwargs))

    class FakePgl:
        def __init__(self):
            events.append("construct_pgl")

        def imageCreate(self, image_data):
            events.append("create_texture")
            return FakeTexture()

        def flush(self):
            events.append("flush")

        def waitSecs(self, seconds):
            events.append(("wait", seconds))

    class FakeExperiment:
        def __init__(self, **kwargs):
            events.append("experiment")

        def initScreen(self):
            events.append("open_screen")

        def endScreen(self):
            events.append("physical_screen_close")

    class FakeDataPixx:
        def __init__(self):
            events.append("construct_dpx")
            self.isActive = True

        class dp:
            @staticmethod
            def DPxGetFirmwareRev():
                events.append("firmware")
                return 42

            @staticmethod
            def DPxIsDoutPixelMode():
                events.append("pixel_mode")
                return False

        def setupConditions(self, **kwargs):
            events.append(("configure_conditions", kwargs))

        def writeCondition(self, code):
            events.append(("condition", code))

        def closeDPx(self):
            events.append("close_dpx")

    class FakeImage:
        def __enter__(self):
            events.append("open_image")
            return self

        def __exit__(self, *args):
            return False

        def convert(self, mode):
            events.append(("convert", mode))
            return self

        def copy(self):
            events.append("copy")
            return "image-copy"

    MINIMAL_EXPERIMENT_MODULE.run(
        tmp_path / "image.png",
        pgl_factory=FakePgl,
        experiment_factory=FakeExperiment,
        datapixx_factory=FakeDataPixx,
        image_open=lambda path: FakeImage(),
        asarray=lambda image: events.append("asarray") or image,
    )

    assert events == [
        "construct_pgl",
        "experiment",
        "open_screen",
        "construct_dpx",
        ("configure_conditions", {"numBits": 8, "pulseLen": 3}),
        "firmware",
        "pixel_mode",
        ("condition", 17),
        "open_image",
        ("convert", "RGB"),
        "copy",
        "asarray",
        "create_texture",
        ("display", {"height": 18}),
        "flush",
        ("wait", 0.5),
        ("condition", 18),
        "close_dpx",
        "physical_screen_close",
    ]


def test_minimal_runner_closes_dpx_before_screen_after_failure(tmp_path):
    events = []

    class FakePgl:
        def imageCreate(self, image_data):
            events.append("create_texture")
            raise RuntimeError("texture creation failed")

    class FakeExperiment:
        def __init__(self, **kwargs):
            events.append("experiment")

        def initScreen(self):
            events.append("open_screen")

        def endScreen(self):
            events.append("physical_screen_close")

    class FakeDataPixx:
        isActive = True

        class dp:
            @staticmethod
            def DPxGetFirmwareRev():
                return 42

            @staticmethod
            def DPxIsDoutPixelMode():
                return False

        def setupConditions(self, **kwargs):
            events.append("configure_conditions")

        def writeCondition(self, code):
            events.append(("condition", code))

        def closeDPx(self):
            events.append("close_dpx")

    class FakeImage:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def convert(self, mode):
            return self

        def copy(self):
            return "image-copy"

    with pytest.raises(RuntimeError, match="texture creation failed"):
        MINIMAL_EXPERIMENT_MODULE.run(
            tmp_path / "image.png",
            pgl_factory=FakePgl,
            experiment_factory=FakeExperiment,
            datapixx_factory=FakeDataPixx,
            image_open=lambda path: FakeImage(),
            asarray=lambda image: image,
        )

    assert events[-2:] == ["close_dpx", "physical_screen_close"]
