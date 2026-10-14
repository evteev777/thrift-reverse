#!/usr/bin/env python3
"""Generate Java sources from Thrift IDL using Apache Thrift 0.24.0."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class GenerationError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise GenerationError(message)


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    require(result.returncode == 0, f'Compiler failed:\n{result.stdout}{result.stderr}')
    return result.stdout, result.stderr


def generate(source, output, compiler='thrift', includes=(), options='generated_annotations=undated', force=False):
    require(source.exists(), f'Input does not exist: {source}')
    if source.is_dir():
        files = sorted(p.resolve() for p in source.rglob('*.thrift') if not p.is_symlink())
        root = source.resolve()
    else:
        require(source.is_file() and source.suffix == '.thrift', 'Input must be a .thrift file or directory')
        files, root = [source.resolve()], source.resolve().parent
    require(files, f'No .thrift files found in {source}')
    for path in includes:
        require(path.is_dir(), f'Include directory does not exist: {path}')
    stdout, stderr = run([compiler, '--version'])
    version = (stdout + stderr).strip()
    require(re.fullmatch(r'Thrift version 0\.24\.0', version), f'Expected Thrift 0.24.0, found {version!r}')
    generator = 'java' + (':' + options if options else '')
    include_args = ['-I', str(root)]
    for path in includes:
        include_args += ['-I', str(path.resolve())]
    report = {'compiler': version, 'generator': generator, 'inputs': [str(p.relative_to(root)) for p in files],
              'diagnostics': [], 'files': []}
    # Isolated output per entry prevents silently overwriting colliding Java classes.
    with tempfile.TemporaryDirectory(prefix='thrift-java-') as tmp:
        staged, owners = {}, {}
        for index, file in enumerate(files):
            stage = Path(tmp) / str(index)
            stage.mkdir()
            stdout, stderr = run([compiler, *include_args, '-r', '-out', str(stage), '--gen', generator, str(file)])
            if stdout.strip() or stderr.strip():
                report['diagnostics'].append({'input': str(file.relative_to(root)), 'message': stdout + stderr})
            for java in sorted(stage.rglob('*.java')):
                relative = java.relative_to(stage)
                content = java.read_bytes()
                require(relative not in staged or staged[relative].read_bytes() == content,
                        f'Conflicting generated class {relative}: {owners.get(relative)} and {file}')
                staged[relative], owners[relative] = java, file
        report['files'] = sorted(str(p) for p in staged)
        require(staged, 'Compiler produced no Java sources')
        report_path = Path(tmp) / 'java-report.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        staged[Path('java-report.json')] = report_path
        # Complete preflight before writing any output, including existing ancestor symlinks.
        for relative in staged:
            destination = output.absolute() / relative
            require(not any(p.is_symlink() for p in (destination, *destination.parents)),
                    f'Refusing symlink output: {destination}')
            require(not destination.exists() or (force and destination.is_file()),
                    f'Output exists: {destination}; use a new directory or --force')
            require(all(not p.exists() or p.is_dir() for p in destination.parents),
                    f'Output parent is not a directory: {destination}')
        for relative, staged_file in staged.items():
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as f:
                temp = Path(f.name)
            try:
                temp.write_bytes(staged_file.read_bytes())
                temp.chmod(0o644)
                temp.replace(destination)
            finally:
                temp.unlink(missing_ok=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='.thrift file or directory scanned recursively')
    parser.add_argument('-o', '--output', required=True, type=Path)
    parser.add_argument('-I', '--include', action='append', default=[], type=Path, help='Additional include directory; repeatable')
    parser.add_argument('--thrift', default='thrift', help='Path to compiler 0.24.0')
    parser.add_argument('--java-options', default='generated_annotations=undated', help='Compiler Java options, comma-separated; replaces the default')
    parser.add_argument('--force', action='store_true', help='Replace generated files; does not delete stale or unrelated files')
    args = parser.parse_args(argv)
    try:
        report = generate(args.input, args.output, args.thrift, args.include, args.java_options, args.force)
        print(json.dumps({'status': 'ok', 'compiler': report['compiler'], 'input_files': len(report['inputs']),
                          'java_files': len(report['files']), 'output': str(args.output)}, ensure_ascii=False))
        for diagnostic in report['diagnostics']:
            print(diagnostic['message'], file=sys.stderr, end='\n')
        return 0
    except (GenerationError, OSError, UnicodeError, subprocess.SubprocessError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
