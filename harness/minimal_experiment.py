"""Smallest hardware-only pgl screen, texture, and DATAPixx trigger check."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

from pgl import pgl, pglDataPixx, pglExperiment


def run(
    image_path,
    *,
    pgl_factory=pgl,
    experiment_factory=pglExperiment,
    datapixx_factory=pglDataPixx,
    image_open=Image.open,
    asarray=np.asarray,
):
    """Run the strict six-step hardware check; do not invoke in automated tests."""
    pgl_instance = None
    experiment = None
    data_pixx = None
    texture = None
    try:
        # 1. Open Metal before making a texture.
        pgl_instance = pgl_factory()
        experiment = experiment_factory(
            pgl=pgl_instance,
            experimentName="usbFailureMinimal",
            subjectID="s0000",
        )
        experiment.initScreen()
        # 2. Construct and verify the DATAPixx connection.
        data_pixx = datapixx_factory()
        if not data_pixx.isActive:
            raise RuntimeError("DATAPixx is not active")
        # 3. Preload the eight-bit trigger condition table and query device state.
        data_pixx.setupConditions(numBits=8, pulseLen=3)
        print(f"DATAPixx firmware: {data_pixx.dp.DPxGetFirmwareRev()}")
        print(f"DATAPixx pixel mode: {data_pixx.dp.DPxIsDoutPixelMode()}")
        # 4. Send the known pre-image condition.
        data_pixx.writeCondition(17)
        # 5. Create, display, flush, and retain one texture for 500 ms.
        with image_open(image_path) as image:
            image_data = asarray(image.convert("RGB").copy())
        texture = pgl_instance.imageCreate(image_data)
        if texture is None:
            raise RuntimeError(f"Could not create texture for {image_path}")
        texture.display(height=18)
        pgl_instance.flush()
        pgl_instance.waitSecs(0.5)
        # 6. Send the known post-image condition.
        data_pixx.writeCondition(18)
    finally:
        try:
            if data_pixx is not None:
                data_pixx.closeDPx()
        finally:
            if experiment is not None:
                experiment.endScreen()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, default=Path("testimage.jpg"))
    args = parser.parse_args()
    print("HARDWARE-ONLY: requires an attached, powered DATAPixx and a Metal display.")
    run(args.image)


if __name__ == "__main__":
    main()
