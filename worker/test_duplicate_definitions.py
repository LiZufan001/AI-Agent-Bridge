import ast
import unittest
from pathlib import Path

class ProtocolDefinitionTests(unittest.TestCase):
    def test_protocol_functions_have_single_definition(self):
        path=Path(__file__).with_name('protocol_core.py');tree=ast.parse(path.read_text())
        names=[n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
        self.assertEqual(len(names),len(set(names)))
