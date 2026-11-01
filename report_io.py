from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TextIO


@contextmanager
def atomic_report_file(path: str | Path) -> Iterator[TextIO]:
    """Даёт временный файл UTF-8 с BOM для записи отчёта.

    Заменяет целевой файл только после успешной записи и закрытия.
    Временный файл удаляется в finally, если файловая система позволяет.
    """
    target = Path(path)
    temporary_path: Path | None = None

    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            yield file
        temporary_path.replace(target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
