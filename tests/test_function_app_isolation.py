import unittest
from pathlib import Path


FUNCTION_APP_PATH = Path(__file__).resolve().parents[1] / "function_app.py"


class TestFunctionAppIsolation(unittest.TestCase):
    def test_timeliness_functions_not_in_main_function_app(self):
        source = FUNCTION_APP_PATH.read_text(encoding="utf-8")

        self.assertIn("def TFLMonitor(", source)
        self.assertNotIn("def TrainTimelinessMonitor(", source)
        self.assertNotIn("def TrainTimelinessAdmin(", source)
        self.assertNotIn("train_timeliness", source)


if __name__ == "__main__":
    unittest.main()
