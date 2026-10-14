import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from thrift_java import generate, GenerationError
from thrift_reverse import Generator

COMPILER = os.environ.get('THRIFT_BIN')


@unittest.skipUnless(COMPILER, 'Set THRIFT_BIN to compiler 0.24.0')
class JavaGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / 'java'
        self.fixtures = Path(__file__).parent

    def test_directory_round_trip(self):
        report = generate(self.fixtures, self.output, COMPILER)
        self.assertEqual(len(report['inputs']), 2)
        self.assertEqual(len(report['files']), 9)
        self.assertTrue((self.output / 'fixture/other/Envelope.java').is_file())
        self.assertTrue((self.output / 'fixture/core/Child.java').is_file())
        files, _ = Generator(self.output).run()
        idl = self.root / 'idl'; idl.mkdir()
        for name, content in files.items(): (idl / name).write_text(content)
        java2 = self.root / 'java2'
        generate(idl, java2, COMPILER)
        files2, _ = Generator(java2).run()
        self.assertEqual(files, files2)

    def test_single_file_generates_includes(self):
        report = generate(self.fixtures / 'other.thrift', self.output, COMPILER)
        self.assertEqual(len(report['inputs']), 1)
        self.assertTrue((self.output / 'fixture/core/Item.java').exists())

    def test_compile_failure_leaves_output_untouched(self):
        inputs = self.root / 'idl'; inputs.mkdir()
        (inputs / 'a.thrift').write_text('struct Good { 1: string name }')
        (inputs / 'z.thrift').write_text('struct Broken { 1: Missing name }')
        with self.assertRaises(GenerationError): generate(inputs, self.output, COMPILER)
        self.assertFalse(self.output.exists())

    def test_conflict_does_not_overwrite(self):
        inputs = self.root / 'idl'; inputs.mkdir()
        (inputs / 'a.thrift').write_text('namespace java conflict\nstruct Item { 1: string name }')
        (inputs / 'b.thrift').write_text('namespace java conflict\nstruct Item { 1: i64 id }')
        with self.assertRaisesRegex(GenerationError, 'Conflicting'): generate(inputs, self.output, COMPILER)
        self.assertFalse(self.output.exists())

    def test_overwrite_guard_and_force(self):
        source = self.fixtures / 'features.thrift'
        generate(source, self.output, COMPILER)
        unrelated = self.output / 'keep.txt'; unrelated.write_text('keep')
        with self.assertRaisesRegex(GenerationError, 'Output exists'): generate(source, self.output, COMPILER)
        generate(source, self.output, COMPILER, force=True)
        self.assertEqual(unrelated.read_text(), 'keep')

    def test_compiler_version(self):
        with patch('thrift_java.run', return_value=('Thrift version 0.23.0', '')):
            with self.assertRaisesRegex(GenerationError, 'Expected Thrift 0.24.0'):
                generate(self.fixtures, self.output, COMPILER)

    def test_extra_include_and_java_options(self):
        inputs = self.root / 'idl'; inputs.mkdir()
        (inputs / 'one.thrift').write_text('include "features.thrift"\nnamespace java extra\nstruct One { 1: features.Item item }')
        generate(inputs, self.output, COMPILER, includes=[self.fixtures], options='generated_annotations=suppress')
        content = (self.output / 'extra/One.java').read_text()
        self.assertNotIn('@javax.annotation.Generated', content)

    def test_output_symlink_rejected(self):
        actual = self.root / 'actual'; actual.mkdir()
        self.output.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(GenerationError, 'symlink'): generate(self.fixtures, self.output, COMPILER)
        self.assertEqual(list(actual.iterdir()), [])


if __name__ == '__main__': unittest.main()
