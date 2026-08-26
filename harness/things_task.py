"""Hardware-only THINGS task with constructor-preloaded image textures."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

from pgl import pgl, pglDataPixx, pglExperiment, pglTask


SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


class ThingsTask(pglTask):
    """Show one preloaded THINGS image, then a fixation marker, on every trial."""

    def __init__(
        self,
        pgl_instance,
        data_pixx,
        image_dir,
        image_count=200,
        image_height=18,
    ):
        super().__init__(pgl_instance)
        self.data_pixx = data_pixx
        self._data_pixx_closed = False
        self.image_height = image_height

        image_dir = Path(image_dir)
        image_paths = sorted(
            path
            for path in image_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
        )
        if len(image_paths) < image_count:
            raise ValueError(
                f"Need at least {image_count} supported images in {image_dir}; "
                f"found {len(image_paths)}."
            )

        self.image_paths = image_paths[:image_count]
        self.textures = []
        for image_path in self.image_paths:
            with Image.open(image_path) as image:
                image_data = np.asarray(image.convert("RGB").copy())
            texture = self.pgl.imageCreate(image_data)
            if texture is None:
                raise RuntimeError(f"Could not create texture for {image_path}")
            self.textures.append(texture)

        self.settings.taskName = "THINGS USB Trigger Task"
        self.settings.seglen = [0.5, 1.0]
        self.settings.nTrials = image_count
        self.settings.fixedParameters = {
            "imageDirectory": str(image_dir.resolve()),
            "imageCount": image_count,
            "imageHeight": image_height,
        }

    def startSegment(self, update_time):
        if self.state.currentSegment == 0:
            self.data_pixx.writeCondition(self.state.currentTrial + 1)

    def updateScreen(self):
        if self.state.currentSegment == 0:
            self.textures[self.state.currentTrial].display(height=self.image_height)
        self.pgl.rect(0, 0, 0.25, 0.25, color=[1, 1, 1])

    def closeDataPixxOnce(self):
        if self._data_pixx_closed:
            return
        self.data_pixx.closeDPx()
        self._data_pixx_closed = True

    def end(self):
        try:
            self.closeDataPixxOnce()
        finally:
            super().end()


def run(
    image_dir,
    *,
    pgl_factory=pgl,
    experiment_factory=pglExperiment,
    datapixx_factory=pglDataPixx,
    task_factory=ThingsTask,
):
    """Run the hardware-only THINGS task after explicitly opening Metal first."""
    pgl_instance = pgl_factory()
    experiment = experiment_factory(
        pgl=pgl_instance,
        experimentName="usbFailureThings",
        subjectID="s0000",
    )
    data_pixx = None
    task = None
    run_completed = False
    try:
        experiment.initScreen()
        data_pixx = datapixx_factory()
        if not data_pixx.isActive:
            raise RuntimeError("DATAPixx is not active")
        data_pixx.setupConditions(numBits=8, pulseLen=3)
        task = task_factory(pgl_instance, data_pixx, image_dir)
        pgl_instance.devicesAdd(data_pixx)
        experiment.addTask(task)
        experiment.run()
        run_completed = True
    finally:
        try:
            if task is not None:
                task.closeDataPixxOnce()
            elif data_pixx is not None:
                data_pixx.closeDPx()
        finally:
            if not run_completed:
                experiment.endScreen()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-dir", required=True, type=Path)
    args = parser.parse_args()
    print("HARDWARE-ONLY: requires an attached, powered DATAPixx and a Metal display.")
    run(args.image_dir)


if __name__ == "__main__":
    main()
