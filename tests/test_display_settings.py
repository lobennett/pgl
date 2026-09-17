import uuid
import unittest

import Quartz

from pgl.pglSettings import pglSettingsManager


class DisplaySettingsTests(unittest.TestCase):
    def test_synthesizes_stable_uuid_when_legacy_binding_is_unavailable(self):
        displays = pglSettingsManager.getDisplaySettings()

        expected = {
            str(uuid.uuid5(
                uuid.NAMESPACE_OID,
                "pgl-display:"
                f"{Quartz.CGDisplayVendorNumber(display_id)}:"
                f"{Quartz.CGDisplayModelNumber(display_id)}:"
                f"{Quartz.CGDisplaySerialNumber(display_id)}:"
                f"{Quartz.CGDisplayUnitNumber(display_id)}",
            ))
            for display_id in Quartz.CGGetActiveDisplayList(16, None, None)[1]
        }
        actual = {display.uuid for display in displays if display.uuid != "windowed"}

        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
