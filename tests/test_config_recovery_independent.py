"""Independent regression for newer work arriving during temp-file staging."""
import unittest
from pathlib import Path
from unittest import mock

import test_config_recovery as config_tests
from mo2_codex_bridge import common


class ConfigurationStagingConcurrencyTests(unittest.TestCase):
    setUp = config_tests.ConfigurationRecoveryTests.setUp
    tearDown = config_tests.ConfigurationRecoveryTests.tearDown
    change = config_tests.ConfigurationRecoveryTests.change
    write = config_tests.ConfigurationRecoveryTests.write

    def test_external_edit_during_atomic_staging_is_preserved(self):
        external = b'newer work written while bridge stages its temporary file'
        real_mkstemp = common.tempfile.mkstemp
        edits = []
        def change_during_staging(*args, **kwargs):
            result = real_mkstemp(*args, **kwargs)
            if Path(kwargs.get('dir', '')).resolve() == self.path.parent.resolve():
                self.path.write_bytes(external)
                edits.append(True)
            return result
        rejection = None
        with mock.patch.object(common.tempfile, 'mkstemp', side_effect=change_during_staging):
            try:
                self.write()
            except ValueError as error:
                rejection = error
        self.assertEqual(edits, [True])
        self.assertEqual(self.path.read_bytes(), external, 'Newer external edits were overwritten during staging')
        self.assertIsNotNone(rejection, 'The bridge must reject the stale write after staging')


if __name__ == '__main__':
    unittest.main(verbosity=2)
