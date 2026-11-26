"""Static undefined-name guard for deployed Python modules (F821 equivalent)."""
import builtins
import ast
import symtable
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULES = (
    "bot.py", "main.py", "whatsapp.py", "whatsapp_state.py",
    "paystack_transfer.py", "reports.py", "receipt_generator.py",
)


def unresolved_names(path):
    source = path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(path))
    top = symtable.symtable(source, str(path), "exec")
    module_names = {
        symbol.get_name() for symbol in top.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()
    }
    builtin_names = set(vars(builtins))
    problems = []

    def visit(table):
        for symbol in table.get_symbols():
            if not symbol.is_referenced() or symbol.is_parameter():
                continue
            name = symbol.get_name()
            if symbol.is_global():
                if name not in module_names and name not in builtin_names:
                    problems.append((table.get_name(), name))
            elif not symbol.is_local() and not symbol.is_free() and name not in builtin_names:
                problems.append((table.get_name(), name))
        for child in table.get_children():
            visit(child)

    visit(top)
    return sorted(set(problems))


class RuntimeUndefinedNameTests(unittest.TestCase):
    def test_runtime_modules_have_no_undefined_names(self):
        findings = {
            filename: unresolved_names(ROOT / filename)
            for filename in RUNTIME_MODULES
            if (ROOT / filename).exists()
        }
        self.assertEqual({name: rows for name, rows in findings.items() if rows}, {})


if __name__ == "__main__":
    unittest.main()
