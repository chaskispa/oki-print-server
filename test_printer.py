import unittest

from printer import Printer, encode_text


class EncodeTextTests(unittest.TestCase):
    def test_unsupported_uppercase_accents_fall_back_to_plain_letters(self):
        self.assertEqual(encode_text("ÁÍÓÚÀÈÌÒÙÃÕ", "cp437"), b"AIOUAEIOUAO")

    def test_supported_accented_letters_are_preserved(self):
        self.assertEqual(
            encode_text("ÉÑÜáéíóúñü", "cp437"),
            "ÉÑÜáéíóúñü".encode("cp437"),
        )

    def test_decomposed_accent_is_normalized_before_encoding(self):
        self.assertEqual(encode_text("A\N{COMBINING TILDE}", "ascii"), b"A")

    def test_prepare_uses_accent_fallback_and_keeps_job_ending(self):
        printer = Printer(encoding="cp437", trailing_lines=1)
        self.assertEqual(printer.prepare("ÁRBOL"), b"ARBOL\r\n")


if __name__ == "__main__":
    unittest.main()
