"""Smoke tests for LNQStudio. Runs in Slicer's CTest environment.

These tests do NOT require a live Chronicle backend. The validator and
client are exercised end-to-end by SlicerLNQ-Chronicler/bin/test-design.sh
against a real CouchDB instance.
"""

import unittest

import slicer


class LNQStudioSmokeTest(unittest.TestCase):

    def test_module_registered(self):
        self.assertIn("LNQStudio", slicer.app.moduleManager().modulesNames())

    def test_module_loadable(self):
        slicer.util.selectModule("LNQStudio")
        widget = slicer.modules.lnqstudio.widgetRepresentation()
        self.assertIsNotNone(widget)

    def test_chronicle_client_imports(self):
        from LNQStudioLib.chronicle_client import ChronicleClient, ChronicleError, new_id
        self.assertTrue(callable(ChronicleClient))
        self.assertTrue(issubclass(ChronicleError, Exception))
        self.assertTrue(new_id("Cohort").startswith("cohort:"))


if __name__ == "__main__":
    unittest.main()
