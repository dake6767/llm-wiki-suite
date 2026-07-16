from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from minisign_verify import verify_ed25519, verify_tauri_minisign


class MinisignVerificationTests(unittest.TestCase):
    def test_rfc8032_ed25519_vector(self):
        public_key = bytes.fromhex(
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
        )
        signature = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        )
        self.assertTrue(verify_ed25519(public_key, b"", signature))
        self.assertFalse(verify_ed25519(public_key, b"changed", signature))

    def test_tauri_encoded_prehashed_minisign_fixture(self):
        public_box = base64.b64encode(
            b"untrusted comment: minisign public key E7620F1842B4E81F\n"
            b"RWQf6LRCGA9i53mlYecO4IzT51TGPpvWucNSCh1CBM0QTaLn73Y7GFO3\n"
        ).decode()
        signature_box = base64.b64encode(
            b"untrusted comment: signature from minisign secret key\n"
            b"RUQf6LRCGA9i559r3g7V1qNyJDApGip8MfqcadIgT9CuhV3EMhHoN1mGTkUidF/"
            b"z7SrlQgXdy8ofjb7bNJJylDOocrCo8KLzZwo=\n"
            b"trusted comment: timestamp:1556193335\tfile:test\n"
            b"y/rUw2y8/hOUYjZU71eHp/Wo1KZ40fGy2VJEDl34XMJM+TX48Ss/17u3IvIfbVR1"
            b"FkZZSNCisQbuQY+bHwhEBg==\n"
        ).decode()
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "test"
            artifact.write_bytes(b"test")
            verify_tauri_minisign(artifact, public_box, signature_box)
            artifact.write_bytes(b"Test")
            with self.assertRaisesRegex(ValueError, "release signature"):
                verify_tauri_minisign(artifact, public_box, signature_box)


if __name__ == "__main__":
    unittest.main()
