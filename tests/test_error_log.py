import logging
import unittest

from flock_cctv.error_log import ErrorLogBuffer


class ErrorLogBufferTests(unittest.TestCase):
    def setUp(self):
        self.buffer = ErrorLogBuffer(capacity=3)
        self.logger = logging.getLogger("flock_cctv.test_error_log")
        self.logger.addHandler(self.buffer)
        self.logger.propagate = False

    def tearDown(self):
        self.logger.removeHandler(self.buffer)
        self.logger.propagate = True

    def test_keeps_the_log_line_and_exception_type_but_not_exception_text(self):
        self.logger.info("Ignored below warning")
        try:
            raise ValueError("secret message text")
        except ValueError:
            self.logger.exception("Slash command %s failed", "flock stats")
        entries = self.buffer.drain()
        self.assertEqual(len(entries), 1)
        _, level, source, summary = entries[0]
        self.assertEqual((level, source), ("ERROR", "test_error_log"))
        self.assertEqual(summary, "Slash command flock stats failed (ValueError)")
        self.assertNotIn("secret", summary)
        self.assertEqual(self.buffer.drain(), [])

    def test_buffer_is_bounded_and_restore_keeps_order(self):
        for number in range(5):
            self.logger.warning("Problem %d", number)
        entries = self.buffer.drain()
        self.assertEqual([entry[3] for entry in entries], ["Problem 2", "Problem 3", "Problem 4"])
        self.logger.warning("Newer")
        self.buffer.restore(entries[1:])
        self.assertEqual(
            [entry[3] for entry in self.buffer.drain()], ["Problem 3", "Problem 4", "Newer"]
        )


if __name__ == "__main__":
    unittest.main()
