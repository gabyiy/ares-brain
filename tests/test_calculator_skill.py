from skills import SkillContext, ToolSelector
from skills.builtin.calculator import CalculatorSkill
from skills.builtin.memory_recall import MemoryRecallSkill
from skills.builtin.time_date import TimeDateSkill


def _calculate(text: str) -> str:
    return CalculatorSkill().handle(text, SkillContext()).text


def test_calculator_simple_math():
    assert _calculate("2 + 2") == "Result: 4"
    assert _calculate("calculate 10 - 3") == "Result: 7"
    assert _calculate("6 * 7") == "Result: 42"
    assert _calculate("8 / 2") == "Result: 4"


def test_calculator_operator_precedence():
    assert _calculate("2 + 3 * 4") == "Result: 14"


def test_calculator_parentheses():
    assert _calculate("(2 + 3) * 4") == "Result: 20"


def test_calculator_decimal_calculation():
    assert _calculate("0.5 + 1.25") == "Result: 1.75"


def test_calculator_safe_powers():
    assert _calculate("2 ^ 3") == "Result: 8"
    assert _calculate("2 ** 3") == "Result: 8"


def test_calculator_rejects_unsafe_input():
    assert _calculate("calculate __import__('os')").startswith("I cannot calculate that safely:")
    assert _calculate("2 / 0") == "I cannot calculate that safely: division by zero is not allowed."
    assert _calculate("2 ** 100") == "I cannot calculate that safely: power is too large."


def test_calculator_rejects_excessively_long_direct_expression():
    response = _calculate("calculate " + " + ".join(["1"] * 200))

    assert response == "I cannot calculate that safely: the expression is too long."


def test_tool_selector_selects_calculator_for_math_without_special_router_case():
    selection = ToolSelector().select(
        "what is 2 + 2",
        [TimeDateSkill(), MemoryRecallSkill(), CalculatorSkill()],
        run_before_intents=True,
    )

    assert selection.skill.name == "calculator"
    assert selection.confidence > 0
