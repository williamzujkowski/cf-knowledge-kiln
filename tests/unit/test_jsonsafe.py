"""Unit tests for the frontmatter JSON-safe coercion helper (#91)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from uuid import UUID

from cf_knowledge_kiln.ingestion._jsonsafe import jsonify


class TestJsonify:
    def test_passthrough_for_json_native_scalars(self) -> None:
        for v in (None, True, False, 0, 1, -1, 1.5, ""):
            assert jsonify(v) == v

    def test_date_becomes_iso_string(self) -> None:
        out = jsonify(date(2026, 5, 16))
        assert out == "2026-05-16"

    def test_datetime_becomes_iso_string(self) -> None:
        out = jsonify(datetime(2026, 5, 16, 12, 30, 45, tzinfo=UTC))
        assert out.startswith("2026-05-16T12:30:45")

    def test_decimal_becomes_float(self) -> None:
        out = jsonify(Decimal("1.5"))
        assert out == 1.5
        assert isinstance(out, float)

    def test_uuid_becomes_string(self) -> None:
        u = UUID("12345678-1234-5678-1234-567812345678")
        assert jsonify(u) == str(u)

    def test_set_becomes_sorted_list(self) -> None:
        out = jsonify({3, 1, 2})
        assert out == [1, 2, 3]

    def test_frozenset_becomes_sorted_list(self) -> None:
        out = jsonify(frozenset(["b", "a"]))
        assert out == ["a", "b"]

    def test_bytes_utf8_decode(self) -> None:
        assert jsonify(b"hello") == "hello"

    def test_bytes_invalid_utf8_hex_fallback(self) -> None:
        assert jsonify(b"\xff\xfe") == "fffe"

    def test_nested_dict_recursed(self) -> None:
        out = jsonify({"created": date(2026, 5, 16), "tags": {"a", "b"}})
        assert out == {"created": "2026-05-16", "tags": ["a", "b"]}

    def test_nested_list_recursed(self) -> None:
        out = jsonify([date(2026, 5, 16), {"d": date(2026, 1, 1)}])
        assert out == ["2026-05-16", {"d": "2026-01-01"}]

    def test_tuple_becomes_list(self) -> None:
        assert jsonify((1, 2, 3)) == [1, 2, 3]

    def test_dict_string_keys_coerced(self) -> None:
        out = jsonify({1: "a", "b": "c"})
        assert out == {"1": "a", "b": "c"}

    def test_path_becomes_posix_string(self) -> None:
        assert jsonify(PurePosixPath("a/b/c.md")) == "a/b/c.md"

    def test_cycle_in_dict_does_not_recurse_infinitely(self) -> None:
        """A self-referential dict short-circuits to "<cycle>"."""
        d: dict[str, object] = {"name": "outer"}
        d["self"] = d
        out = jsonify(d)
        assert out["name"] == "outer"
        assert out["self"] == "<cycle>"

    def test_cycle_in_list_does_not_recurse_infinitely(self) -> None:
        a: list[object] = [1, 2]
        a.append(a)
        out = jsonify(a)
        assert out[:2] == [1, 2]
        assert out[2] == "<cycle>"

    def test_output_round_trips_through_json(self) -> None:
        """The whole point: output must hand off to json.dumps cleanly."""
        meta = {
            "id": "ADR-0001",
            "date": date(2026, 5, 16),
            "owner": UUID("12345678-1234-5678-1234-567812345678"),
            "tags": {"db", "phase-2"},
            "decimal_price": Decimal("9.99"),
            "nested": {"created_at": datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)},
        }
        encoded = json.dumps(jsonify(meta))
        decoded = json.loads(encoded)
        assert decoded["date"] == "2026-05-16"
        assert decoded["owner"] == "12345678-1234-5678-1234-567812345678"
        assert sorted(decoded["tags"]) == ["db", "phase-2"]
        assert decoded["decimal_price"] == 9.99
        assert decoded["nested"]["created_at"].startswith("2026-05-16T00:00:00")
