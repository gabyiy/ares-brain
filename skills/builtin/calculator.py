import ast
import math
import operator
import re

from skills.base import Skill, SkillContext, SkillResponse


class CalculatorError(ValueError):
    pass


class CalculatorSkill(Skill):
    name = "calculator"
    description = "Safely calculates local arithmetic expressions."
    version = "0.1"
    intent_names = ("calculate",)
    run_before_intents = True
    triggers = (
        "calculate",
        "calculator",
        "compute",
        "math",
        "plus",
        "minus",
        "times",
        "multiplied by",
        "divided by",
        "power",
    )
    selection_keywords = (
        "add",
        "subtract",
        "multiply",
        "divide",
        "arithmetic",
    )
    selection_priority = 0.05

    _MAX_ABS_VALUE = 1_000_000_000_000
    _MAX_POWER_EXPONENT = 10
    _MAX_INPUT_LENGTH = 512
    _MAX_EXPRESSION_LENGTH = 256
    _BINARY_OPERATORS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
    }
    _UNARY_OPERATORS = {
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }
    _WORD_OPERATORS = (
        (r"\bmultiplied\s+by\b", "*"),
        (r"\bdivided\s+by\b", "/"),
        (r"\bto\s+the\s+power\s+of\b", "**"),
        (r"\bplus\b", "+"),
        (r"\bminus\b", "-"),
        (r"\btimes\b", "*"),
        (r"\bover\b", "/"),
    )
    _LEADING_PHRASES = (
        r"^please\s+",
        r"^calculate\s+",
        r"^calculator\s+",
        r"^compute\s+",
        r"^solve\s+",
        r"^what\s+is\s+",
        r"^what's\s+",
        r"^how\s+much\s+is\s+",
    )

    def can_handle(self, text: str) -> bool:
        return self._has_calculator_intent(text) or self._extract_expression(text) is not None

    def handle(self, text: str, context: SkillContext) -> SkillResponse:
        try:
            expression = self._extract_expression(text)
        except CalculatorError as error:
            return self._reject(str(error))

        if not expression:
            return self._reject()

        try:
            result = self._evaluate(expression)
        except CalculatorError as error:
            return self._reject(str(error))

        return SkillResponse(
            text=f"Result: {self._format_number(result)}",
            skill=self.name,
            metadata={"expression": expression, "result": result},
        )

    def _extract_expression(self, text: str):
        raw = (text or "").strip().lower()
        if not raw:
            return None
        if len(raw) > self._MAX_INPUT_LENGTH:
            raise CalculatorError("the expression is too long")

        normalized = raw.strip(" ?!.")
        for pattern in self._LEADING_PHRASES:
            normalized = re.sub(pattern, "", normalized).strip()

        for pattern, replacement in self._WORD_OPERATORS:
            normalized = re.sub(pattern, replacement, normalized)

        normalized = normalized.replace("^", "**")
        normalized = normalized.strip()

        if not normalized:
            return None
        if len(normalized) > self._MAX_EXPRESSION_LENGTH:
            raise CalculatorError("the expression is too long")

        if not self._has_allowed_characters(normalized):
            if self._has_calculator_intent(text):
                raise CalculatorError("only numbers and arithmetic operators are allowed")
            return None

        if not self._has_arithmetic_shape(normalized):
            return None

        return normalized

    def _evaluate(self, expression: str):
        try:
            parsed = ast.parse(expression, mode="eval")
        except (SyntaxError, ValueError) as error:
            raise CalculatorError("the expression is not valid arithmetic") from error

        return self._eval_node(parsed.body)

    def _eval_node(self, node):
        if isinstance(node, ast.Constant):
            return self._number(node.value)

        if isinstance(node, ast.BinOp):
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            return self._apply_binary(node.op, left, right)

        if isinstance(node, ast.UnaryOp):
            operand = self._eval_node(node.operand)
            return self._apply_unary(node.op, operand)

        raise CalculatorError("only safe arithmetic expressions are allowed")

    def _apply_binary(self, op, left, right):
        operator_type = type(op)
        if operator_type not in self._BINARY_OPERATORS:
            raise CalculatorError("only +, -, *, /, and powers are allowed")

        if operator_type is ast.Div and right == 0:
            raise CalculatorError("division by zero is not allowed")

        if operator_type is ast.Pow:
            if abs(right) > self._MAX_POWER_EXPONENT:
                raise CalculatorError("power is too large")

        result = self._BINARY_OPERATORS[operator_type](left, right)
        return self._bounded(result)

    def _apply_unary(self, op, operand):
        operator_type = type(op)
        if operator_type not in self._UNARY_OPERATORS:
            raise CalculatorError("only unary + and - are allowed")
        return self._bounded(self._UNARY_OPERATORS[operator_type](operand))

    def _number(self, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CalculatorError("only numbers are allowed")
        return self._bounded(value)

    def _bounded(self, value):
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CalculatorError("result is not finite")
        if abs(value) > self._MAX_ABS_VALUE:
            raise CalculatorError("result is too large")
        return value

    def _has_allowed_characters(self, expression: str) -> bool:
        return bool(re.fullmatch(r"[0-9+\-*/().\s]+", expression))

    def _has_arithmetic_shape(self, expression: str) -> bool:
        has_number = bool(re.search(r"\d", expression))
        has_operator = bool(re.search(r"[+\-*/]", expression))
        return has_number and has_operator

    def _has_calculator_intent(self, text: str) -> bool:
        low = (text or "").lower()
        if any(trigger in low for trigger in self.triggers):
            return True
        return bool(re.search(r"\d\s*(?:[+\-*/^]|\*\*)\s*\d", low))

    def _format_number(self, value) -> str:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return f"{value:.12g}"

    def _reject(self, reason: str = "only safe arithmetic is supported") -> SkillResponse:
        return SkillResponse(
            text=f"I cannot calculate that safely: {reason}.",
            skill=self.name,
            metadata={"error": reason},
        )
