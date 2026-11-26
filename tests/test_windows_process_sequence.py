"""Validation of Windows process sequence buffers without Windows process access."""
import ctypes
import unittest

from mo2_codex_bridge.running_apps import _BasicProcessInformation, WindowsProcessObserver


def entry(pid, sequence, next_offset=0, parent=0):
    return bytes(_BasicProcessInformation(next_offset=next_offset, pid=pid, parent_pid=parent, sequence=sequence))


class ProcessSequenceBufferTests(unittest.TestCase):
    def parse(self, buffer, used=None):
        return WindowsProcessObserver._sequence_rows(buffer, len(buffer) if used is None else used)

    def test_reads_variable_entry_padding_and_ignores_idle_process(self):
        first = entry(0, 0, ctypes.sizeof(_BasicProcessInformation) + 16)
        second = entry(4, 900, ctypes.sizeof(_BasicProcessInformation))
        self.assertEqual(self.parse(first + bytes(16) + second + entry(144, 950, parent=4)), {4: (0, 900), 144: (4, 950)})

    def test_duplicate_pid_is_an_invalid_snapshot(self):
        buffer = entry(5, 910, ctypes.sizeof(_BasicProcessInformation)) + entry(5, 911)
        with self.assertRaisesRegex(ValueError, "Duplicate PID"):
            self.parse(buffer)

    def test_short_first_or_following_entry_cannot_be_parsed_as_complete(self):
        first = entry(5, 901, ctypes.sizeof(_BasicProcessInformation))
        last = entry(7, 903)
        with self.assertRaises(ValueError):
            self.parse(first[:-1])
        with self.assertRaises(ValueError):
            self.parse(first + last, used=len(first + last) - 1)

    def test_nonadvancing_or_misaligned_offsets_fail(self):
        for offset in (1, ctypes.sizeof(_BasicProcessInformation) + 1, 2**32 - 8):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.parse(entry(10, 930, offset) + entry(11, 931))

    def test_no_live_process_identities_is_not_success(self):
        with self.assertRaises(ValueError):
            self.parse(entry(0, 0))


if __name__ == "__main__":
    unittest.main()
