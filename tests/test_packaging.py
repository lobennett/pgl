import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


class WheelContentsTests(unittest.TestCase):
    def test_wheel_contains_native_metal_renderer(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary_directory:
            wheel_directory = Path(temporary_directory)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--wheel-dir",
                    str(wheel_directory),
                    str(repository),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            wheel_path = next(wheel_directory.glob("pgl-*.whl"))
            with zipfile.ZipFile(wheel_path) as wheel:
                packaged_files = set(wheel.namelist())

        self.assertIn("metal/mglCommandTypes.h", packaged_files)
        self.assertIn(
            "metal/mglMetal.app/Contents/MacOS/mglMetal",
            packaged_files,
        )


if __name__ == "__main__":
    unittest.main()
