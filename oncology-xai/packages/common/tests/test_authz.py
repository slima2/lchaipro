"""Unit tests para oncology_common.authz (F6.5)."""

from __future__ import annotations

import pytest

from oncology_common.authz import (
    InvalidArtifactKey,
    case_id_from_key,
    caller_from_request,
)


class _FakeHeaders:
    def __init__(self, data: dict) -> None:
        self._d = {k.lower(): v for k, v in data.items()}

    def get(self, key: str, default: str = "") -> str:
        return self._d.get(key.lower(), default)


class _FakeRequest:
    def __init__(self, headers: dict) -> None:
        self.headers = _FakeHeaders(headers)


# ── caller_from_request ────────────────────────────────────────────────


def test_caller_parses_user_and_multiple_roles() -> None:
    req = _FakeRequest({"x-user-id": "u1", "x-roles": "clinician, admin , auditor"})
    caller = caller_from_request(req)  # type: ignore[arg-type]
    assert caller.user_id == "u1"
    assert caller.roles == frozenset({"clinician", "admin", "auditor"})
    assert caller.is_admin and caller.is_auditor and caller.is_supervisor


def test_caller_without_headers_defaults_safely() -> None:
    caller = caller_from_request(_FakeRequest({}))  # type: ignore[arg-type]
    assert caller.user_id == "unknown"
    assert caller.roles == frozenset()
    assert not caller.is_admin and not caller.is_supervisor


# ── case_id_from_key ───────────────────────────────────────────────────


def test_key_images_prefix_extracts_case_id() -> None:
    assert case_id_from_key("images/case-A/slide.png") == "case-A"


def test_key_results_prefix_extracts_case_id() -> None:
    assert case_id_from_key("results/case-B/rb-1/shap.png") == "case-B"


def test_key_artifacts_prefix_extracts_case_id() -> None:
    assert case_id_from_key("artifacts/case-C/thumb.png") == "case-C"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/images/c/x.png",  # absolute
        "images/../other/x.png",  # traversal
        "images/c\\x.png",  # backslash
        "images/c/",  # no file segment
        "images/c",  # no slash after case_id
        "logs/c/x.png",  # prefix not in allow-list
        "../images/c/x.png",
        "images//x.png",  # empty case_id
    ],
)
def test_malformed_keys_rejected(bad: str) -> None:
    with pytest.raises(InvalidArtifactKey):
        case_id_from_key(bad)
