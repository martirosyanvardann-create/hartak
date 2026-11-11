import os
import unittest
from unittest import mock

from pageload.chrome import extra_chrome_flags


class ChromeFlagsTest(unittest.TestCase):
    def test_flags_from_environment(self):
        with mock.patch.dict(os.environ, {"CHROME_FLAGS": "--no-sandbox  --disable-dev-shm-usage"}):
            self.assertEqual(extra_chrome_flags(), ["--no-sandbox", "--disable-dev-shm-usage"])

    def test_no_flags_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(extra_chrome_flags(), [])


if __name__ == "__main__":
    unittest.main()
