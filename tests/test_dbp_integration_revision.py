import ast
from pathlib import Path


def test_public_package_declares_dbp_integration_revision():
    source = Path(__file__).resolve().parents[1] / "pgl" / "__init__.py"
    tree = ast.parse(source.read_text())
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    assert assignments["DBP_INTEGRATION_REVISION"] == "dbp-prepared-block-v2"
