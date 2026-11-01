import csv
import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import TypedDict, cast

from report_io import atomic_report_file

CSV_FIELD_SIZE_LIMIT = 131_072
ASCII_UPPER_TRANSLATION = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)


class Certificate(TypedDict):
    manufacturer: str
    product_group: str
    masks: list[str]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"В JSON повторяется ключ {key!r}")
        result[key] = value
    return result


def load_certificate(path: str | Path) -> object:
    """Читает JSON в UTF-8, допуская BOM и отклоняя повторяющиеся ключи."""
    with open(path, encoding="utf-8-sig") as file:
        return json.load(file, object_pairs_hook=_unique_json_object)


def validate_certificate(certificate: object) -> None:
    """Проверяет обязательные поля и формат масок, при ошибке вызывает ValueError."""
    if not isinstance(certificate, dict):
        raise ValueError("Сертификат должен быть JSON-объектом")

    for field in ("manufacturer", "product_group"):
        value = certificate.get(field)

        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Поле '{field}' должно быть непустой строкой")

    masks = certificate.get("masks")

    if not isinstance(masks, list) or not masks:
        raise ValueError("Поле 'masks' должно быть непустым списком")

    for number, mask in enumerate(masks, start=1):
        if not isinstance(mask, str) or not mask.strip():
            raise ValueError(f"Маска №{number} должна быть непустой строкой")

        if not re.fullmatch(r"[A-Z0-9]{3}\*{4}", mask):
            raise ValueError(
                f"Маска №{number}: ожидаются три символа A–Z/0–9 "
                "и ровно четыре '*', например '4VB****'"
            )


def _read_catalog(path: str | Path) -> Iterator[dict[str, str]]:
    """Выдаёт строки CSV после проверки заголовков и числа полей.

    Временно задаёт общий CSV-лимит и восстанавливает его при завершении.
    """
    previous_limit = csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)
    try:
        with open(path, encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file, strict=True)
            columns = reader.fieldnames

            if not columns:
                raise ValueError("В каталоге отсутствует строка заголовков")

            for field in ("article", "manufacturer", "product_group"):
                if field not in columns:
                    raise ValueError(f"В каталоге отсутствует колонка '{field}'")

            if len(columns) != len(set(columns)):
                raise ValueError("В каталоге есть повторяющиеся названия колонок")

            for number, row in enumerate(reader, start=1):
                if None in row or None in row.values():
                    raise ValueError(
                        f"Запись №{number}: количество полей не совпадает с заголовком"
                    )
                yield cast(dict[str, str], row)
    finally:
        csv.field_size_limit(previous_limit)


def normalize_article(article: str) -> str:
    """Переводит a-z в A-Z, заменяет неразрывные пробелы обычными.

    Убирает только обычные пробелы по краям.
    """
    return article.replace("\u00a0", " ").translate(ASCII_UPPER_TRANSLATION).strip(" ")


def process_catalog(path: str | Path, certificate: object) -> list[dict[str, str]]:
    """Возвращает строки каталога с ключом, статусом и причиной.

    Сначала проверяет сертификат. Ошибки входа передаёт вызывающему коду.
    """
    validate_certificate(certificate)
    validated_certificate = cast(Certificate, certificate)

    patterns = [
        re.compile(re.escape(mask[:3]) + r"[A-Z0-9]{4}")
        for mask in validated_certificate["masks"]
    ]
    results: list[dict[str, str]] = []

    for row in _read_catalog(path):
        search_key = normalize_article(row["article"])
        reasons = []

        if row["manufacturer"] != validated_certificate["manufacturer"]:
            reasons.append("Не совпадает производитель")
        if row["product_group"] != validated_certificate["product_group"]:
            reasons.append("Не совпадает товарная группа")
        if not any(pattern.fullmatch(search_key) for pattern in patterns):
            reasons.append("Артикул не соответствует маскам")

        results.append(
            {
                **row,
                "search_key": search_key,
                "status": "declined" if reasons else "accepted",
                "reason": "; ".join(reasons),
            }
        )

    return results


def _write_csv(
    rows: Iterable[dict[str, str]], path: str | Path, columns: list[str]
) -> None:
    with atomic_report_file(path) as file:
        writer = csv.writer(file)
        writer.writerow(columns)

        for row in rows:
            writer.writerow([row[column] for column in columns])


def write_results(results: list[dict[str, str]], path: str | Path) -> None:
    """Сохраняет первую принятую строку каждого ключа в порядке появления."""
    columns = ["article", "manufacturer", "product_group", "search_key"]
    accepted: dict[str, dict[str, str]] = {}

    for result in results:
        if result["status"] == "accepted":
            accepted.setdefault(result["search_key"], result)

    _write_csv(accepted.values(), path, columns)


def write_exceptions(results: list[dict[str, str]], path: str | Path) -> None:
    """Сохраняет отклонённые строки в исходном порядке, включая дубли."""
    columns = ["article", "manufacturer", "product_group", "search_key", "reason"]
    declined = (result for result in results if result["status"] == "declined")
    _write_csv(declined, path, columns)


def main() -> int:
    directory = Path(__file__).resolve().parent
    report_path = directory / "report.csv"
    exceptions_path = directory / "exceptions.csv"

    try:
        certificate = load_certificate(directory / "certificate.json")
    except (OSError, ValueError) as error:
        print(f"Ошибка чтения сертификата: {error}", file=sys.stderr)
        return 1

    try:
        results = process_catalog(directory / "catalog.csv", certificate)
    except (OSError, ValueError, csv.Error) as error:
        print(
            f"Ошибка проверки входных данных или обработки каталога: {error}",
            file=sys.stderr,
        )
        return 1

    try:
        write_results(results, report_path)
    except (OSError, ValueError, csv.Error) as error:
        print(f"Ошибка записи отчёта: {error}", file=sys.stderr)
        return 1

    try:
        write_exceptions(results, exceptions_path)
    except (OSError, ValueError, csv.Error) as error:
        print(
            f"Ошибка записи списка исключений: {error}. "
            f"Итоговый отчёт уже сохранён: {report_path}",
            file=sys.stderr,
        )
        return 1

    print(f"Результат сохранён: {report_path}")
    print(f"Список исключений сохранён: {exceptions_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
