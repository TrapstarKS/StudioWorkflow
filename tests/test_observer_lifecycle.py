from pathlib import Path
import shutil
import subprocess
import unittest


class ObserverLifecycleTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("lune"), "Lune is required to execute the full observer harness")
    def test_complete_observer_network_and_play_lifecycle(self):
        workflow = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("lune"), "run", str(workflow / "tests/observer_lifecycle.luau"),
             str(workflow / "studioworkflow/observer.luau")],
            capture_output=True, text=True, timeout=55,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
