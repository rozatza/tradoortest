"""Strategy IRs the P1/P2 tests already build, paired with hand-built bars.

The collector reads ``schemas/examples`` and the backtest, metrics, jobs,
charts, and agent tests. A ``_strategy`` call that the collector cannot
execute stays in ``uncovered_calls`` so a new fixture cannot be skipped.
Nothing here calls the network. ``random`` is not used.
"""

from __future__ import annotations

import ast
import builtins
import copy
import inspect
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import test_backtest
from test_backtest import _bar

_REPO = Path(__file__).resolve().parents[2]
_TESTS = Path(__file__).resolve().parent
_EXAMPLES = _REPO / "schemas" / "examples"
_HAND = _TESTS / "fixtures" / "backtest"
_PATTERNS = _TESTS / "fixtures" / "runner" / "corpus_bars.json"
_BAR_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
}
_SAFE_CALLS = frozenset(
    {
        "_bar",
        "_dca_trim",
        "_entry_band",
        "_eq",
        "_fixture_bar",
        "_flat",
        "_gt",
        "_money",
        "_oco_hand",
        "_payload",
        "_pyramid",
        "_pyramid_levels",
        "_pyramid_oco",
        "_risk",
        "_round_trip_document",
        "_row",
        "_sma",
        "_strategy",
        "abs",
        "all",
        "any",
        "bool",
        "canonicalize",
        "dict",
        "enumerate",
        "float",
        "format",
        "format_strategy",
        "frozenset",
        "int",
        "ir_hash",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "range",
        "repr",
        "reversed",
        "round",
        "run_backtest",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "validate_ir",
        "zip",
        "Bar",
        "Decimal",
        "Fraction",
        "Path",
    }
)
_SAFE_ATTRS = frozenset(
    {
        "append",
        "copy",
        "deepcopy",
        "dumps",
        "endswith",
        "extend",
        "format",
        "get",
        "isdigit",
        "items",
        "join",
        "keys",
        "loads",
        "lower",
        "read_text",
        "replace",
        "rsplit",
        "split",
        "startswith",
        "strip",
        "update",
        "upper",
        "values",
    }
)
_DANGEROUS = frozenset(
    {
        "Dispatcher",
        "JobQueue",
        "Popen",
        "StrategyStore",
        "TestClient",
        "asyncio",
        "create_app",
        "monkeypatch",
        "sleep",
        "socket",
        "sqlite3",
        "subprocess",
        "threading",
        "tracemalloc",
    }
)
_LOOP_CAP = 256
_SITE: ContextVar[str] = ContextVar("strategy_corpus_site", default="")
# The service forbids a bare eval, exec, or compile call, including one
# fetched from builtins. test_expr.py allowlists these three lines only.
# They run strategy constructors that already live in the test suite.
_EXEC = getattr(builtins, "exec")
_EVAL = getattr(builtins, "eval")
_COMPILE = getattr(builtins, "compile")


class _Missing:
    """A fixture argument the collector must not touch."""


class _Soft:
    """Stand-in for a backtest result. Collection must not run the engine."""

    def __getattr__(self, name: str) -> _Soft:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Soft()

    def __call__(self, *_args: object, **_kwargs: object) -> _Soft:
        return _Soft()

    def __getitem__(self, _item: object) -> _Soft:
        return _Soft()

    def __setitem__(self, _key: object, _value: object) -> None:
        return None

    def __iter__(self) -> Iterator[object]:
        return iter(())

    def __bool__(self) -> bool:
        return False

    def __len__(self) -> int:
        return 0

    def __eq__(self, _other: object) -> bool:
        return False

    def __hash__(self) -> int:
        return 0

    def __deepcopy__(self, _memo: dict[int, object]) -> _Soft:
        return self

    def __repr__(self) -> str:
        return "Soft()"


class _Return(Exception):
    def __init__(self, value: object) -> None:
        self.value = value


class _Continue(Exception):
    pass


class _Break(Exception):
    pass


@dataclass(frozen=True)
class Corpus:
    """Deduped strategy documents and the calls the collector did not run."""

    documents: tuple[tuple[str, dict[str, Any]], ...]
    uncovered_calls: tuple[str, ...]
    harvest_failures: tuple[str, ...]
    string_failures: tuple[str, ...]
    example_names: tuple[str, ...]


def scanned_test_files() -> tuple[Path, ...]:
    """Backtest, metrics, jobs, charts, and agent tests. A new file is included."""
    names = ["test_backtest.py", "test_metrics.py"]
    for prefix in ("test_jobs", "test_charts", "test_agent"):
        names.extend(path.name for path in sorted(_TESTS.glob(f"{prefix}*.py")))
    return tuple(_TESTS / name for name in names)


def bar_pattern_names() -> tuple[str, ...]:
    return tuple(name for name, _spec in _patterns())


def golden_cases() -> tuple[tuple[str, dict[str, Any], dict[str, list[Any]]], ...]:
    """Each collected IR crossed with every bar pattern, including the empty book."""
    cases: list[tuple[str, dict[str, Any], dict[str, list[Any]]]] = []
    for doc_name, document in collect().documents:
        for pattern_name, spec in _patterns():
            bars = _materialize(document, spec)
            cases.append((f"{doc_name}#{pattern_name}", document, bars))
    return tuple(cases)


def collect() -> Corpus:
    """Import the scanned tests and evaluate every strategy constructor."""
    cached = getattr(collect, "_cached", None)
    if cached is not None:
        return cached
    documents: dict[str, dict[str, Any]] = {}
    sites: dict[str, list[str]] = {}
    covered: set[tuple[str, int]] = set()
    failures: list[str] = []
    string_failures: list[str] = []
    calls: list[tuple[str, int, int]] = []

    def record(document: object, site: str) -> None:
        if not isinstance(document, dict) or isinstance(document, _Soft):
            return
        if not _is_strategy(document):
            return
        try:
            cloned = copy.deepcopy(document)
            key = json.dumps(cloned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError):
            return
        documents[key] = cloned
        sites.setdefault(key, []).append(site)

    def fake_run(document: object, _bars: object = None, **_kwargs: object) -> _Soft:
        record(document, _SITE.get() or "run_backtest")
        return _Soft()

    real_strategy = test_backtest._strategy

    def wrapped(**overrides: object) -> dict[str, Any]:
        site = _SITE.get()
        if not site:
            frame = sys._getframe(1)
            site = f"{_rel(frame.f_code.co_filename)}:{frame.f_lineno}"
            covered.add((_rel(frame.f_code.co_filename), frame.f_lineno))
        document = real_strategy(**overrides)
        record(document, site)
        return document

    modules = _import_scanned()
    patched: list[tuple[object, str, object]] = []
    _install_snapshot_recorder.active = record  # type: ignore[attr-defined]
    try:
        for module in modules:
            for key, value in list(vars(module).items()):
                if value is real_strategy:
                    patched.append((module, key, value))
                    setattr(module, key, wrapped)
                elif key == "run_backtest" and getattr(value, "__module__", "") == (
                    "app.backtest.engine"
                ):
                    patched.append((module, key, value))
                    setattr(module, key, fake_run)
        for module in modules:
            _collect_module(
                module,
                fake_run,
                wrapped,
                record,
                covered,
                failures,
                string_failures,
                calls,
            )
        for path in sorted(_EXAMPLES.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            record(document, f"schemas/examples/{path.name}")
    finally:
        _install_snapshot_recorder.active = None  # type: ignore[attr-defined]
        for module, key, value in reversed(patched):
            setattr(module, key, value)

    named: list[tuple[str, dict[str, Any]]] = []
    for key, document in documents.items():
        named.append((_prefer_site(sites[key]), document))
    named.sort(key=lambda item: item[0])
    example_names = tuple(sorted(path.name for path in _EXAMPLES.glob("*.json")))
    uncovered = []
    for path, start, end in calls:
        if not any(path == hit and start <= hit_line <= end for hit, hit_line in covered):
            uncovered.append(f"{path}:{start}")
    result = Corpus(
        documents=tuple(named),
        uncovered_calls=tuple(uncovered),
        harvest_failures=tuple(failures),
        string_failures=tuple(string_failures),
        example_names=example_names,
    )
    collect._cached = result  # type: ignore[attr-defined]
    return result


def _prefer_site(found: list[str]) -> str:
    examples = [site for site in found if site.startswith("schemas/examples/")]
    if examples:
        return min(examples)
    return min(found)


def _import_scanned() -> list[object]:
    import importlib

    modules = []
    for path in scanned_test_files():
        modules.append(importlib.import_module(path.stem))
    return modules


def _collect_module(
    module: object,
    fake_run: Callable[..., _Soft],
    wrapped: Callable[..., dict[str, Any]],
    record: Callable[[object, str], None],
    covered: set[tuple[str, int]],
    failures: list[str],
    string_failures: list[str],
    calls: list[tuple[str, int, int]],
) -> None:
    path = Path(str(getattr(module, "__file__", "")))
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    rel = _rel(str(path))
    for node in ast.walk(tree):
        if _is_strategy_call(node) and hasattr(node, "end_lineno"):
            calls.append((rel, node.lineno, node.end_lineno or node.lineno))
    for name, value in vars(module).items():
        if isinstance(value, dict) and _is_strategy(value):
            record(copy.deepcopy(value), f"{rel}:{name}")
    sandbox = dict(vars(module))
    sandbox["run_backtest"] = fake_run
    sandbox["_strategy"] = wrapped
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            _interpret_function(
                node, sandbox, rel, path, covered, failures, calls_only=False
            )
    _harvest_strings(tree, sandbox, rel, record, string_failures)


def _interpret_function(
    fn: ast.FunctionDef,
    sandbox: dict[str, Any],
    rel: str,
    path: Path,
    covered: set[tuple[str, int]],
    failures: list[str],
    *,
    calls_only: bool,
) -> None:
    if any(isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)) for node in ast.walk(fn)):
        _harvest_tree(fn, {}, sandbox, rel, path, covered, failures)
        return
    local: dict[str, Any] = {}
    if fn.name.startswith("test_"):
        for arg in _all_args(fn):
            local[arg] = _Missing()
    elif _required_args(fn):
        return
    else:
        _bind_defaults(fn, sandbox, local)
    try:
        _interpret(fn.body, local, sandbox, rel, path, covered, failures)
    except _Return:
        return
    except Exception as exc:
        failures.append(f"{rel}:{fn.lineno}: {fn.name}: {type(exc).__name__}: {exc}")


def _interpret(
    statements: list[ast.stmt],
    local: dict[str, Any],
    sandbox: dict[str, Any],
    rel: str,
    path: Path,
    covered: set[tuple[str, int]],
    failures: list[str],
) -> None:
    for stmt in statements:
        _one(stmt, local, sandbox, rel, path, covered, failures)


def _one(
    stmt: ast.stmt,
    local: dict[str, Any],
    sandbox: dict[str, Any],
    rel: str,
    path: Path,
    covered: set[tuple[str, int]],
    failures: list[str],
) -> None:
    if isinstance(stmt, ast.FunctionDef):
        local[stmt.name] = _local_function(stmt, local, sandbox, rel, path, covered, failures)
        return
    if isinstance(stmt, ast.Return):
        value = None if stmt.value is None else _eval(stmt.value, sandbox, local)
        raise _Return(value)
    if isinstance(stmt, ast.Continue):
        raise _Continue()
    if isinstance(stmt, ast.Break):
        raise _Break()
    if isinstance(stmt, (ast.Raise, ast.Pass, ast.Import, ast.ImportFrom, ast.Delete)):
        if isinstance(stmt, ast.Delete) and _runnable(stmt, local, sandbox):
            _exec(stmt, local, sandbox, rel, path, covered)
        else:
            _harvest_tree(stmt, local, sandbox, rel, path, covered, failures)
        return
    if isinstance(stmt, ast.If):
        if not _runnable(stmt.test, local, sandbox):
            for branch in (stmt.body, stmt.orelse):
                scratch = dict(local)
                _interpret(branch, scratch, sandbox, rel, path, covered, failures)
            return
        taken = bool(_eval(stmt.test, sandbox, local))
        branch = stmt.body if taken else stmt.orelse
        _interpret(branch, local, sandbox, rel, path, covered, failures)
        _harvest_tree(
            stmt.orelse if taken else stmt.body,
            local,
            sandbox,
            rel,
            path,
            covered,
            failures,
        )
        return
    if isinstance(stmt, ast.For):
        if not _runnable(stmt.iter, local, sandbox):
            _harvest_tree(stmt, local, sandbox, rel, path, covered, failures)
            scratch = dict(local)
            _bind_target(stmt.target, _Missing(), scratch)
            try:
                _interpret(stmt.body, scratch, sandbox, rel, path, covered, failures)
            except (_Continue, _Break):
                return
            return
        try:
            items = _bounded(_eval(stmt.iter, sandbox, local, rel, covered))
        except Exception as exc:
            failures.append(f"{rel}:{stmt.lineno}: {type(exc).__name__}: {exc}")
            _harvest_tree(stmt, local, sandbox, rel, path, covered, failures)
            return
        for item in items:
            _bind_target(stmt.target, item, local)
            try:
                _interpret(stmt.body, local, sandbox, rel, path, covered, failures)
            except _Continue:
                continue
            except _Break:
                break
        if not items:
            _harvest_tree(stmt, local, sandbox, rel, path, covered, failures)
        return
    if isinstance(stmt, ast.While):
        scratch = dict(local)
        try:
            _interpret(stmt.body, scratch, sandbox, rel, path, covered, failures)
        except (_Continue, _Break):
            return
        return
    if isinstance(stmt, ast.Try):
        _interpret(stmt.body, local, sandbox, rel, path, covered, failures)
        for handler in stmt.handlers:
            scratch = dict(local)
            if handler.name:
                scratch[handler.name] = _Soft()
            _interpret(handler.body, scratch, sandbox, rel, path, covered, failures)
        _interpret(stmt.finalbody, local, sandbox, rel, path, covered, failures)
        return
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        _interpret(getattr(stmt, "body"), local, sandbox, rel, path, covered, failures)
        return
    if not _runnable(stmt, local, sandbox):
        _harvest_tree(stmt, local, sandbox, rel, path, covered, failures)
        _drop_assignments(stmt, local)
        return
    try:
        _exec(stmt, local, sandbox, rel, path, covered)
    except AssertionError:
        _snapshot(local, rel, stmt.lineno)
    except _Return:
        raise
    except Exception as exc:
        failures.append(f"{rel}:{stmt.lineno}: {type(exc).__name__}: {exc}")
        _harvest_tree(stmt, local, sandbox, rel, path, covered, failures)
        _drop_assignments(stmt, local)
        return
    try:
        _snapshot(local, rel, stmt.lineno)
    except Exception as exc:
        failures.append(f"{rel}:{stmt.lineno}: snapshot: {type(exc).__name__}: {exc}")


def _local_function(
    fn: ast.FunctionDef,
    enclosing: dict[str, Any],
    sandbox: dict[str, Any],
    rel: str,
    path: Path,
    covered: set[tuple[str, int]],
    failures: list[str],
) -> Callable[..., object]:
    def call(*args: object, **kwargs: object) -> object:
        inner = dict(enclosing)
        _bind_call(fn, args, kwargs, sandbox, inner)
        try:
            _interpret(fn.body, inner, sandbox, rel, path, covered, failures)
        except _Return as returned:
            return returned.value
        return None

    call._corpus_local = True  # type: ignore[attr-defined]
    return call


def _exec(
    stmt: ast.stmt,
    local: dict[str, Any],
    sandbox: dict[str, Any],
    rel: str,
    path: Path,
    covered: set[tuple[str, int]],
) -> None:
    calls = [node for node in ast.walk(stmt) if _is_strategy_call(node)]
    site = f"{rel}:{calls[0].lineno}" if calls else ""
    token = _SITE.set(site) if site else None
    try:
        source = ast.unparse(stmt)
        _EXEC(_COMPILE(source, str(path), "exec"), sandbox, local)
        for node in calls:
            covered.add((rel, node.lineno))
    finally:
        if token is not None:
            _SITE.reset(token)


def _harvest_tree(
    tree: ast.AST,
    local: dict[str, Any],
    sandbox: dict[str, Any],
    rel: str,
    path: Path,
    covered: set[tuple[str, int]],
    failures: list[str],
) -> None:
    nodes: list[ast.AST] = tree if isinstance(tree, list) else [tree]  # type: ignore[assignment]
    for root in nodes:
        for node in ast.walk(root):
            if _is_strategy_call(node) and not _reads_missing(node, local):
                site = f"{rel}:{node.lineno}"
                token = _SITE.set(site)
                try:
                    _eval(node, sandbox, local, rel, covered)
                    covered.add((rel, node.lineno))
                except Exception as exc:
                    failures.append(f"{site}: {type(exc).__name__}: {exc}")
                finally:
                    _SITE.reset(token)
                continue
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            target = local.get(node.func.id)
            if not getattr(target, "_corpus_local", False) or _reads_missing(node, local):
                continue
            if not _runnable(node, local, sandbox):
                continue
            try:
                _eval(node, sandbox, local, rel, covered)
            except Exception as exc:
                failures.append(f"{rel}:{node.lineno}: {type(exc).__name__}: {exc}")


def _harvest_strings(
    tree: ast.AST,
    sandbox: dict[str, Any],
    rel: str,
    record: Callable[[object, str], None],
    string_failures: list[str],
) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if '"ir_version"' not in node.value:
            continue
        try:
            embedded = ast.parse(node.value)
        except SyntaxError as exc:
            string_failures.append(f"{rel}:{node.lineno}: {exc}")
            continue
        local: dict[str, Any] = {}
        try:
            for stmt in embedded.body:
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    continue
                if not _runnable(stmt, local, sandbox):
                    continue
                source = ast.unparse(stmt)
                _EXEC(_COMPILE(source, f"{rel}:string", "exec"), sandbox, local)
                _snapshot(local, f"{rel}:string", node.lineno)
        except Exception as exc:
            string_failures.append(f"{rel}:{node.lineno}: {type(exc).__name__}: {exc}")
        for value in local.values():
            if isinstance(value, dict) and _is_strategy(value):
                record(value, f"{rel}:{node.lineno}")


def _snapshot(local: dict[str, Any], rel: str, lineno: int) -> None:
    # Documents are recorded by the wrapped constructor and the fake runner.
    # A mutated document sitting in a local is recorded here as well.
    site = f"{rel}:{lineno}"
    seen: list[object] = list(local.values())
    for value in seen:
        _record_local(value, site)


def _record_local(value: object, site: str) -> None:
    # Imported lazily so a snapshot during early import cannot recurse.
    record = _record_local_target
    if record is None:
        return
    if isinstance(value, dict):
        if _is_strategy(value):
            record(value, site)
        inner = value.get("ir")
        if isinstance(inner, dict) and _is_strategy(inner):
            record(inner, site)
    elif isinstance(value, tuple):
        for item in value:
            if isinstance(item, dict):
                _record_local(item, site)


_record_local_target: Callable[[object, str], None] | None = None


def _patterns() -> tuple[tuple[str, list[dict[str, Any]] | None], ...]:
    payload = json.loads(_PATTERNS.read_text(encoding="utf-8"))
    patterns: list[tuple[str, list[dict[str, Any]] | None]] = [
        (name, spec) for name, spec in payload["patterns"].items()
    ]
    hand = json.loads((_HAND / "hand_round_trip.json").read_text(encoding="utf-8"))
    patterns.append(("hand_round_trip", [_from_offset(item) for item in hand["bars"]]))
    oco = json.loads((_HAND / "hand_oco.json").read_text(encoding="utf-8"))
    entry = _from_offset(oco["entry"])
    pairs = {
        "hand_oco_entry": [entry],
        "hand_oco_take_profit": [entry, _from_offset(oco["take_profit_bar"])],
        "hand_oco_stop_loss": [entry, _from_offset(oco["stop_loss_bar"])],
        "hand_oco_both": [entry, _from_offset(oco["both_bar"])],
        "hand_oco_gap": [entry, _from_offset(oco["gap_bar"])],
        "hand_oco_after_hole": [entry, _from_offset(oco["after_synthetic_bar"])],
        "hand_oco_after_synthetic": [
            entry,
            {
                "step": 1,
                "open": 90,
                "high": 90,
                "low": 90,
                "close": 90,
                "volume": 0,
                "synthetic": True,
            },
            _from_offset(oco["after_synthetic_bar"]),
        ],
    }
    patterns.extend(pairs.items())
    patterns.append(("missing", None))
    return tuple(patterns)


def _from_offset(item: Mapping[str, Any]) -> dict[str, Any]:
    offset = int(item["offset"])
    if offset % 60 != 0:
        raise ValueError(f"hand bar offset {offset} is not a 1m step")
    return {
        "step": offset // 60,
        "open": item["open"],
        "high": item["high"],
        "low": item["low"],
        "close": item["close"],
        "volume": item["volume"],
        "synthetic": False,
    }


def _materialize(
    document: Mapping[str, Any], spec: list[dict[str, Any]] | None
) -> dict[str, list[Any]]:
    if spec is None:
        return {}
    size = "1m"
    engine = document.get("engine")
    if isinstance(engine, dict) and engine.get("bar_size") in _BAR_SECONDS:
        size = str(engine["bar_size"])
    step = _BAR_SECONDS[size]
    bars = [_one_bar(item, step, size) for item in spec]
    return {asset_id: list(bars) for asset_id in _asset_ids(document)}


def _one_bar(item: Mapping[str, Any], step: int, size: str) -> object:
    offset = int(item["step"]) * step
    close = float(item["close"])
    if item.get("synthetic"):
        return _bar(offset, close, synthetic=True, timeframe=size)
    return _bar(
        offset,
        close,
        open_=float(item["open"]),
        high=float(item["high"]),
        low=float(item["low"]),
        volume=float(item["volume"]),
        timeframe=size,
    )


def _asset_ids(document: Mapping[str, Any]) -> list[str]:
    assets = document.get("assets")
    if isinstance(assets, list):
        found = []
        for asset in assets:
            if isinstance(asset, dict) and isinstance(asset.get("id"), str):
                found.append(str(asset["id"]))
        if found:
            return found
    return ["sol"]


def _is_strategy(document: Mapping[str, Any]) -> bool:
    if not isinstance(document, dict):
        return False
    if "ir_version" in document and "rules" in document:
        return True
    return "rules" in document and "actions" in document and "signals" in document


def _is_strategy_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "_strategy"
    return isinstance(func, ast.Attribute) and func.attr == "_strategy"


def _runnable(node: ast.AST, local: dict[str, Any], sandbox: dict[str, Any]) -> bool:
    if _reads_missing(node, local):
        return False
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if not _call_is_safe(child, local, sandbox):
            return False
    return True


def _call_is_safe(node: ast.Call, local: dict[str, Any], sandbox: dict[str, Any]) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in _SAFE_CALLS or func.id in local and callable(local[func.id]):
            return True
        target = local.get(func.id, sandbox.get(func.id))
        return _safe_function(target)
    if isinstance(func, ast.Attribute):
        return func.attr in _SAFE_ATTRS
    return False


def _safe_function(target: object) -> bool:
    if not inspect.isfunction(target):
        return False
    module = getattr(target, "__module__", "")
    if module.startswith("app."):
        return getattr(target, "__name__", "") in _SAFE_CALLS
    if not module.startswith("test_"):
        return False
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError):
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _DANGEROUS:
            return False
        if isinstance(node, ast.Attribute) and node.attr in _DANGEROUS:
            return False
    return True


def _reads_missing(node: ast.AST, local: dict[str, Any]) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            if local.get(child.id, None) is _Missing() or (
                child.id in local and isinstance(local[child.id], _Missing)
            ):
                return True
    return False


def _eval(
    node: ast.AST,
    sandbox: dict[str, Any],
    local: dict[str, Any],
    rel: str = "",
    covered: set[tuple[str, int]] | None = None,
) -> object:
    calls = [child for child in ast.walk(node) if _is_strategy_call(child)]
    site = f"{rel}:{calls[0].lineno}" if rel and calls else ""
    token = _SITE.set(site) if site else None
    try:
        source = ast.unparse(node)
        value = _EVAL(source, sandbox, local)
        if covered is not None:
            for child in calls:
                covered.add((rel, child.lineno))
        return value
    finally:
        if token is not None:
            _SITE.reset(token)


def _bounded(value: object) -> list[object]:
    if isinstance(value, range):
        if len(value) > _LOOP_CAP:
            return []
        return list(value)
    if isinstance(value, (str, bytes)):
        return []
    if isinstance(value, (list, tuple)):
        if len(value) > _LOOP_CAP:
            return list(value[:_LOOP_CAP])
        return list(value)
    return []


def _bind_target(target: ast.AST, value: object, local: dict[str, Any]) -> None:
    if isinstance(target, ast.Name):
        local[target.id] = value
        return
    if isinstance(target, ast.Tuple):
        seq = list(value) if isinstance(value, (list, tuple)) else []
        for child, item in zip(target.elts, seq, strict=False):
            _bind_target(child, item, local)


def _drop_assignments(stmt: ast.stmt, local: dict[str, Any]) -> None:
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local[node.id] = _Missing()


def _all_args(fn: ast.FunctionDef) -> list[str]:
    names = [arg.arg for arg in fn.args.args]
    names.extend(arg.arg for arg in fn.args.kwonlyargs)
    if fn.args.vararg:
        names.append(fn.args.vararg.arg)
    if fn.args.kwarg:
        names.append(fn.args.kwarg.arg)
    return names


def _bind_varargs(fn: ast.FunctionDef, local: dict[str, Any]) -> None:
    if fn.args.vararg and fn.args.vararg.arg not in local:
        local[fn.args.vararg.arg] = ()
    if fn.args.kwarg and fn.args.kwarg.arg not in local:
        local[fn.args.kwarg.arg] = {}


def _required_args(fn: ast.FunctionDef) -> bool:
    positional = fn.args.args
    if len(fn.args.defaults) < len(positional):
        return True
    kw_defaults = fn.args.kw_defaults
    return any(default is None for default in kw_defaults)


def _bind_defaults(fn: ast.FunctionDef, sandbox: dict[str, Any], local: dict[str, Any]) -> None:
    positional = fn.args.args
    defaults = fn.args.defaults
    offset = len(positional) - len(defaults)
    for arg in positional[:offset]:
        local[arg.arg] = _Missing()
    for arg, default in zip(positional[offset:], defaults, strict=True):
        local[arg.arg] = _eval(default, sandbox, local)
    for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True):
        if default is None:
            local[arg.arg] = _Missing()
        else:
            local[arg.arg] = _eval(default, sandbox, local)
    _bind_varargs(fn, local)


def _bind_call(
    fn: ast.FunctionDef,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    sandbox: dict[str, Any],
    local: dict[str, Any],
) -> None:
    positional = fn.args.args
    defaults = fn.args.defaults
    required = len(positional) - len(defaults)
    remaining = dict(kwargs)
    for index, arg in enumerate(positional):
        if index < len(args):
            local[arg.arg] = args[index]
        elif arg.arg in remaining:
            local[arg.arg] = remaining.pop(arg.arg)
        else:
            local[arg.arg] = _eval(defaults[index - required], sandbox, local)
    if fn.args.vararg:
        local[fn.args.vararg.arg] = args[len(positional) :]
    for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True):
        if arg.arg in remaining:
            local[arg.arg] = remaining.pop(arg.arg)
        elif default is not None:
            local[arg.arg] = _eval(default, sandbox, local)
    if fn.args.kwarg and fn.args.kwarg.arg not in local:
        local[fn.args.kwarg.arg] = remaining


def _rel(filename: str) -> str:
    path = Path(filename)
    try:
        return str(path.resolve().relative_to(_REPO))
    except ValueError:
        return str(path)


def _install_snapshot_recorder() -> None:
    global _record_local_target

    def record(document: object, site: str) -> None:
        # Filled in once collect() is running. Module import records nothing.
        active = _install_snapshot_recorder.active
        if active is not None:
            active(document, site)

    _install_snapshot_recorder.active = None  # type: ignore[attr-defined]
    _record_local_target = record


_install_snapshot_recorder()
