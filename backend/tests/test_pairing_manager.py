import importlib.util
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "services" / "pairing_manager.py"
spec = importlib.util.spec_from_file_location("pairing_manager", MODULE_PATH)
if spec is None or spec.loader is None:
    raise ImportError("pairing_manager module spec not found")
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)
PairingManager = pm.PairingManager


class TestPairingManager(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop(PairingManager.ENV_KEY, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = os.path.join(self._tmp.name, "pairing.json")

    def tearDown(self):
        if self._saved is not None:
            os.environ[PairingManager.ENV_KEY] = self._saved
        self._tmp.cleanup()

    def test_first_get_generates_and_persists(self):
        mgr = PairingManager(filepath=self.cfg_path)
        token = mgr.get_token()
        self.assertTrue(token)
        # 同一文件第二次读取应为同一 token（持久化）
        mgr2 = PairingManager(filepath=self.cfg_path)
        self.assertEqual(mgr2.get_token(), token)
        self.assertTrue(mgr2.has_custom_token())

    def test_verify_accepts_exact_and_rejects_wrong(self):
        mgr = PairingManager(filepath=self.cfg_path)
        token = mgr.get_token()
        self.assertTrue(mgr.verify(token))
        self.assertFalse(mgr.verify(token + "x"))
        self.assertFalse(mgr.verify(""))
        self.assertFalse(mgr.verify(None))

    def test_regenerate_rotates_token(self):
        mgr = PairingManager(filepath=self.cfg_path)
        old = mgr.get_token()
        new = mgr.regenerate()
        self.assertNotEqual(old, new)
        self.assertTrue(mgr.verify(new))
        self.assertFalse(mgr.verify(old))

    def test_env_override_wins_and_blocks_regenerate(self):
        os.environ[PairingManager.ENV_KEY] = "env-token-123"
        mgr = PairingManager(filepath=self.cfg_path)
        self.assertEqual(mgr.get_token(), "env-token-123")
        self.assertTrue(mgr.verify("env-token-123"))
        with self.assertRaises(RuntimeError):
            mgr.regenerate()


if __name__ == "__main__":
    unittest.main()
