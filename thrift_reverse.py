#!/usr/bin/env python3
"""Recover Apache Thrift IDL from generated Java, without executing Java code."""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

VERSION = '1.1.0'
TOKEN = re.compile(r'\s+|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[fFdDlL]?|.', re.ASCII)
IDENT = re.compile(r'^[A-Za-z_][A-Za-z_0-9]*$')
PRIMITIVES = {'BOOL':'bool', 'BYTE':'byte', 'I16':'i16', 'I32':'i32', 'I64':'i64', 'DOUBLE':'double', 'STRING':'string', 'UUID':'uuid'}

class RecoveryError(Exception):
    pass

def require(condition, message):
    if not condition:
        raise RecoveryError(message)

def tokens(source):
    # Comments and quoted strings are tokens: braces in them cannot alter scopes.
    return [x for x in TOKEN.findall(source) if not x.isspace() and not x.startswith(('//', '/*'))]

def closing(ts, start):
    pairs = {'(':')', '[':']', '{':'}'}
    stack = []
    for i in range(start, len(ts)):
        if ts[i] in pairs:
            stack.append(pairs[ts[i]])
        elif ts[i] in pairs.values():
            require(stack and stack.pop() == ts[i], 'Unbalanced Java delimiters')
            if not stack:
                return i
    raise RecoveryError('Unclosed Java delimiter')

def split(ts, separator=','):
    result, start, depth = [], 0, 0
    for i, t in enumerate(ts):
        if t in ('(', '[', '{'): depth += 1
        if t in (')', ']', '}'): depth -= 1
        if t == separator and depth == 0:
            result.append(ts[start:i]); start = i + 1
    if start < len(ts): result.append(ts[start:])
    return result

def text(ts):
    return ''.join(ts)

def simple(t):
    return t.rsplit('.', 1)[-1]

def integer(ts):
    s = text(ts)
    s = re.sub(r'^\((?:short|byte|int|long)\)', '', s)
    require(bool(re.fullmatch(r'[+-]?\d+[lL]?', s)), f'Unsupported integer: {s}')
    return int(s.rstrip('lL'))

def string(t):
    require(t.startswith('"'), f'Expected string, got {t}')
    try:
        # Java octal escapes are rare in identifiers; fail rather than corrupt them.
        return json.loads(t)
    except ValueError as e:
        raise RecoveryError(f'Unsupported Java string escape: {t}') from e

def identifier(s):
    require(bool(IDENT.fullmatch(s)), f'Invalid Thrift identifier: {s}')
    return s

def calls(ts, name):
    for i in range(len(ts)-2):
        if ts[i] == 'new' and simple(ts[i+1]) == name and ts[i+2] == '(':
            end = closing(ts, i+2)
            yield split(ts[i+3:end])

@dataclass
class Node:
    name: str
    kind: str
    header: list
    body: list
    children: list = field(default_factory=list)


def nodes(ts):
    result, i = [], 0
    while i < len(ts):
        if ts[i] not in ('class', 'enum', 'interface'):
            i += 1; continue
        require(i+1 < len(ts), 'Incomplete Java type declaration')
        name, kind = ts[i+1], ts[i]
        j = i+2
        while j < len(ts) and ts[j] != '{': j += 1
        require(j < len(ts), f'Missing body for {name}')
        end = closing(ts, j)
        body = ts[j+1:end]
        result.append(Node(name, kind, ts[i:j], body, nodes(body)))
        i = end+1
    return result

@dataclass
class Type:
    name: str
    args: list = field(default_factory=list)
    ref: str | None = None

@dataclass
class Field:
    id: int
    name: str
    requirement: str
    type: Type
    default: str | None = None

@dataclass
class Definition:
    name: str
    package: str
    kind: str
    source: str
    node: Node
    imports: list
    fields: list = field(default_factory=list)
    values: list = field(default_factory=list)
    methods: list = field(default_factory=list)
    base: str | None = None

    @property
    def key(self):
        return f'{self.package}.{self.name}' if self.package else self.name


def metadata(ts):
    require(len(ts) >= 4 and ts[0] == 'new' and ts[2] == '(' and closing(ts, 2) == len(ts)-1,
            f'Unsupported metadata expression: {text(ts)}')
    name, args = simple(ts[1]), split(ts[3:-1])
    require(bool(args), 'Empty metadata constructor')
    wire = simple(text(args[0]))
    if name in ('StructMetaData', 'EnumMetaData'):
        require(len(args) == 2 and text(args[1]).endswith('.class'), 'Missing metadata class')
        return Type('ref', ref=text(args[1])[:-6])
    if name in ('ListMetaData', 'SetMetaData', 'MapMetaData'):
        count = 3 if name == 'MapMetaData' else 2
        require(len(args) == count, f'Invalid {name}')
        return Type({'ListMetaData':'list', 'SetMetaData':'set', 'MapMetaData':'map'}[name],
                    [metadata(a) for a in args[1:]])
    require(name == 'FieldValueMetaData' and wire in PRIMITIVES,
            f'Unsupported metadata: {name}({wire})')
    require(len(args) <= 2, f'Unsupported metadata arguments: {text(ts)}')
    if len(args) == 2:
        require(text(args[1]) in ('true', 'false'),
                'Typedef metadata requires manual reconstruction; refusing to discard typedef')
        require(wire == 'STRING' or text(args[1]) == 'false', 'Unexpected binary flag')
    return Type('binary' if len(args) == 2 and text(args[1]) == 'true' else PRIMITIVES[wire])


def recover_fields(node):
    ids, wire_types = {}, {}
    for a in calls(node.body, 'TField'):
        require(len(a) == 3, f'{node.name}: invalid TField')
        name, fid = string(text(a[0])), integer(a[2])
        require(-32768 <= fid <= 32767, f'{node.name}: field id outside i16')
        require(name not in ids, f'{node.name}: duplicate field descriptor {name}')
        ids[name], wire_types[name] = fid, simple(text(a[1]))
    fields = []
    for a in calls(node.body, 'FieldMetaData'):
        require(len(a) == 3, f'{node.name}: invalid FieldMetaData')
        name, req = string(text(a[0])), simple(text(a[1]))
        require(name in ids, f'{node.name}: metadata has no TField for {name}')
        require(req in ('DEFAULT', 'OPTIONAL', 'REQUIRED'), f'Unknown requiredness: {req}')
        typ = metadata(a[2])
        expected = {'list':'LIST','set':'SET','map':'MAP','binary':'STRING', **{v:k for k,v in PRIMITIVES.items()}}.get(typ.name)
        if typ.ref:
            expected = 'I32' if 'EnumMetaData' in {simple(t) for t in a[2]} else 'STRUCT'
        require(wire_types[name] == expected, f'{node.name}.{name}: descriptor/metadata type mismatch')
        fields.append(Field(ids[name], identifier(name), {'DEFAULT':'', 'OPTIONAL':'optional', 'REQUIRED':'required'}[req], typ))
    require(len(fields) == len(ids), f'{node.name}: missing field metadata (descriptors={len(ids)}, metadata={len(fields)})')
    require(len({f.name for f in fields}) == len(fields), f'{node.name}: duplicate field names')
    require(len({f.id for f in fields}) == len(fields), f'{node.name}: duplicate field IDs')
    # Empty structs still have a metadata registration; otherwise an unsupported generator is likely.
    require(any(simple(t) == 'metaDataMap' for t in node.body), f'{node.name}: metaDataMap missing')
    return fields


def constructor(node):
    ts = node.body
    for i in range(len(ts)-3):
        if ts[i:i+4] == [node.name, '(', ')', '{']:
            return ts[i+4:closing(ts, i+3)]
    return []


def literal(ts, typ, resolve, render_ref):
    s = text(ts)
    s = re.sub(r'^\((?:byte|short|int|long|double|float)\)', '', s)
    if typ.name == 'ref':
        require(bool(re.fullmatch(r'[\w.]+', s)) and '.' in s, f'Unsupported enum default {s}')
        owner, value = s.rsplit('.', 1)
        require(resolve(owner) == resolve(typ.ref), f'Unexpected enum default type {s}')
        return render_ref(resolve(owner)) + '.' + identifier(value)
    if typ.name in ('string', 'binary'):
        require(len(ts) == 1 and ts[0].startswith('"') and typ.name == 'string', f'Unsupported {typ.name} default: {s}')
        return json.dumps(string(ts[0]), ensure_ascii=False)
    if typ.name == 'bool':
        require(s in ('true', 'false'), f'Invalid boolean default: {s}')
        return s
    if typ.name in ('byte', 'i16', 'i32', 'i64'):
        return str(integer(tokens(s)))
    if typ.name == 'double':
        require(bool(re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?[dDfF]?', s)), f'Unsupported double default: {s}')
        return s.rstrip('dDfF')
    raise RecoveryError(f'Unsupported default for {typ.name}: {s}')


class Generator:
    def __init__(self, root, target='portable', namespace=None, defaults='strict', excludes=()):
        self.root, self.target, self.namespace, self.defaults = root, target, namespace, defaults
        self.excludes = excludes
        self.defs = {}
        self.warnings = []
        self.scanned = 0
        self.skipped = []
        self.versions = set()
        self.files = {}
        self.dependencies = {}

    def scan(self):
        for path in sorted(self.root.rglob('*.java')):
            if path.is_symlink() or any(path.relative_to(self.root).match(p) for p in self.excludes): continue
            self.scanned += 1
            source = path.read_text(encoding='utf-8-sig')
            self.versions.update(re.findall(r'Thrift Compiler \(([^)]+)\)', source))
            ts = tokens(source)
            package = next((ts[i+1] for i,t in enumerate(ts[:-1]) if t == 'package'), '')
            imports = []
            for i,t in enumerate(ts[:-1]):
                if t == 'import':
                    j = ts.index(';', i)
                    imports.append(text(ts[i+1:j]))
            recognized = False
            for node in nodes(ts):
                header = {simple(t) for t in node.header}
                if node.kind == 'enum' and 'TEnum' in header: kind = 'enum'
                elif 'TUnion' in header: kind = 'union'
                elif 'TBase' in header: kind = 'exception' if 'TException' in header else 'struct'
                elif any(c.name == 'Iface' and c.kind == 'interface' for c in node.children): kind = 'service'
                else:
                    if 'Autogenerated by Thrift Compiler' in source:
                        raise RecoveryError(f'{path}: unsupported generated class {node.name} (e.g. constants)')
                    continue
                recognized = True
                d = Definition(identifier(node.name), package, kind, str(path.relative_to(self.root)), node, imports)
                require(d.key not in self.defs, f'Duplicate Java type: {d.key}; narrow input or use --exclude')
                self.defs[d.key] = d
            if not recognized: self.skipped.append(str(path.relative_to(self.root)))
        require(bool(self.defs), 'No supported generated Thrift Java classes found')
        packages = sorted({d.package for d in self.defs.values()})
        if self.namespace is not None:
            require(all(IDENT.fullmatch(p) for p in self.namespace.split('.')), 'Invalid --namespace')
            names = [d.name for d in self.defs.values()]
            require(len(set(names)) == len(names), 'Duplicate names cannot be merged into one IDL')
            self.files = {p:'recovered' for p in packages}
        else:
            self.files = {p:('pkg_' + p.replace('.', '_') if p else 'default_package') for p in packages}
            require(len(set(self.files.values())) == len(packages), 'Package filename collision; use --namespace')
        self.dependencies = {f:set() for f in self.files.values()}

    def resolve(self, name, d):
        if name in self.defs: return name
        local = f'{d.package}.{name}' if d.package else name
        if local in self.defs: return local
        explicit = [i for i in d.imports if i.endswith('.'+name) and i in self.defs]
        if len(explicit) == 1: return explicit[0]
        wild = [i[:-1]+name for i in d.imports if i.endswith('.*') and i[:-1]+name in self.defs]
        require(len(wild) == 1, f'{d.key}: unresolved or ambiguous Java type {name}; include its source directory')
        return wild[0]

    def reference(self, key, d):
        target = self.defs[key]
        own, other = self.files[d.package], self.files[target.package]
        if own != other:
            self.dependencies[own].add(other)
            return f'{other}.{target.name}'
        return target.name

    def type_text(self, t, d):
        if t.ref:
            key = self.resolve(t.ref, d)
            require(self.defs[key].kind != 'service', f'{key}: service used as field type')
            return self.reference(key, d)
        name = 'i8' if self.target == '0.24.0' and t.name == 'byte' else t.name
        return name + ('<' + ', '.join(self.type_text(a,d) for a in t.args) + '>' if t.args else '')

    def defaults_for(self, node, fields, d):
        by_name = {f.name:f for f in fields}
        body = constructor(node)
        for statement in split(body, ';'):
            if '=' not in statement: continue
            pos = statement.index('=')
            lhs = text(statement[:pos])
            name = lhs.removeprefix('this.')
            if name not in by_name:
                # Collection-building defaults cannot be faithfully evaluated.
                if any(f'this.{n}' in statement for n in by_name):
                    raise RecoveryError(f'{d.key}: unsupported constructor statement {text(statement)}')
                continue
            rhs = statement[pos+1:]
            if rhs == ['null']: continue
            f = by_name[name]
            try:
                require(self.defaults != 'omit', 'Defaults omitted by option')
                if f.type.ref:
                    target = self.defs[self.resolve(f.type.ref, d)]
                    require(target.kind == 'enum', 'Struct default cannot be evaluated')
                    value = text(rhs).rsplit('.', 1)[-1]
                    require(value in {v[0] for v in target.values}, f'Unknown enum default {value}')
                f.default = literal(rhs, f.type, lambda n:self.resolve(n,d), lambda k:self.reference(k,d))
            except RecoveryError as e:
                if self.defaults == 'strict': raise RecoveryError(f'{d.key}.{name}: {e}') from e
                self.warnings.append(f'{d.key}.{name}: default omitted: {text(rhs)} ({e})')
        # Detect mutation-based defaults, not just assignments.
        for f in fields:
            if any(t.startswith(f'this.{f.name}.') or t.startswith(f'{f.name}.') for t in body):
                msg = f'{d.key}.{f.name}: constructor mutates field; complex default is unsupported'
                if self.defaults == 'strict': raise RecoveryError(msg)
                f.default = None
                self.warnings.append(msg)

    def field_text(self, f, d, requirement=True):
        req = f.requirement + ' ' if f.requirement and requirement else ''
        result = f'{f.id}: {req}{self.type_text(f.type,d)} {f.name}'
        return result + (f' = {f.default}' if f.default is not None else '')

    def parse(self):
        # Enum defaults can reference enums appearing later in the input tree.
        for d in self.defs.values():
            if d.kind != 'enum': continue
            ts = d.node.body
            require(';' in ts, f'{d.key}: malformed enum')
            for entry in split(ts[:ts.index(';')]):
                require(len(entry) >= 4 and entry[1] == '(' and entry[-1] == ')', f'{d.key}: unsupported enum member')
                d.values.append((identifier(entry[0]), integer(entry[2:-1])))
            require(len({v[0] for v in d.values}) == len(d.values), f'{d.key}: duplicate enum name')
        for d in self.defs.values():
            if d.kind in ('struct', 'exception', 'union'):
                d.fields = recover_fields(d.node)
                self.defaults_for(d.node, d.fields, d)
                if d.kind == 'union':
                    require(all(f.requirement != 'required' and f.default is None for f in d.fields), f'{d.key}: invalid union fields')
            elif d.kind == 'service': self.parse_service(d)

    def parse_service(self, d):
        iface = next(c for c in d.node.children if c.name == 'Iface')
        if 'extends' in iface.header:
            base = text(iface.header[iface.header.index('extends')+1:])
            require(base.endswith('.Iface'), f'{d.key}: unsupported service inheritance')
            d.base = self.resolve(base[:-6],d)
            require(self.defs[d.base].kind == 'service', 'Service base is not a service')
        children = {c.name:c for c in d.node.children}
        for signature in split(iface.body, ';'):
            if not signature: continue
            require('(' in signature, f'{d.key}: unsupported Iface member')
            pos = signature.index('(')
            name = identifier(signature[pos-1])
            args_node = children.get(name+'_args')
            result_node = children.get(name+'_result')
            require(args_node is not None, f'{d.key}.{name}: missing args class')
            args = recover_fields(args_node)
            self.defaults_for(args_node,args,d)
            # isOneway in synchronous Processor is authoritative; absence of result is insufficient.
            processor = children.get('Processor')
            fn = next((c for c in processor.children if c.name == name), None) if processor else None
            require(fn is not None, f'{d.key}.{name}: missing Processor method for oneway detection')
            body = fn.body
            flags = []
            for i in range(len(body)-7):
                if body[i:i+4] == ['isOneway','(',')','{']:
                    flag = body[i+4:closing(body,i+3)]
                    require(flag in (['return','true',';'], ['return','false',';']), 'Unsupported isOneway implementation')
                    flags.append(flag[1] == 'true')
            require(len(flags) == 1, f'{d.key}.{name}: cannot determine oneway')
            oneway = flags[0]
            require(oneway or result_node is not None, f'{d.key}.{name}: missing result class')
            result = recover_fields(result_node) if result_node else []
            success = [f for f in result if f.id == 0]
            exceptions = [f for f in result if f.id != 0]
            require(len(success) <= 1 and (not success or success[0].name == 'success'), 'Invalid result field 0')
            for f in exceptions:
                require(f.type.ref and self.defs[self.resolve(f.type.ref,d)].kind == 'exception', f'{d.key}.{name}: throws field is not an exception')
            require(not oneway or not result, f'{d.key}.{name}: oneway has a result')
            # Verify Java arity and void return; IDs/types come from metadata.
            end = closing(signature,pos)
            require(len(split(signature[pos+1:end])) == len(args), f'{d.key}.{name}: Iface/args arity mismatch')
            require(('void' in signature[:pos-1]) == (not success), f'{d.key}.{name}: Iface/result return mismatch')
            d.methods.append((name,args,success[0].type if success else Type('void'),exceptions,oneway))

    def render(self):
        sections = {f:[] for f in self.files.values()}
        # Definitions of base services must precede derived services in a merged file.
        ordered, active, done = [], set(), set()
        def visit(d):
            require(d.key not in active, 'Cyclic service inheritance')
            if d.key in done: return
            active.add(d.key)
            if d.base: visit(self.defs[d.base])
            active.remove(d.key); done.add(d.key); ordered.append(d)
        for d in sorted(self.defs.values(), key=lambda x:({'enum':0,'struct':1,'exception':1,'union':1,'service':2}[x.kind], x.key)): visit(d)
        for d in ordered:
            lines = [f'// Source: {d.source}']
            if d.kind == 'enum':
                lines += [f'enum {d.name} {{'] + [f'  {n} = {v},' for n,v in d.values] + ['}']
            elif d.kind == 'service':
                base = ' extends ' + self.reference(d.base,d) if d.base else ''
                lines.append(f'service {d.name}{base} {{')
                for n,args,ret,exceptions,oneway in d.methods:
                    s = f'  {"oneway " if oneway else ""}{self.type_text(ret,d)} {n}('
                    s += ', '.join(self.field_text(f,d) for f in args) + ')'
                    if exceptions: s += ' throws (' + ', '.join(self.field_text(f,d,False) for f in exceptions) + ')'
                    lines.append(s + ',')
                lines.append('}')
            else:
                lines += [f'{d.kind} {d.name} {{']
                lines += ['  ' + self.field_text(f,d,d.kind!='union') + ',' for f in d.fields]
                lines.append('}')
            sections[self.files[d.package]].append('\n'.join(lines))
        active, done = set(), set()
        def check(f):
            require(f not in active, 'Cyclic package includes; use --namespace to merge, if names are unique')
            if f in done: return
            active.add(f)
            for dep in self.dependencies[f]: check(dep)
            active.remove(f); done.add(f)
        for f in sections: check(f)
        outputs = {}
        for filename, parts in sections.items():
            pkg = self.namespace if self.namespace is not None else next(p for p,f in self.files.items() if f == filename)
            preamble = ['// Recovered from Java; review report.json before use.', f'// Profile: {self.target}. Original IDL layout/annotations are not recoverable.']
            preamble += [f'include "{dep}.thrift"' for dep in sorted(self.dependencies[filename])]
            if pkg: preamble.append(f'namespace java {pkg}')
            outputs[filename+'.thrift'] = '\n'.join(preamble)+'\n\n'+'\n\n'.join(parts)+'\n'
        return outputs

    def run(self):
        self.scan(); self.parse()
        outputs = self.render()
        report = {'generator_version':VERSION, 'target':self.target, 'source_compiler_versions':sorted(self.versions),
                  'java_files_scanned':self.scanned, 'skipped_java_files':self.skipped,
                  'counts':{k:sum(d.kind==k for d in self.defs.values()) for k in ('enum','struct','exception','union','service')},
                  'methods':sum(len(d.methods) for d in self.defs.values()),
                  'fields':sum(len(d.fields) for d in self.defs.values()),
                  'warnings':self.warnings,
                  'limitations':['Original filenames, includes, comments, non-Java namespaces and annotations are not reconstructed.',
                                 'Typedef names and standalone constants are not reconstructed; detected unsupported forms fail.',
                                 'Default values are inferred from no-argument constructors, not proof of the original IDL.',
                                 'Modified Java serialization code is not analyzed; metadata must match serialization behavior.'],
                  'files':list(outputs), 'validation':{'performed':False}}
        return outputs, report


def validate(directory, files, executable, target):
    version = subprocess.run([executable,'--version'],capture_output=True,text=True,check=True).stdout.strip()
    if target == '0.24.0': require(bool(re.search(r'\b0\.24\.0\b',version)), f'Expected Thrift 0.24.0, found {version}')
    out = directory / 'compiler-check'
    out.mkdir()
    diagnostics = []
    for filename in files:
        cmd = [executable, '-out', str(out), '--gen', 'java', str(directory / filename)]
        p = subprocess.run(cmd,capture_output=True,text=True)
        require(p.returncode == 0, f'Compiler rejected {filename}:\n{p.stdout}{p.stderr}')
        if p.stdout.strip() or p.stderr.strip(): diagnostics.append(p.stdout+p.stderr)
    return {'performed':True, 'compiler':version, 'diagnostics':diagnostics}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('input', type=Path, help='Root directory; scans *.java recursively')
    p.add_argument('-o','--output', type=Path, required=True)
    p.add_argument('--target', choices=['portable','0.24.0'], default='portable', help='IDL compatibility profile; 0.24.0 also pins validation version')
    p.add_argument('--namespace', help='Merge into recovered.thrift with this Java namespace (changes original packages)')
    p.add_argument('--defaults', choices=['strict','omit'], default='strict', help='Fail on complex defaults, or explicitly omit ALL defaults with warnings')
    p.add_argument('--exclude', action='append', default=[], help='Repeatable relative Path.match glob')
    p.add_argument('--validate', action='store_true', help='Compile recovered IDL to Java in a temporary directory; Java is not compiled')
    p.add_argument('--thrift', default='thrift', help='Thrift compiler executable')
    p.add_argument('--force', action='store_true', help='Replace only generated filenames; unrelated files stay untouched')
    p.add_argument('--version', action='version', version=VERSION)
    args = p.parse_args(argv)
    try:
        require(args.input.is_dir(), f'Not a directory: {args.input}')
        outputs, report = Generator(args.input,args.target,args.namespace,args.defaults,args.exclude).run()
        # All parse/resolve/compiler checks finish before output is touched.
        with tempfile.TemporaryDirectory(prefix='thrift-reverse-') as tmp:
            stage = Path(tmp)
            for name, content in outputs.items(): (stage/name).write_text(content,encoding='utf-8')
            if args.validate:
                report['validation'] = validate(stage,outputs,args.thrift,args.target)
            outputs['report.json'] = json.dumps(report,ensure_ascii=False,indent=2)+'\n'
            for name in outputs:
                dest = args.output/name
                require(not dest.is_symlink(), f'Refusing symlink output: {dest}')
                require(args.force or not dest.exists(), f'Output exists: {dest}; choose a new directory or --force')
            args.output.mkdir(parents=True,exist_ok=True)
            for name, content in outputs.items():
                # Atomic replacement per file, not a directory-wide transaction.
                with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=args.output,delete=False) as f:
                    f.write(content)
                    temp = Path(f.name)
                temp.chmod(0o644)
                temp.replace(args.output/name)
        print(json.dumps({'status':'ok', 'output':str(args.output), 'counts':report['counts'], 'warnings':len(report['warnings']), 'validated':args.validate}, ensure_ascii=False))
        for w in report['warnings']: print('WARNING: '+w,file=sys.stderr)
        return 0
    except (RecoveryError, OSError, UnicodeError, subprocess.SubprocessError) as e:
        print(f'ERROR: {e}',file=sys.stderr)
        return 2

if __name__ == '__main__':
    sys.exit(main())
