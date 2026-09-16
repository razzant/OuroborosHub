"""
Core analysis pipeline: OCR text → structured lab results → interpretation.

Uses jinja2 prompts adapted from Maestro SH and calls the host LLM
through the Ouroboros Host Service API.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, List, Optional

from jinja2 import Environment, FileSystemLoader

from .models import (
    LabAnalysis,
    LabTestValue,
    PatientInfo,
    Reference,
    TestNamesResponse,
)

# ── Prompt loading ────────────────────────────────────────

_SKILL_DIR = Path(__file__).resolve().parent
_PROMPTS_DIR = _SKILL_DIR / "prompts"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_PROMPTS_DIR)),
    keep_trailing_newline=True,
)


def _render_prompt(template_name: str, **kwargs: Any) -> str:
    tpl = _jinja_env.get_template(template_name)
    return tpl.render(**kwargs)


# ── JSON extraction helper ────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Extract JSON from LLM response (possibly wrapped in ```json blocks)."""
    m = _JSON_BLOCK_RE.search(text)
    raw = m.group(1) if m else text
    # Strip leading/trailing whitespace and try to parse
    raw = raw.strip()
    return json.loads(raw)


# ── LLM call wrapper (set externally by plugin.py) ───────

_llm_call = None  # type: ignore


def set_llm_caller(fn):
    """Set the LLM call function. Called by plugin.py during registration."""
    global _llm_call
    _llm_call = fn


def _ask_llm(prompt: str) -> str:
    """Call the host LLM. Raises RuntimeError if not configured."""
    if _llm_call is None:
        raise RuntimeError("LLM caller not configured. Call set_llm_caller first.")
    return _llm_call(prompt)


# ── Pipeline steps ────────────────────────────────────────


def extract_report_info(raw_text: str) -> tuple[str, PatientInfo, List[str]]:
    """Classify text and extract patient info and test names in one LLM call."""
    prompt = _render_prompt("report_info.jinja2", raw_text=raw_text)
    resp = _ask_llm(prompt)
    try:
        data = _extract_json(resp)
        if not isinstance(data, dict):
            return "other", PatientInfo(), []
        result = int(data.get("result", 3))
    except (json.JSONDecodeError, ValueError, TypeError):
        return "other", PatientInfo(), []

    text_type = {1: "lab", 2: "instrumental", 3: "other"}.get(result, "other")
    if text_type != "lab":
        return text_type, PatientInfo(), []

    # Validate independently so malformed demographics do not discard tests.
    try:
        patient = PatientInfo.model_validate(data)
    except ValueError:
        patient = PatientInfo()
    try:
        test_names = TestNamesResponse.model_validate(data).tests
    except ValueError:
        test_names = []
    return text_type, patient, test_names


def extract_test_values(raw_text: str, test_name: str) -> Optional[LabTestValue]:
    """Extract structured values for a single test from OCR text."""
    prompt = _render_prompt(
        "lab_tests_values_extraction.jinja2",
        raw_text=raw_text,
        test_name=test_name,
    )
    resp = _ask_llm(prompt)
    try:
        data = _extract_json(resp)
        return _parse_test_value(data, test_name)
    except Exception:
        return None


def extract_all_test_values(
    raw_text: str, test_names: List[str]
) -> List[LabTestValue]:
    """Extract structured values for ALL tests in a single LLM call (batch mode).

    This replaces the per-test loop and reduces N LLM calls to 1.
    Falls back to per-test extraction if batch parsing fails.
    """
    if not test_names:
        return []
    prompt = _render_prompt(
        "lab_tests_values_batch.jinja2",
        raw_text=raw_text,
        test_names=test_names,
    )
    resp = _ask_llm(prompt)
    try:
        data = _extract_json(resp)
        if not isinstance(data, list):
            # Single-object response — wrap it
            if isinstance(data, dict):
                data = [data]
            else:
                raise ValueError("Expected JSON array")
        results = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            name = test_names[i] if i < len(test_names) else item.get("name", "")
            test = _parse_test_value(item, name)
            if test:
                results.append(test)
        # If we got fewer results than expected, try filling gaps
        if len(results) < len(test_names):
            found_names = {t.name for t in results}
            for name in test_names:
                if name not in found_names:
                    results.append(LabTestValue(name=name))
        return results
    except Exception:
        # Fallback: per-test extraction (slower but resilient)
        results = []
        for name in test_names:
            test = extract_test_values(raw_text, name)
            if test:
                results.append(test)
        return results


def _parse_test_value(data: dict, fallback_name: str) -> Optional[LabTestValue]:
    """Parse a single test value dict from LLM response."""
    ref_data = data.get("reference")
    reference = None
    if ref_data and isinstance(ref_data, dict):
        reference = Reference(
            description=ref_data.get("description", ""),
            low_value=ref_data.get("low_value"),
            high_value=ref_data.get("high_value"),
        )
    test = LabTestValue(
        name=data.get("name", fallback_name),
        value=data.get("value"),
        unit=data.get("unit"),
        biomaterial=data.get("biomaterial"),
        reference=reference,
        comment=data.get("comment"),
        status=data.get("status"),
    )
    return _validate_status_by_reference(test)


def _validate_status_by_reference(test: LabTestValue) -> LabTestValue:
    """Re-check status against numeric reference values."""
    ref = test.reference
    if ref is None:
        return test
    low = ref.low_value
    high = ref.high_value
    if low is None and high is None:
        return test
    try:
        value = float(str(test.value).replace(",", "."))
    except (TypeError, ValueError):
        return test

    if low is not None and value < low:
        test.status = "снижен"
    elif high is not None and value > high:
        test.status = "повышен"
    else:
        test.status = "норма"
    return test


def interpret_test(
    test: LabTestValue,
    sex_age_str: str,
    exam_name: str = "Лабораторный анализ",
) -> str:
    """Generate interpretation text for a single lab test."""
    # Determine bucket type
    status = (test.status or "").lower()
    if status in ("повышен", "снижен", "отклонение"):
        bucket_type = "abnormal"
    elif status == "норма":
        bucket_type = "normal"
    elif test.reference and test.reference.description:
        bucket_type = "lab_comment"
    else:
        bucket_type = "no_reference"

    test_json = json.dumps(test.model_dump(), ensure_ascii=False, indent=2)
    prompt = _render_prompt(
        "lab_test_single_interpretation.jinja2",
        test_json=test_json,
        sex_age_str=sex_age_str,
        exam_name=exam_name,
        bucket_type=bucket_type,
    )
    return _ask_llm(prompt).strip()


def interpret_tests_batch(
    tests: List[LabTestValue],
    sex_age_str: str,
    exam_name: str = "Лабораторный анализ",
) -> List[str]:
    """Interpret multiple lab tests in a single LLM call (batch mode).

    Groups ALL tests (not just abnormal) into one call, reducing N
    interpretation calls to 1.
    """
    if not tests:
        return []

    test_items = []
    has_abnormal = has_normal = has_lab_comment = has_no_ref = False
    for test in tests:
        status = (test.status or "").lower()
        if status in ("повышен", "снижен", "отклонение"):
            bucket_type = "abnormal"
            has_abnormal = True
        elif status == "норма":
            bucket_type = "normal"
            has_normal = True
        elif test.reference and test.reference.description:
            bucket_type = "lab_comment"
            has_lab_comment = True
        else:
            bucket_type = "no_reference"
            has_no_ref = True

        test_items.append({
            "test_json": json.dumps(
                test.model_dump(), ensure_ascii=False, indent=2
            ),
            "bucket_type": bucket_type,
        })

    prompt = _render_prompt(
        "lab_tests_batch_interpretation.jinja2",
        test_items=test_items,
        sex_age_str=sex_age_str,
        exam_name=exam_name,
        has_abnormal=has_abnormal,
        has_normal=has_normal,
        has_lab_comment=has_lab_comment,
        has_no_ref=has_no_ref,
    )
    resp = _ask_llm(prompt).strip()

    # Split response into individual lines (one per test)
    lines = [ln.strip() for ln in resp.split("\n") if ln.strip()]
    if len(lines) == len(tests):
        return lines

    # Fallback: if line count mismatch, interpret individually
    results = []
    for test in tests:
        results.append(interpret_test(test, sex_age_str, exam_name))
    return results


# ── Full pipeline ─────────────────────────────────────────


def analyze(raw_text: str, max_tests: int = 40) -> LabAnalysis:
    """
    Run the full analysis pipeline on OCR text.

    Steps:
    1. Classify text and extract patient info and test names in one call
    2. Extract all test values in one batch call
    3. Interpret all tests in one batch call
    4. Build summary locally
    """
    result = LabAnalysis(raw_text=raw_text)

    # Step 1: Extract report info in a single call
    text_type, patient, test_names = extract_report_info(raw_text)
    result.text_type = text_type
    if text_type != "lab":
        result.interpretation = (
            "Текст не является лабораторным исследованием. "
            f"Определённый тип: {text_type}."
        )
        return result

    result.patient = patient

    sex_str = "мужчина" if patient.sex is True else (
        "женщина" if patient.sex is False else "не указан"
    )
    age_str = f"{patient.age} лет" if patient.age else "не указан"
    sex_age_str = f"Пол: {sex_str}, возраст: {age_str}"

    # Cap test names to max_tests
    if not test_names:
        result.interpretation = "Не удалось извлечь названия тестов из текста."
        return result

    capped = len(test_names) > max_tests
    if capped:
        test_names = test_names[:max_tests]

    # Step 2: Extract values for all tests in a single batch call
    tests = extract_all_test_values(raw_text, test_names)
    result.tests = tests

    if not tests:
        result.interpretation = "Не удалось извлечь значения тестов."
        return result

    # Step 3: Interpret all tests in a single batch call
    interpretations = interpret_tests_batch(tests, sex_age_str)

    # Step 4: Build summary
    summary_parts = []
    normal_count = sum(
        1 for t in tests if (t.status or "").lower() == "норма"
    )
    abnormal_tests = [
        t for t in tests
        if (t.status or "").lower() in ("повышен", "снижен", "отклонение")
    ]

    summary_parts.append(f"Всего показателей: {len(tests)}")
    summary_parts.append(f"В норме: {normal_count}")
    if abnormal_tests:
        summary_parts.append(f"С отклонениями: {len(abnormal_tests)}")

    if capped:
        summary_parts.append(
            f"⚠️ Обработано {max_tests} из общего числа показателей (лимит)."
        )

    if interpretations:
        summary_parts.append("\n**Интерпретация отклонений:**")
        for interp in interpretations:
            summary_parts.append(f"• {interp}")
    else:
        summary_parts.append(
            "\nЗначимых отклонений от референсных значений не выявлено."
        )

    result.interpretation = "\n".join(summary_parts)

    return result
