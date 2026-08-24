"""Safe condition DSL for alerts-mcp trigger rules.

`evaluate(expr, ctx)` compiles `expr` with `ast.parse(mode="eval")` and walks
the tree allowing only a strict whitelist of node types. No `eval`/`exec` is
ever used — the tree is interpreted by hand, so a malicious expression cannot
read secrets, access attributes, or call arbitrary code.

Grammar (subset of Python syntax):
  - Names:     `latest`, `prev`  (bound to the two most recent series values)
  - Funcs:     `crosses_above(t)`, `crosses_below(t)`, `pct_change(n)`,
               `value(n)`, `avg(n)`, `min(n)`, `max(n)`  (fixed whitelist)
  - Ops:       `and`, `or`, `not`, comparisons (`> < >= <= == !=`),
               arithmetic (`+ - * /`) on numbers
  - Constants: numbers, strings, True/False/None

`ctx` is `{"latest": v0, "prev": v1, "series": [v0, v1, v2, ...]}` with series
ordered newest-first. `value(n)` returns series[n] (0 = latest); `pct_change(n)`
returns `(latest - series[n]) / series[n]`; `avg/min/max(n)` aggregate over the
last n values (series[0:n]); `crosses_above(t)` is `prev <= t and latest > t`.
"""
from __future__ import annotations

import ast
import operator as _op
from typing import Any

__all__ = ["ExpressionError", "evaluate"]


class ExpressionError(Exception):
    """Raised for any disallowed construct, unknown name, or eval failure."""


# Whitelisted binary operators.
_BIN_OPS = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.Mod: _op.mod,
    ast.Pow: _op.pow,
}

# Whitelisted unary operators.
_UNARY_OPS = {
    ast.UAdd: _op.pos,
    ast.USub: _op.neg,
    ast.Not: _op.not_,
}

# Whitelisted comparison operators.
_CMP_OPS = {
    ast.Eq: _op.eq,
    ast.NotEq: _op.ne,
    ast.Lt: _op.lt,
    ast.LtE: _op.le,
    ast.Gt: _op.gt,
    ast.GtE: _op.ge,
}

# Whitelisted functions: name -> (min_args, max_args, callable(ctx, args))
_BOOL_FN = {ast.And, ast.Or}


def _fn_crosses_above(ctx: dict, args: list) -> bool:
    t = args[0]
    prev = ctx.get("prev")
    latest = ctx.get("latest")
    if prev is None or latest is None:
        return False
    return prev <= t and latest > t


def _fn_crosses_below(ctx: dict, args: list) -> bool:
    t = args[0]
    prev = ctx.get("prev")
    latest = ctx.get("latest")
    if prev is None or latest is None:
        return False
    return prev >= t and latest < t


def _series(ctx: dict) -> list:
    s = ctx.get("series") or []
    return list(s)


def _fn_value(ctx: dict, args: list):
    n = int(args[0])
    s = _series(ctx)
    if n < 0 or n >= len(s):
        return None
    return s[n]


def _fn_pct_change(ctx: dict, args: list):
    n = int(args[0])
    s = _series(ctx)
    if n <= 0 or n >= len(s):
        return None
    base = s[n]
    latest = s[0]
    if base in (0, None):
        return None
    return (latest - base) / base


def _fn_avg(ctx: dict, args: list):
    n = int(args[0])
    s = _series(ctx)[:n] if n > 0 else _series(ctx)
    if not s:
        return None
    return sum(s) / len(s)


def _fn_min(ctx: dict, args: list):
    n = int(args[0])
    s = _series(ctx)[:n] if n > 0 else _series(ctx)
    if not s:
        return None
    return min(s)


def _fn_max(ctx: dict, args: list):
    n = int(args[0])
    s = _series(ctx)[:n] if n > 0 else _series(ctx)
    if not s:
        return None
    return max(s)


_FUNCS = {
    "crosses_above": (1, 1, _fn_crosses_above),
    "crosses_below": (1, 1, _fn_crosses_below),
    "value": (1, 1, _fn_value),
    "pct_change": (1, 1, _fn_pct_change),
    "avg": (1, 1, _fn_avg),
    "min": (1, 1, _fn_min),
    "max": (1, 1, _fn_max),
}

# Names bound in the context (anything else is rejected).
_NAMES = {"latest", "prev"}


def _eval(node: ast.AST, ctx: dict) -> Any:
    """Recursively evaluate a whitelisted AST node."""

    if isinstance(node, ast.Expression):
        return _eval(node.body, ctx)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id not in _NAMES:
            raise ExpressionError(f"unknown name: {node.id!r}")
        if node.id not in ctx:
            raise ExpressionError(f"name not in context: {node.id!r}")
        return ctx[node.id]

    if isinstance(node, ast.BoolOp):
        op = type(node.op)
        if op not in _BOOL_FN:
            raise ExpressionError(f"bool op not allowed: {op.__name__}")
        values = [_eval(v, ctx) for v in node.values]
        if op is ast.And:
            result = True
            for v in values:
                if not v:
                    return v
                result = v
            return result
        # Or
        for v in values:
            if v:
                return v
        return values[-1] if values else False

    if isinstance(node, ast.UnaryOp):
        op = type(node.op)
        if op not in _UNARY_OPS:
            raise ExpressionError(f"unary op not allowed: {op.__name__}")
        return _UNARY_OPS[op](_eval(node.operand, ctx))

    if isinstance(node, ast.BinOp):
        op = type(node.op)
        if op not in _BIN_OPS:
            raise ExpressionError(f"binary op not allowed: {op.__name__}")
        return _BIN_OPS[op](_eval(node.left, ctx), _eval(node.right, ctx))

    if isinstance(node, ast.Compare):
        # Only single-operator comparisons (a < b); chained (a < b < c) is fine
        # because Python evaluates left-to-right, but we walk pairs explicitly.
        left = _eval(node.left, ctx)
        result = True
        for op, right_node in zip(node.ops, node.comparators):
            op_t = type(op)
            if op_t not in _CMP_OPS:
                raise ExpressionError(f"comparison not allowed: {op_t.__name__}")
            right = _eval(right_node, ctx)
            if not _CMP_OPS[op_t](left, right):
                return False
            left = right
        return result

    if isinstance(node, ast.Call):
        # Only whitelisted bare-name calls with positional args, no kwargs.
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("only direct function calls are allowed")
        fname = node.func.id
        if fname not in _FUNCS:
            raise ExpressionError(f"function not allowed: {fname!r}")
        if node.keywords:
            raise ExpressionError("keyword arguments not allowed")
        min_a, max_a, fn = _FUNCS[fname]
        args = [_eval(a, ctx) for a in node.args]
        if len(args) < min_a or len(args) > max_a:
            raise ExpressionError(
                f"{fname} expects {min_a} arg(s), got {len(args)}"
            )
        return fn(ctx, args)

    # Explicitly rejected constructs (non-exhaustive — anything not above is
    # rejected by falling through).
    raise ExpressionError(f"construct not allowed: {type(node).__name__}")


def evaluate(expr: str, ctx: dict) -> bool:
    """Compile + evaluate `expr` against `ctx`, returning a truthy bool.

    Raises `ExpressionError` for any disallowed construct. Never uses eval/exec.
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ExpressionError("empty condition")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"syntax error: {e.msg}") from e
    # Reject any node type outside the whitelist by scanning first — gives a
    # clear error and a second layer of defense beyond the fallthrough in _eval.
    for node in ast.walk(tree):
        if isinstance(
            node,
            (
                ast.Attribute,
                ast.Subscript,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
                ast.Lambda,
                ast.IfExp,
                ast.JoinedStr,
                ast.FormattedValue,
                ast.Starred,
                ast.Slice,
                ast.Await,
                ast.Yield,
                ast.YieldFrom,
                ast.NamedExpr,
            ),
        ):
            raise ExpressionError(f"construct not allowed: {type(node).__name__}")
    result = _eval(tree, ctx)
    return bool(result)
