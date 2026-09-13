"""真机以 ``uvicorn app:app --app-dir proxy`` 顶层模块方式运行 proxy 目录下的模块。

此时 ``recommend`` / ``app`` 等全是**没有父包的顶层模块**，任何未用
``try/except ImportError`` 兜底的 ``from . import x`` 都会在真机上直接抛
ImportError。v2.9.2 实锤过一次：本地每日推荐在扫描函数里写了一句无兜底的
延迟导入 ``from . import local_library``，一进来就炸、扫描整个报废，表现成
「歌单静默不出现」，且测试全绿（测试是包导入形态，有父包）。

这个静态检查从 AST 层面保证：proxy/*.py 里所有相对导入都必须位于
``except ImportError`` 兜底的 try 块内。
"""
import ast
from pathlib import Path

PROXY_DIR = Path(__file__).resolve().parent.parent


def _import_error_guarded_spans(tree: ast.AST) -> "list[tuple[int, int]]":
    """收集「有 ImportError handler 的 try 块」的 body 行号区间。"""
    spans: "list[tuple[int, int]]" = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        has_guard = False
        for handler in node.handlers:
            t = handler.type
            names = t.elts if isinstance(t, ast.Tuple) else [t]
            if any(isinstance(n, ast.Name) and n.id == "ImportError"
                   for n in names if n is not None):
                has_guard = True
                break
        if not has_guard:
            continue
        for stmt in node.body:
            spans.append((stmt.lineno, getattr(stmt, "end_lineno", None) or stmt.lineno))
    return spans


def test_every_relative_import_is_guarded_for_flat_execution():
    offenders = []
    for py in sorted(PROXY_DIR.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        spans = _import_error_guarded_spans(tree)

        def guarded(lineno: int, spans=spans) -> bool:
            return any(a <= lineno <= b for a, b in spans)

        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.level >= 1
                    and not guarded(node.lineno)):
                offenders.append(
                    f"{py.name}:{node.lineno}: "
                    f"from {'.' * node.level}{node.module or ''} import ...")
    assert not offenders, (
        "这些相对导入没有 ImportError 兜底，真机 uvicorn --app-dir proxy "
        "扁平运行时会直接炸（表现是功能静默失效）:\n" + "\n".join(offenders)
    )
