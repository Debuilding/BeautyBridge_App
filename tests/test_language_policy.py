"""Language policy regression tests for the salon assistant prompts."""

import universal_runtime as ur
import main


CFG = {
    "name": "Rozmary",
    "language": "uk",
    "crm_type": "manual",
    "booking_mode": "table",
    "booking_backend": "table",
    "services": {},
    "masters": {},
}


def test_runtime_prompt_defaults_to_ukrainian_and_forbids_russian_replies():
    prompt = ur.build_prompt("rozmary", CFG, {})
    assert "Мова за замовчуванням — українська" in prompt
    assert "Якщо клієнт пише російською, завжди відповідай українською" in prompt
    assert "не коментуй і не виправляй мову клієнта" in prompt


def test_runtime_prompt_adapts_to_any_non_russian_language():
    prompt = ur.build_prompt("rozmary", CFG, {})
    assert "будь-якою іншою мовою" in prompt
    assert "навіть якщо її немає в переліку налаштувань салону" in prompt
    assert "якщо визначити мову складно — відповідай українською" in prompt


def test_legacy_prompt_has_same_language_policy():
    prompt = main.system_prompt("rozmary", CFG, {})
    assert "якщо клієнт пише російською — завжди відповідай українською" in prompt
    assert "якщо клієнт пише будь-якою іншою мовою — відповідай цією мовою" in prompt
