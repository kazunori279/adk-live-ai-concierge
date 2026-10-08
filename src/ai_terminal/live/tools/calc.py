"""Safe mathematical expression evaluation tool for live agent and planner.

Answers arithmetic and multi-step math calculations via a sandboxed AST
evaluator, eliminating hallucinations in numbers, unit formulas, and powers.
"""

from __future__ import annotations

import ast
import math
import operator
from typing import Any

from google.adk.tools import FunctionTool, ToolContext

from ._lang import of_turn

_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_SAFE_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "pow": pow,
    "sqrt": math.sqrt,
    "cbrt": getattr(math, "cbrt", lambda x: x ** (1 / 3)),
    "exp": math.exp,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "radians": math.radians,
    "degrees": math.degrees,
    "factorial": math.factorial,
    "floor": math.floor,
    "ceil": math.ceil,
    "gcd": math.gcd,
    "lcm": getattr(math, "lcm", math.gcd),
}

_SAFE_CONSTANTS: dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "tau": getattr(math, "tau", 2 * math.pi),
    "inf": math.inf,
}

MAX_EXPR_LEN = 500
MAX_POWER = 1000


def _safe_eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, complex)):
            return node.value
        raise ValueError(f"Unsupported constant type: {type(node.value).__name__}")
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _OPERATORS:
            raise ValueError(f"Unsupported operator: {op_type.__name__}")
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        if op_type is ast.Pow:
            if isinstance(right, (int, float)) and abs(right) > MAX_POWER:
                raise ValueError(f"Exponent {right} exceeds safety limit {MAX_POWER}")
            if isinstance(left, (int, float)) and abs(left) > 1e10 and right > 10:
                raise ValueError("Calculation exceeds safety limits")
        return _OPERATORS[op_type](left, right)
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
        operand = _safe_eval(node.operand)
        return _OPERATORS[op_type](operand)
    if isinstance(node, ast.Name):
        if node.id in _SAFE_CONSTANTS:
            return _SAFE_CONSTANTS[node.id]
        if node.id in _SAFE_FUNCTIONS:
            return _SAFE_FUNCTIONS[node.id]
        raise ValueError(f"Unknown variable or function: {node.id}")
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "math":
            if node.attr in _SAFE_CONSTANTS:
                return _SAFE_CONSTANTS[node.attr]
            if node.attr in _SAFE_FUNCTIONS:
                return _SAFE_FUNCTIONS[node.attr]
        raise ValueError(f"Unsupported attribute access: {ast.dump(node)}")
    if isinstance(node, ast.Call):
        func = _safe_eval(node.func)
        if not callable(func):
            raise ValueError(f"Object {func} is not callable")
        args = [_safe_eval(arg) for arg in node.args]
        return func(*args)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_safe_eval(elt) for elt in node.elts]
    raise ValueError(f"Unsupported syntax: {type(node).__name__}")


def calculate(
    expr: str = "",
    lang: str = "en",
    tool_context: ToolContext = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Evaluate a mathematical expression and return the exact numeric result.

    Use this tool for mathematical calculations, arithmetic, formulas, powers,
    percentages, and trigonometric computations rather than guessing in head.

    Args:
        expr: A mathematical expression string (e.g. '125 * 34',
            'sqrt(144) + 2**8', '(1500 * 0.1) / 12', 'sin(radians(30))').
        lang: Language code ('en' or 'ja').
        tool_context: ADK tool context if called in a live session.

    Returns:
        A dict with 'expression', 'result', and 'formatted' string, or 'error'.
    """
    lang = of_turn(lang, tool_context)
    raw = expr or kwargs.get("expression", "")
    target = (raw or "").strip()
    if not target:
        return {"expression": raw, "error": "Empty expression"}
    if len(target) > MAX_EXPR_LEN:
        return {
            "expression": target,
            "error": f"Expression too long (max {MAX_EXPR_LEN} chars)",
        }
    try:
        tree = ast.parse(target, mode="eval")
        res = _safe_eval(tree)
        if isinstance(res, float):
            if res.is_integer():
                formatted = f"{int(res):,}"
            else:
                formatted = f"{res:,.6g}"
        elif isinstance(res, int):
            formatted = f"{res:,}"
        else:
            formatted = str(res)
        return {"expression": target, "result": res, "formatted": formatted}
    except ZeroDivisionError:
        return {"expression": target, "error": "Division by zero"}
    except Exception as err:
        return {"expression": target, "error": str(err)}


calculate_tool = FunctionTool(calculate)
