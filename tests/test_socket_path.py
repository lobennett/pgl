import tempfile
import unittest
from pathlib import Path

from pgl.pglBase import pglBase


class MetalSocketPathTests(unittest.TestCase):
    def test_prepare_metal_socket_path_creates_private_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            socket_path = Path(pglBase.prepareMetalSocketPath(home))

            self.assertEqual(
                socket_path,
                home / "Library" / "Containers" / "gru.mglMetal" / "Data",
            )
            self.assertTrue(socket_path.is_dir())
            self.assertEqual(socket_path.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
