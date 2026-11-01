import contextlib
import csv
import io
import json
import subprocess
import sys
import tempfile
import unittest
from itertools import permutations
from pathlib import Path
from typing import Any
from unittest.mock import patch

import script
from script import (
    load_certificate,
    normalize_article,
    process_catalog,
    validate_certificate,
    write_exceptions,
    write_results,
)

REPORT_TEST_COLUMNS = ["article", "manufacturer", "product_group", "search_key"]

HI_LEX_TEST_MASKS = (
    "4VB****",
    "4JB****",
    "4AB****",
    "9TB****",
    "4TB****",
    "4HB****",
    "9CB****",
    "9NB****",
    "4GB****",
    "4RB****",
    "4PB****",
    "9VB****",
    "4UB****",
    "4CB****",
    "4NB****",
    "4DB****",
    "9AB****",
    "4FB****",
    "4BB****",
    "4ZB****",
    "9HB****",
    "4MB****",
    "9DB****",
    "9ZB****",
    "1PB****",
    "5PB****",
    "9JB****",
    "2HB****",
    "5CB****",
    "2JB****",
    "2PB****",
    "9RB****",
    "9YB****",
    "2KB****",
    "9GB****",
    "2YB****",
    "7HB****",
    "9BB****",
)


class ScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.addCleanup(csv.field_size_limit, csv.field_size_limit())
        self.path = Path(directory.name) / "catalog.csv"
        self.certificate: dict[str, Any] = {
            "manufacturer": "HI-LEX CORPORATION",
            "product_group": "тросы стояночного тормоза",
            "masks": ["4VB****", "4JB****"],
        }

    def make_catalog(
        self,
        rows: list[list[str]],
        columns: tuple[str, ...] = ("article", "manufacturer", "product_group"),
    ) -> None:
        with self.path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(columns)
            writer.writerows(rows)

    def product(self, article: str) -> list[str]:
        return [
            article,
            self.certificate["manufacturer"],
            self.certificate["product_group"],
        ]

    def test_load_certificate(self) -> None:
        self.path.write_text(
            json.dumps(self.certificate, ensure_ascii=False),
            encoding="utf-8-sig",
        )
        self.assertEqual(load_certificate(self.path), self.certificate)
        self.path.write_text('{"masks":', encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            load_certificate(self.path)

    def test_load_certificate_rejects_duplicate_keys(self) -> None:
        original = json.dumps(self.certificate)
        cases = [
            ("manufacturer", original[:-1] + ', "manufacturer": "OTHER"}'),
            ("manufacturer", '{"manufacturer": "WRONG", ' + original[1:]),
            ("manufacturer", original[:-1] + ', "\\u006danufacturer": "OTHER"}'),
            ("product_group", original[:-1] + ', "product_group": "other"}'),
            ("masks", original[:-1] + ', "masks": ["4VB****", "4JB****"]}'),
            ("note", original[:-1] + ', "note": "first", "note": "last"}'),
            (
                "name",
                original[:-1] + ', "metadata": {"name": "first", "name": "last"}}',
            ),
            (
                "name",
                original[:-1] + ', "metadata": [{"name": "first", "name": "last"}]}',
            ),
        ]
        for key, text in cases:
            for encoding in ("utf-8", "utf-8-sig"):
                with self.subTest(key=key, text=text, encoding=encoding):
                    self.path.write_text(text, encoding=encoding)
                    with self.assertRaisesRegex(ValueError, key):
                        load_certificate(self.path)

    def test_load_certificate_allows_same_key_in_separate_objects(self) -> None:
        certificate = {
            **self.certificate,
            "metadata": {
                "manufacturer": "source",
                "items": [{"name": "first"}, {"name": "second"}],
            },
        }
        self.path.write_text(json.dumps(certificate), encoding="utf-8")
        loaded = load_certificate(self.path)
        self.assertEqual(loaded, certificate)
        self.assertIsNone(validate_certificate(loaded))

    def test_validate_certificate(self) -> None:
        self.assertIsNone(validate_certificate(self.certificate))
        for value in (None, [], "certificate", 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_certificate(value)
        for field in self.certificate:
            invalid = self.certificate.copy()
            del invalid[field]
            with self.subTest(missing=field), self.assertRaisesRegex(ValueError, field):
                validate_certificate(invalid)
        for field in ("manufacturer", "product_group"):
            for value in (None, "", " \t ", 123, []):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(ValueError, field):
                        validate_certificate({**self.certificate, field: value})

    def test_validate_masks(self) -> None:
        for masks in (
            None,
            [],
            "4VB****",
            [None],
            [123],
            [""],
            [" "],
            ["4VB***"],
            ["4VB*****"],
            ["4VB1234"],
            ["4vb****"],
            ["4VВ****"],
            [" 4VB****"],
            ["4VB****\n"],
        ):
            with self.subTest(masks=masks), self.assertRaises(ValueError):
                validate_certificate({**self.certificate, "masks": masks})
        validate_certificate({**self.certificate, "masks": ["4QB****"]})

    def test_catalog_structure(self) -> None:
        self.make_catalog([["", "", ""]])
        self.assertEqual(
            [row["status"] for row in process_catalog(self.path, self.certificate)],
            ["declined"],
        )
        for text in (
            "",
            "article,manufacturer\n",
            "article,manufacturer,product_group,article\n",
            "article,manufacturer,product_group\na,b\n",
            "article,manufacturer,product_group\na,b,c,d\n",
        ):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    process_catalog(self.path, self.certificate)

    def test_catalog_syntax_and_missing_file(self) -> None:
        self.path.write_text(
            'article,manufacturer,product_group\n"unfinished',
            encoding="utf-8",
        )
        with self.assertRaises(csv.Error):
            process_catalog(self.path, self.certificate)
        with self.assertRaises(FileNotFoundError):
            process_catalog(self.path.with_name("missing.csv"), self.certificate)

    def test_catalog_is_read_once_and_closed(self) -> None:
        for case in ("empty", "valid", "invalid"):
            with self.subTest(case=case):
                rows = (
                    []
                    if case == "empty"
                    else [self.product("4VB0000"), self.product("4QB1234")]
                )
                self.make_catalog(rows)
                if case == "invalid":
                    with self.path.open("a", encoding="utf-8") as file:
                        file.write("a,b\n")
                with (
                    self.path.open(encoding="utf-8-sig", newline="") as file,
                    patch("script.open", return_value=file) as open_catalog,
                    patch("script.csv.DictReader", wraps=csv.DictReader) as reader,
                ):
                    if case == "invalid":
                        with self.assertRaisesRegex(ValueError, "Запись №3"):
                            process_catalog(self.path, self.certificate)
                    else:
                        results = process_catalog(self.path, self.certificate)
                        self.assertEqual(
                            [row["status"] for row in results],
                            [] if case == "empty" else ["accepted", "declined"],
                        )
                    open_catalog.assert_called_once_with(
                        self.path, encoding="utf-8-sig", newline=""
                    )
                    reader.assert_called_once_with(file, strict=True)
                    self.assertTrue(file.closed)

    def test_catalog_rejects_invalid_encoding(self) -> None:
        for content in (
            b"\xffarticle,manufacturer,product_group\n",
            b"article,manufacturer,product_group\n4VB0000,HI-LEX CORPORATION,\xff\n",
        ):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(UnicodeDecodeError):
                    process_catalog(self.path, self.certificate)

    def test_catalog_column_order_and_extra_columns(self) -> None:
        for required in permutations(("article", "manufacturer", "product_group")):
            for extras in (False, True):
                with self.subTest(columns=required, extras=extras):
                    columns = required
                    if extras:
                        columns = (
                            ("description",)
                            + columns
                            + (
                                "status",
                                "search_key",
                                "reason",
                            )
                        )
                    rows = []
                    for article in (" 4vb0000 ", "4QB0000"):
                        values = {
                            "article": article,
                            "manufacturer": self.certificate["manufacturer"],
                            "product_group": self.certificate["product_group"],
                            "description": "ignored",
                            "status": "incoming",
                            "search_key": "incoming",
                            "reason": "incoming",
                        }
                        rows.append([values[column] for column in columns])
                    self.make_catalog(rows, columns)
                    results = process_catalog(self.path, self.certificate)
                    self.assertEqual(
                        [row["article"] for row in results], [" 4vb0000 ", "4QB0000"]
                    )
                    self.assertEqual(
                        [row["status"] for row in results], ["accepted", "declined"]
                    )
                    self.assertEqual(
                        [row["search_key"] for row in results], ["4VB0000", "4QB0000"]
                    )
                    self.assertEqual(
                        [row["reason"] for row in results],
                        ["", "Артикул не соответствует маскам"],
                    )
                    for row in results:
                        self.assertEqual(
                            row["manufacturer"], self.certificate["manufacturer"]
                        )
                        self.assertEqual(
                            row["product_group"], self.certificate["product_group"]
                        )
                        if extras:
                            self.assertEqual(row["description"], "ignored")

    def test_catalog_accepts_fields_at_size_limit(self) -> None:
        limit = 131_072
        columns = ("article", "manufacturer", "product_group", "description")
        for index, column in enumerate(columns):
            for size in (limit - 1, limit):
                with self.subTest(column=column, size=size):
                    row = self.product("4VB0000") + ["notes"]
                    row[index] = "Я" * size
                    self.make_catalog([row], columns)
                    csv.field_size_limit(64)
                    results = process_catalog(self.path, self.certificate)
                    self.assertEqual(len(results), 1)
                    self.assertEqual(results[0][column], row[index])
                    expected = "accepted" if column == "description" else "declined"
                    self.assertEqual(results[0]["status"], expected)

    def test_catalog_rejects_fields_over_size_limit(self) -> None:
        limit = 131_072
        columns = ("article", "manufacturer", "product_group", "description")
        for index, column in enumerate(columns):
            with self.subTest(column=column):
                row = self.product("4VB0000") + ["notes"]
                row[index] = "Я" * (limit + 1)
                self.make_catalog([row], columns)
                csv.field_size_limit(limit * 2)
                with self.assertRaises(csv.Error):
                    process_catalog(self.path, self.certificate)

    def test_catalog_restores_field_size_limit_on_success(self) -> None:
        for previous_limit in (64, 262_144):
            for empty in (False, True):
                with self.subTest(previous_limit=previous_limit, empty=empty):
                    self.make_catalog([] if empty else [self.product("4VB0000")])
                    csv.field_size_limit(previous_limit)
                    results = process_catalog(self.path, self.certificate)
                    self.assertEqual(len(results), 0 if empty else 1)
                    self.assertEqual(csv.field_size_limit(), previous_limit)
                    if previous_limit == 262_144:
                        value = "x" * 131_073
                        self.assertEqual(
                            list(csv.reader(io.StringIO(value))), [[value]]
                        )

    def test_catalog_restores_field_size_limit_on_error(self) -> None:
        header = b"article,manufacturer,product_group\n"
        cases = (
            ("missing", FileNotFoundError, None),
            ("empty", ValueError, b""),
            ("headers", ValueError, b"article\n"),
            ("duplicate", ValueError, b"article,manufacturer,product_group,article\n"),
            ("short", ValueError, header + b"a,b\n"),
            ("long", ValueError, header + b"a,b,c,d\n"),
            ("syntax", csv.Error, header + b'"unfinished'),
            ("encoding", UnicodeDecodeError, header + b"a,b,\xff\n"),
            ("size", csv.Error, header + b"x" * 131_073 + b",b,c\n"),
        )
        for previous_limit in (64, 262_144):
            for case, error, content in cases:
                with self.subTest(previous_limit=previous_limit, case=case):
                    path = self.path
                    if content is None:
                        path = self.path.with_name("missing.csv")
                    else:
                        path.write_bytes(content)
                    csv.field_size_limit(previous_limit)
                    with self.assertRaises(error):
                        process_catalog(path, self.certificate)
                    self.assertEqual(csv.field_size_limit(), previous_limit)

    def test_normalization(self) -> None:
        self.assertEqual(normalize_article("\u00a0 4vb0oi1 \u00a0"), "4VB0OI1")
        for article in (
            "4VB1 23",
            "4VB12-3",
            "4VB12.3",
            "4VB12А3",
            "4VBßıſ1",
            "\t4VB0000",
            "4VB0000\n",
            "4VB0000\r",
            "\u20034VB0000",
        ):
            with self.subTest(article=article):
                self.assertEqual(normalize_article(article), article)
        self.assertEqual(normalize_article("4VB1\u00a023"), "4VB1 23")
        self.assertEqual(
            len(
                {
                    normalize_article("4VB" + suffix)
                    for suffix in ("0000", "OOOO", "1111", "IIII")
                }
            ),
            4,
        )

    def test_article_matching(self) -> None:
        accepted = ["4VB0000", "4JBZZZZ", "4vba1z9", " 4JB0OI1 "]
        declined = [
            "4VB123",
            "4VB12345",
            "4VB12-3",
            "4QB1234",
            "4VB12А3",
            "4VB1 23",
            "4VB1\u00a023",
            "4VB12.3",
            "4VB0000\n",
            "\t4VB0000",
            "4VBı123",
            "4VB１２３４",
            "",
        ]
        self.make_catalog([self.product(article) for article in accepted + declined])
        results = process_catalog(self.path, self.certificate)
        self.assertEqual(
            [row["status"] for row in results],
            ["accepted"] * len(accepted) + ["declined"] * len(declined),
        )
        self.assertEqual([row["article"] for row in results], accepted + declined)
        self.assertEqual(results[3]["search_key"], "4JB0OI1")
        for row in results:
            self.assertEqual(bool(row["reason"]), row["status"] == "declined")

    def test_all_hi_lex_masks_accept_valid_articles(self) -> None:
        self.assertEqual(len(HI_LEX_TEST_MASKS), 38)
        self.assertEqual(len(set(HI_LEX_TEST_MASKS)), 38)
        cases = [
            (mask, mask[:3] + suffix)
            for mask in HI_LEX_TEST_MASKS
            for suffix in ("0000", "9999", "AAAA", "ZZZZ", "A1Z9", "0OI1")
        ]
        self.make_catalog([self.product(article) for _, article in cases])
        results = process_catalog(
            self.path, {**self.certificate, "masks": list(HI_LEX_TEST_MASKS)}
        )
        self.assertEqual(len(results), len(cases))
        for (mask, article), result in zip(cases, results, strict=True):
            with self.subTest(mask=mask, article=article):
                self.assertEqual(result["status"], "accepted")
                self.assertEqual(result["search_key"], article)
                self.assertEqual(result["reason"], "")

    def test_all_hi_lex_masks_reject_invalid_articles(self) -> None:
        for mask in HI_LEX_TEST_MASKS:
            prefix = mask[:3]
            articles = [
                prefix + "123",
                prefix + "12345",
                prefix + "12-3",
                prefix + "12.3",
                prefix + "1 23",
                prefix + "1\u00a023",
                prefix + "1\t23",
                prefix + "1\n23",
                prefix + "1234\n",
                prefix + "12А3",
                prefix + "ı123",
                prefix + "１２３４",
                prefix[:2] + "01234",
            ]
            self.make_catalog([self.product(article) for article in articles])
            results = process_catalog(self.path, {**self.certificate, "masks": [mask]})
            self.assertEqual(len(results), len(articles))
            for article, result in zip(articles, results, strict=True):
                with self.subTest(mask=mask, article=article):
                    self.assertEqual(result["article"], article)
                    self.assertEqual(result["status"], "declined")
                    self.assertEqual(
                        result["reason"], "Артикул не соответствует маскам"
                    )

    def test_metadata_and_multiple_reasons(self) -> None:
        rows = [
            ["4VB0000", "", self.certificate["product_group"]],
            ["4VB0000", "Другой производитель", self.certificate["product_group"]],
            ["4VB0000", "hi-lex corporation", self.certificate["product_group"]],
            ["4VB0000", self.certificate["manufacturer"], ""],
            ["4VB0000", self.certificate["manufacturer"], "другая группа"],
            ["4QB1234", "", ""],
        ]
        self.make_catalog(rows)
        results = process_catalog(self.path, self.certificate)
        self.assertTrue(all(row["status"] == "declined" for row in results))
        self.assertEqual(
            results[-1]["reason"],
            (
                "Не совпадает производитель; Не совпадает товарная группа; "
                "Артикул не соответствует маскам"
            ),
        )

    def test_masks_come_only_from_certificate(self) -> None:
        self.make_catalog([self.product("4VB0000"), self.product("4QB1234")])
        results = process_catalog(self.path, {**self.certificate, "masks": ["4QB****"]})
        self.assertEqual([row["status"] for row in results], ["declined", "accepted"])

    def test_duplicates_are_preserved(self) -> None:
        self.make_catalog(
            [
                ["4VB0000", "", ""],
                self.product("4VB0000"),
                self.product("4VB0000"),
            ]
        )
        results = process_catalog(self.path, self.certificate)
        self.assertEqual(
            [row["status"] for row in results], ["declined", "accepted", "accepted"]
        )

    def test_invalid_certificate_stops_before_catalog_reading(self) -> None:
        with patch("script.open") as open_catalog:
            with self.assertRaisesRegex(ValueError, "manufacturer"):
                process_catalog(self.path, {})
            open_catalog.assert_not_called()

    def test_empty_catalog(self) -> None:
        self.make_catalog([])
        self.assertEqual(process_catalog(self.path, self.certificate), [])

    def test_final_report_and_overwrite(self) -> None:
        self.make_catalog(
            [
                self.product(" 4vb0oi1 "),
                self.product("4QB1234"),
                self.product("4JB0000"),
            ]
        )
        results = process_catalog(self.path, self.certificate)
        report = self.path.with_name("report.csv")
        report.write_text("previous report", encoding="utf-8")
        write_results(results, report)
        with report.open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(
                list(csv.reader(file)),
                [
                    REPORT_TEST_COLUMNS,
                    self.product(" 4vb0oi1 ") + ["4VB0OI1"],
                    self.product("4JB0000") + ["4JB0000"],
                ],
            )
        for rows in ([], [results[1]]):
            write_results(rows, report)
            with report.open(encoding="utf-8-sig", newline="") as file:
                self.assertEqual(list(csv.reader(file)), [REPORT_TEST_COLUMNS])

    def test_final_report_preserves_raw_fields_without_reason_or_extra(self) -> None:
        self.certificate["manufacturer"] = "=1+1"
        self.certificate["product_group"] = "+SUM(1,2)"
        article = "\u00a0 4vb0000 \u00a0"
        self.make_catalog(
            [self.product(article) + ["not exported"]],
            ("article", "manufacturer", "product_group", "extra"),
        )
        results = process_catalog(self.path, self.certificate)
        report = self.path.with_name("report.csv")
        write_results(results, report)
        with report.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            self.assertEqual(reader.fieldnames, REPORT_TEST_COLUMNS)
            self.assertEqual(
                list(reader),
                [
                    {
                        "article": article,
                        "manufacturer": "=1+1",
                        "product_group": "+SUM(1,2)",
                        "search_key": "4VB0000",
                    }
                ],
            )

    def test_final_report_removes_exact_duplicates(self) -> None:
        self.make_catalog(
            [
                self.product("4VB0000"),
                self.product("4VB0000"),
                self.product("4VB0000"),
            ]
        )
        results = process_catalog(self.path, self.certificate)
        report = self.path.with_name("report.csv")
        write_results(results, report)
        with report.open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(
                list(csv.reader(file)),
                [REPORT_TEST_COLUMNS, self.product("4VB0000") + ["4VB0000"]],
            )

    def test_final_report_deduplicates_keys_in_first_accepted_order(self) -> None:
        self.make_catalog(
            [
                self.product(" 4jb0000 "),
                self.product("4VB0000"),
                self.product("4JB0000"),
                self.product("\u00a04vb0000\u00a0"),
                self.product("4JBA1Z9"),
                self.product("4jba1z9"),
            ]
        )
        results = process_catalog(self.path, self.certificate)
        original_results = [row.copy() for row in results]
        report = self.path.with_name("report.csv")
        write_results(results, report)
        with report.open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(
                list(csv.reader(file)),
                [
                    REPORT_TEST_COLUMNS,
                    self.product(" 4jb0000 ") + ["4JB0000"],
                    self.product("4VB0000") + ["4VB0000"],
                    self.product("4JBA1Z9") + ["4JBA1Z9"],
                ],
            )
        self.assertEqual(results, original_results)

        self.assertEqual(len(results), 6)
        self.assertTrue(all(row["status"] == "accepted" for row in results))

    def test_declined_duplicate_does_not_reserve_key_or_order(self) -> None:
        self.make_catalog(
            [
                ["4VB0000", "Другой производитель", self.certificate["product_group"]],
                ["4VB0000", self.certificate["manufacturer"], "другая группа"],
                self.product("4JB0000"),
                self.product(" 4vb0000 "),
                self.product("4VB0000"),
            ]
        )
        results = process_catalog(self.path, self.certificate)
        report = self.path.with_name("report.csv")
        write_results(results, report)
        with report.open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(
                list(csv.reader(file)),
                [
                    REPORT_TEST_COLUMNS,
                    self.product("4JB0000") + ["4JB0000"],
                    self.product(" 4vb0000 ") + ["4VB0000"],
                ],
            )
        self.assertEqual(
            [row["status"] for row in results],
            ["declined", "declined", "accepted", "accepted", "accepted"],
        )

    def test_exceptions_preserve_raw_values_order_and_duplicates(self) -> None:
        raw = ['=HYPERLINK("a,b","c")\n', "=1+1", "+SUM(1,2)"]
        self.make_catalog(
            [
                self.product("4VB0000") + ["accepted"],
                raw + ["first"],
                self.product("4QB1234") + ["middle"],
                raw + ["last"],
            ],
            ("article", "manufacturer", "product_group", "extra"),
        )
        results = process_catalog(self.path, self.certificate)
        original_results = [row.copy() for row in results]
        report = self.path.with_name("exceptions.csv")
        write_exceptions(results, report)
        self.assertTrue(report.read_bytes().startswith(b"\xef\xbb\xbf"))
        with report.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            self.assertEqual(
                reader.fieldnames,
                ["article", "manufacturer", "product_group", "search_key", "reason"],
            )
            rows = list(reader)
        expected = {
            "article": raw[0],
            "manufacturer": raw[1],
            "product_group": raw[2],
            "search_key": '=HYPERLINK("A,B","C")\n',
            "reason": (
                "Не совпадает производитель; Не совпадает товарная группа; "
                "Артикул не соответствует маскам"
            ),
        }
        self.assertEqual(
            rows,
            [
                expected,
                {
                    "article": "4QB1234",
                    "manufacturer": self.certificate["manufacturer"],
                    "product_group": self.certificate["product_group"],
                    "search_key": "4QB1234",
                    "reason": "Артикул не соответствует маскам",
                },
                expected,
            ],
        )
        self.assertEqual(results, original_results)

    def test_exceptions_overwrite_with_header_when_empty(self) -> None:
        self.make_catalog([self.product("4VB0000")])
        accepted = process_catalog(self.path, self.certificate)
        report = self.path.with_name("exceptions.csv")
        for results in ([], accepted):
            with self.subTest(results=results):
                report.write_bytes(b"previous exceptions")
                write_exceptions(results, report)
                with report.open(encoding="utf-8-sig", newline="") as file:
                    self.assertEqual(
                        list(csv.reader(file)),
                        [
                            [
                                "article",
                                "manufacturer",
                                "product_group",
                                "search_key",
                                "reason",
                            ]
                        ],
                    )

    def prepare_run(self) -> None:
        self.make_catalog([self.product(" 4vb0000 "), self.product("4QB1234")])
        self.path.with_name("certificate.json").write_text(
            json.dumps(self.certificate), encoding="utf-8"
        )

    def run_main(self) -> tuple[int, str, str]:
        with (
            patch("script.__file__", str(self.path.with_name("script.py"))),
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = script.main()
        return code, output.getvalue(), errors.getvalue()

    def test_reports_survive_write_failure(self) -> None:
        self.make_catalog([self.product("4VB0000"), self.product("4QB1234")])
        results = process_catalog(self.path, self.certificate)
        for write_report in (write_results, write_exceptions):
            for existing in (True, False):
                with self.subTest(writer=write_report.__name__, existing=existing):
                    report = self.path.with_name(
                        f"{write_report.__name__}.{existing}.csv"
                    )
                    if existing:
                        report.write_bytes(b"previous report")
                    before = set(self.path.parent.iterdir())
                    with patch(
                        "script.csv.writer", side_effect=OSError("write failed")
                    ):
                        with self.assertRaises(OSError):
                            write_report(results, report)
                    if existing:
                        self.assertEqual(report.read_bytes(), b"previous report")
                    else:
                        self.assertFalse(report.exists())
                    self.assertEqual(set(self.path.parent.iterdir()), before)

    def test_partial_temporary_report_is_not_published(self) -> None:
        report = self.path.with_name("report.csv")
        report.write_bytes(b"previous report")
        before = set(self.path.parent.iterdir())
        with self.assertRaises(OSError):
            with script.atomic_report_file(report) as file:
                file.write("partially written report")
                self.assertEqual(report.read_bytes(), b"previous report")
                raise OSError("disk full")
        self.assertEqual(report.read_bytes(), b"previous report")
        self.assertEqual(set(self.path.parent.iterdir()), before)

    def test_reports_survive_replace_failure(self) -> None:
        self.make_catalog([self.product("4VB0000"), self.product("4QB1234")])
        results = process_catalog(self.path, self.certificate)
        for write_report in (write_results, write_exceptions):
            with self.subTest(writer=write_report.__name__):
                report = self.path.with_name(f"{write_report.__name__}.csv")
                report.write_bytes(b"previous report")
                before = set(self.path.parent.iterdir())
                with patch(
                    "report_io.Path.replace", side_effect=PermissionError("locked")
                ):
                    with self.assertRaises(PermissionError):
                        write_report(results, report)
                self.assertEqual(report.read_bytes(), b"previous report")
                self.assertEqual(set(self.path.parent.iterdir()), before)

    def test_main_writes_both_reports(self) -> None:
        self.prepare_run()
        code, output, errors = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertNotIn("accepted", output)
        self.assertNotIn("declined", output)
        self.assertIn("Результат сохранён", output)
        self.assertIn("Список исключений сохранён", output)
        self.assertEqual(
            {path.name for path in self.path.parent.iterdir()},
            {"catalog.csv", "certificate.json", "report.csv", "exceptions.csv"},
        )
        with self.path.with_name("report.csv").open(
            encoding="utf-8-sig", newline=""
        ) as file:
            self.assertEqual(
                list(csv.reader(file)),
                [REPORT_TEST_COLUMNS, self.product(" 4vb0000 ") + ["4VB0000"]],
            )
        with self.path.with_name("exceptions.csv").open(
            encoding="utf-8-sig", newline=""
        ) as file:
            self.assertEqual(
                list(csv.DictReader(file)),
                [
                    {
                        "article": "4QB1234",
                        "manufacturer": self.certificate["manufacturer"],
                        "product_group": self.certificate["product_group"],
                        "search_key": "4QB1234",
                        "reason": "Артикул не соответствует маскам",
                    }
                ],
            )

    def test_main_handles_empty_and_single_status_catalogs(self) -> None:
        cases = (
            ("empty", [], [], []),
            ("accepted", [self.product("4VB0000")], ["4VB0000"], []),
            ("declined", [self.product("4QB1234")], [], ["4QB1234"]),
        )
        for name, rows, accepted, declined in cases:
            with self.subTest(case=name):
                self.prepare_run()
                self.make_catalog(rows)
                code, _, errors = self.run_main()
                self.assertEqual(code, 0)
                self.assertEqual(errors, "")
                with self.path.with_name("report.csv").open(
                    encoding="utf-8-sig", newline=""
                ) as file:
                    self.assertEqual(
                        list(csv.reader(file)),
                        [REPORT_TEST_COLUMNS]
                        + [self.product(key) + [key] for key in accepted],
                    )
                with self.path.with_name("exceptions.csv").open(
                    encoding="utf-8-sig", newline=""
                ) as file:
                    reader = csv.DictReader(file)
                    self.assertEqual(len(reader.fieldnames), 5)
                    self.assertEqual([row["article"] for row in reader], declined)

    def test_main_preserves_report_on_invalid_input(self) -> None:
        for case in (
            "json",
            "schema",
            "mask",
            "encoding",
            "catalog_encoding",
            "csv",
            "columns",
            "missing",
        ):
            with self.subTest(case=case):
                self.prepare_run()
                certificate_path = self.path.with_name("certificate.json")
                if case == "json":
                    certificate_path.write_text("{", encoding="utf-8")
                elif case == "schema":
                    certificate_path.write_text("{}", encoding="utf-8")
                elif case == "mask":
                    certificate_path.write_text(
                        json.dumps(
                            {
                                **self.certificate,
                                "masks": ["unsupported"],
                            }
                        ),
                        encoding="utf-8",
                    )
                elif case == "encoding":
                    certificate_path.write_bytes(b"\xff")
                elif case == "catalog_encoding":
                    self.path.write_bytes(
                        b"article,manufacturer,product_group\n4VB0000,HI-LEX CORPORATION,\xff\n"
                    )
                elif case == "csv":
                    with self.path.open("a", encoding="utf-8") as file:
                        file.write('"unfinished')
                elif case == "columns":
                    self.path.write_text("article\n", encoding="utf-8")
                elif case == "missing":
                    certificate_path.rename(self.path.with_name("missing.json"))
                report = self.path.with_name("report.csv")
                report.write_bytes(b"previous report")
                exceptions = self.path.with_name("exceptions.csv")
                exceptions.write_bytes(b"previous exceptions")
                code, output, errors = self.run_main()
                self.assertEqual(code, 1)
                if case in ("json", "encoding", "missing"):
                    self.assertTrue(errors.startswith("Ошибка чтения сертификата:"))
                else:
                    self.assertTrue(
                        errors.startswith(
                            "Ошибка проверки входных данных или обработки каталога:"
                        )
                    )
                self.assertNotIn("Traceback", errors)
                self.assertEqual(output, "")
                self.assertEqual(report.read_bytes(), b"previous report")
                self.assertEqual(exceptions.read_bytes(), b"previous exceptions")

    def test_main_preserves_reports_on_late_catalog_error(self) -> None:
        for tail in (
            b"a,b\n",
            b"a,b,c,d\n",
            b'"unfinished',
            ("Я" * 131_073).encode("utf-8"),
            b"\xff",
        ):
            with self.subTest(tail=tail[:20]):
                self.prepare_run()
                # Valid data exceeds the decoding buffer before the invalid tail.
                self.make_catalog(
                    [self.product("4VB0000"), self.product("4QB1234")] * 100
                )
                with self.path.open("ab") as file:
                    file.write(tail)
                report = self.path.with_name("report.csv")
                report.write_bytes(b"previous report")
                exceptions = self.path.with_name("exceptions.csv")
                exceptions.write_bytes(b"previous exceptions")
                before = set(self.path.parent.iterdir())
                with (
                    patch(
                        "script.normalize_article", wraps=normalize_article
                    ) as normalize,
                    patch("script.write_results") as write,
                    patch("script.write_exceptions") as write_rejected,
                ):
                    code, output, errors = self.run_main()
                self.assertGreaterEqual(normalize.call_count, 2)
                write.assert_not_called()
                write_rejected.assert_not_called()
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertTrue(
                    errors.startswith(
                        "Ошибка проверки входных данных или обработки каталога:"
                    )
                )
                self.assertNotIn("Traceback", errors)
                self.assertEqual(report.read_bytes(), b"previous report")
                self.assertEqual(exceptions.read_bytes(), b"previous exceptions")
                self.assertEqual(set(self.path.parent.iterdir()), before)

    def test_main_reports_write_error(self) -> None:
        self.prepare_run()
        report = self.path.with_name("report.csv")
        exceptions = self.path.with_name("exceptions.csv")
        report.write_bytes(b"previous report")
        exceptions.write_bytes(b"previous exceptions")
        with (
            patch("script.write_results", side_effect=PermissionError("locked")),
            patch("script.write_exceptions") as write,
        ):
            code, output, errors = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertTrue(errors.startswith("Ошибка записи отчёта:"))
        self.assertIn("locked", errors)
        self.assertNotIn("Traceback", errors)
        write.assert_not_called()
        self.assertEqual(report.read_bytes(), b"previous report")
        self.assertEqual(exceptions.read_bytes(), b"previous exceptions")

    def test_main_reports_partial_success_on_exceptions_replace_failure(self) -> None:
        self.prepare_run()
        report = self.path.with_name("report.csv")
        exceptions = self.path.with_name("exceptions.csv")
        report.write_bytes(b"previous report")
        exceptions.write_bytes(b"previous exceptions")
        before = set(self.path.parent.iterdir())
        original_replace = Path.replace

        def replace_report(source: Path, target: str | Path) -> Path:
            if Path(target) == exceptions:
                raise PermissionError("exceptions locked")
            return original_replace(source, target)

        with patch("report_io.Path.replace", autospec=True, side_effect=replace_report):
            code, output, errors = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertTrue(errors.startswith("Ошибка записи списка исключений:"))
        self.assertIn("Итоговый отчёт уже сохранён", errors)
        self.assertIn(str(report), errors)
        self.assertNotIn("Traceback", errors)
        with report.open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(
                list(csv.reader(file)),
                [REPORT_TEST_COLUMNS, self.product(" 4vb0000 ") + ["4VB0000"]],
            )
        self.assertEqual(exceptions.read_bytes(), b"previous exceptions")
        self.assertEqual(set(self.path.parent.iterdir()), before)

    def test_main_preserves_report_on_duplicate_json_key(self) -> None:
        for key in ("manufacturer", "masks"):
            with self.subTest(key=key):
                self.prepare_run()
                original = json.dumps(self.certificate)
                duplicate = original[:-1] + (
                    f", {json.dumps(key)}: {json.dumps(self.certificate[key])}" + "}"
                )
                self.path.with_name("certificate.json").write_text(
                    duplicate, encoding="utf-8"
                )
                report = self.path.with_name("report.csv")
                report.write_bytes(b"previous report")
                exceptions = self.path.with_name("exceptions.csv")
                exceptions.write_bytes(b"previous exceptions")
                before = set(self.path.parent.iterdir())
                with (
                    patch("script.process_catalog") as process,
                    patch("script.write_results") as write,
                    patch("script.write_exceptions") as write_rejected,
                ):
                    code, output, errors = self.run_main()
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertTrue(errors.startswith("Ошибка чтения сертификата:"))
                self.assertIn(key, errors)
                self.assertNotIn("Traceback", errors)
                process.assert_not_called()
                write.assert_not_called()
                write_rejected.assert_not_called()
                self.assertEqual(report.read_bytes(), b"previous report")
                self.assertEqual(exceptions.read_bytes(), b"previous exceptions")
                self.assertEqual(set(self.path.parent.iterdir()), before)

    def test_main_preserves_report_on_oversized_field(self) -> None:
        self.prepare_run()
        self.make_catalog(
            [self.product("4VB0000") + ["Я" * 131_073]],
            ("article", "manufacturer", "product_group", "description"),
        )
        report = self.path.with_name("report.csv")
        report.write_bytes(b"previous report")
        exceptions = self.path.with_name("exceptions.csv")
        exceptions.write_bytes(b"previous exceptions")
        before = set(self.path.parent.iterdir())
        csv.field_size_limit(262_144)
        code, output, errors = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertTrue(
            errors.startswith("Ошибка проверки входных данных или обработки каталога:")
        )
        self.assertIn("131072", errors)
        self.assertNotIn("Traceback", errors)
        self.assertEqual(report.read_bytes(), b"previous report")
        self.assertEqual(exceptions.read_bytes(), b"previous exceptions")
        self.assertEqual(set(self.path.parent.iterdir()), before)

    def test_script_exit_codes_and_repeated_run(self) -> None:
        self.certificate["masks"] = list(HI_LEX_TEST_MASKS)
        self.prepare_run()
        self.make_catalog(
            [
                ["4VB0000", "Другой производитель", self.certificate["product_group"]],
                self.product(" 9bb0oi1 "),
                self.product("4vba1z9"),
                self.product("9BB0OI1"),
                self.product("\u00a04vbA1Z9\u00a0"),
                self.product("7HBZZZZ"),
                ["4JB1234", self.certificate["manufacturer"], "другая группа"],
                self.product("4VB12А3"),
                self.product("4QB1234"),
                self.product("4VB0000"),
                self.product(""),
            ]
        )
        inputs = {
            name: self.path.with_name(name).read_bytes()
            for name in ("certificate.json", "catalog.csv")
        }
        source = Path(script.__file__).resolve().parent
        for name in ("script.py", "report_io.py"):
            self.path.with_name(name).write_bytes((source / name).read_bytes())
        previous_reports = None
        for _ in range(2):
            run = subprocess.run(
                [sys.executable, "-B", "-m", "script"],
                cwd=self.path.parent,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stderr, "")
            with self.path.with_name("report.csv").open(
                encoding="utf-8-sig", newline=""
            ) as file:
                self.assertEqual(
                    list(csv.reader(file)),
                    [
                        REPORT_TEST_COLUMNS,
                        self.product(" 9bb0oi1 ") + ["9BB0OI1"],
                        self.product("4vba1z9") + ["4VBA1Z9"],
                        self.product("7HBZZZZ") + ["7HBZZZZ"],
                        self.product("4VB0000") + ["4VB0000"],
                    ],
                )
            with self.path.with_name("exceptions.csv").open(
                encoding="utf-8-sig", newline=""
            ) as file:
                self.assertEqual(
                    [row["article"] for row in csv.DictReader(file)],
                    ["4VB0000", "4JB1234", "4VB12А3", "4QB1234", ""],
                )
            reports = {
                name: self.path.with_name(name).read_bytes()
                for name in ("report.csv", "exceptions.csv")
            }
            for content in reports.values():
                self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
            if previous_reports is not None:
                self.assertEqual(reports, previous_reports)
            previous_reports = reports
            for name, original in inputs.items():
                self.assertEqual(self.path.with_name(name).read_bytes(), original)
        self.path.with_name("certificate.json").write_text("{", encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-B", "-m", "script"],
            cwd=self.path.parent,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(run.returncode, 1)
        self.assertIn("Ошибка", run.stderr)
        self.assertNotIn("Traceback", run.stderr)
        for name, previous in reports.items():
            self.assertEqual(self.path.with_name(name).read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
