import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from thrift_reverse import Generator, RecoveryError, tokens, nodes, metadata, main

class LexicalTests(unittest.TestCase):
    def test_comments_and_strings_are_not_scopes(self):
        ns = nodes(tokens('/* class Nope { */ class A { String x = "} class Fake {"; class B {} }'))
        self.assertEqual([(n.name,[c.name for c in n.children]) for n in ns],[('A',['B'])])

    def test_nested_metadata(self):
        t = metadata(tokens('new MapMetaData(TType.MAP, new FieldValueMetaData(TType.STRING), new ListMetaData(TType.LIST, new FieldValueMetaData(TType.STRING, true)))'))
        self.assertEqual((t.name,t.args[1].args[0].name),('map','binary'))

    def test_unknown_metadata_fails(self):
        with self.assertRaises(RecoveryError): metadata(tokens('new FieldValueMetaData(TType.STRUCT)'))

    def test_typedef_metadata_not_silently_dropped(self):
        with self.assertRaises(RecoveryError): metadata(tokens('new FieldValueMetaData(TType.I64, "UserId")'))

COMPILER = os.environ.get('THRIFT_BIN')

@unittest.skipUnless(COMPILER, 'Set THRIFT_BIN for compiler round-trip integration tests')
class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.java = self.root/'java'; self.java.mkdir()
        self.out = self.root/'out'
        fixtures = Path(__file__).parent
        for f in ('features.thrift','other.thrift'):
            self.compile(fixtures/f, self.java)

    def compile(self, source, out):
        subprocess.run([COMPILER,'-out',str(out),'--gen','java',str(source)],check=True,capture_output=True,text=True)

    def test_roundtrip(self):
        files, report = Generator(self.java,'0.24.0').run()
        self.assertEqual(report['counts'], {'enum':1,'struct':3,'exception':1,'union':1,'service':3})
        self.assertEqual(report['methods'],5)
        self.assertEqual(report['warnings'],[])
        self.out.mkdir()
        for name, content in files.items(): (self.out/name).write_text(content)
        java2 = self.root/'java2'; java2.mkdir()
        for name in files: self.compile(self.out/name,java2)
        files2, _ = Generator(java2,'0.24.0').run()
        self.assertEqual(files,files2)
        self.assertIn('oneway void notify(1: string message)', files['pkg_fixture_core.thrift'])
        self.assertIn('throws (2: Failure problem)', files['pkg_fixture_core.thrift'])
        self.assertIn('map<string, list<set<i32>>>', files['pkg_fixture_core.thrift'])

    def test_missing_dependency_fails(self):
        (self.java/'fixture/core/Item.java').unlink()
        with self.assertRaisesRegex(RecoveryError,'unresolved'): Generator(self.java).run()

    def test_duplicate_class_fails(self):
        src = self.java/'fixture/core/Item.java'
        (self.java/'Copy.java').write_text(src.read_text())
        with self.assertRaisesRegex(RecoveryError,'Duplicate'): Generator(self.java).run()

    def test_missing_metadata_fails(self):
        src = self.java/'fixture/core/Item.java'
        src.write_text(src.read_text().replace('new org.apache.thrift.meta_data.FieldMetaData("id"', 'new Bogus("id"'))
        with self.assertRaisesRegex(RecoveryError,'missing field metadata'): Generator(self.java).run()

    def test_unrelated_java_skipped(self):
        (self.java/'App.java').write_text('class App { String x="not thrift"; }')
        _, r = Generator(self.java).run()
        self.assertEqual(r['skipped_java_files'], ['App.java'])

    def test_defaults_explicit_omit(self):
        files, r = Generator(self.java,defaults='omit').run()
        self.assertTrue(r['warnings'])
        self.assertNotIn('mode = Mode.ON',files['pkg_fixture_core.thrift'])

    def test_missing_result_is_not_oneway(self):
        src = self.java/'fixture/core/Base.java'
        src.write_text(src.read_text().replace('class find_result ', 'class hidden_result '))
        with self.assertRaisesRegex(RecoveryError,'missing result'): Generator(self.java).run()

    def test_merge_namespace(self):
        files,_ = Generator(self.java,namespace='merged').run()
        self.assertEqual(list(files),['recovered.thrift'])
        self.assertNotIn('include ',files['recovered.thrift'])
        self.out.mkdir()
        src = self.out/'recovered.thrift'; src.write_text(files['recovered.thrift'])
        self.compile(src,self.out)

    def test_cli_validation_and_overwrite_guard(self):
        args = [str(self.java),'-o',str(self.out),'--target','0.24.0','--validate','--thrift',COMPILER]
        self.assertEqual(main(args),0)
        self.assertTrue(json.loads((self.out/'report.json').read_text())['validation']['performed'])
        saved = (self.out/'report.json').read_bytes()
        self.assertEqual(main(args),2)
        self.assertEqual(saved,(self.out/'report.json').read_bytes())

if __name__ == '__main__': unittest.main()
