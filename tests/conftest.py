"""Portabilidade Windows: diretórios temporários com SQLite ainda aberto não podem ser apagados (WinError 32).
Tolera erros de limpeza em TemporaryDirectory (Python >= 3.10)."""

import tempfile

_Orig = tempfile.TemporaryDirectory


class _TolerantTemporaryDirectory(_Orig):  # type: ignore[misc, valid-type]
    def __init__(self, *a, **k):
        k.setdefault("ignore_cleanup_errors", True)
        super().__init__(*a, **k)


tempfile.TemporaryDirectory = _TolerantTemporaryDirectory  # type: ignore[misc]
