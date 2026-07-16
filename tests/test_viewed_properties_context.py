import ast
from pathlib import Path


APP_PATH = Path(__file__).resolve().parents[1] / 'app' / 'app.py'


def _helpers():
    tree = ast.parse(APP_PATH.read_text(encoding='utf-8'), filename=str(APP_PATH))
    wanted = {'_resolve_focus_listing', '_build_viewed_properties_context'}
    module = ast.Module(
        body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted],
        type_ignores=[],
    )
    namespace = {}
    exec(compile(module, str(APP_PATH), 'exec'), namespace)
    return namespace['_build_viewed_properties_context']


def test_ask_ai_history_keeps_multiple_unique_properties():
    build_context = _helpers()
    first = {
        'address': '12 Oxford Rd, Manchester M1 5AN',
        'price': '£1200/month',
        'travel_time': '15 min',
        'url': 'https://example.test/oxford',
    }
    second = {
        'address': '99 Deansgate, Manchester M3 4EN',
        'price': '£1450/month',
        'travel_time': '22 min',
        'url': 'https://example.test/deansgate',
    }

    context = build_context([first, second, first], [], [])

    assert '12 Oxford Rd, Manchester M1 5AN' in context
    assert '99 Deansgate, Manchester M3 4EN' in context
    assert context.count('12 Oxford Rd, Manchester M1 5AN') == 1
