"""The Naming Lab launch command always binds `127.0.0.1`, unconditionally."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from tools.naming_lab.cli import BIND_ADDRESS, build_streamlit_command, main


class BuildStreamlitCommandTests(unittest.TestCase):
    def test_binds_loopback_address(self):
        command = build_streamlit_command(Path("/tmp/some-corpus"))
        self.assertIn("--server.address", command)
        self.assertEqual(command[command.index("--server.address") + 1], "127.0.0.1")

    def test_bind_address_constant_is_loopback(self):
        self.assertEqual(BIND_ADDRESS, "127.0.0.1")

    def test_no_parameter_can_change_the_bind_address(self):
        # The function's own signature has no address/host parameter to override.
        parameters = inspect.signature(build_streamlit_command).parameters
        self.assertNotIn("address", parameters)
        self.assertNotIn("host", parameters)
        self.assertNotIn("bind", parameters)

    def test_corpus_is_resolved_and_passed_through(self):
        command = build_streamlit_command(Path("."))
        self.assertIn("--corpus", command)
        corpus_value = command[command.index("--corpus") + 1]
        self.assertTrue(Path(corpus_value).is_absolute())

    def test_headless_flag_is_set(self):
        command = build_streamlit_command(Path("/tmp/some-corpus"))
        self.assertIn("--server.headless", command)


class MainRejectsMissingCorpusDirectoryTests(unittest.TestCase):
    def test_nonexistent_corpus_directory_is_rejected(self):
        exit_code = main(["naming-lab", "--corpus", "/definitely/does/not/exist/naming-lab-xyz"])
        self.assertEqual(exit_code, 2)

    def test_no_subcommand_returns_nonzero(self):
        self.assertEqual(main([]), 2)


if __name__ == "__main__":
    unittest.main()
