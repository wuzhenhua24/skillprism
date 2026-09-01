from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def upstream_report() -> dict:
    """一份真实的 SkillEvaluator JSON 报告，用于锁定 adapter 的解析契约。"""
    return json.loads((FIXTURES / "upstream_report_passed.json").read_text(encoding="utf-8"))
