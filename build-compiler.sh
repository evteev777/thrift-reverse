#!/bin/sh
# Build only the Java backend needed for IDL validation, from the official release.
set -eu
version=0.24.0
sha256=e0fa5839a4c5c1d631b0931cf2c554ebbfa4e2fee3a9fb3ffd4f82ce4396c6e4
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT
curl -fsSL --retry 3 "https://archive.apache.org/dist/thrift/$version/thrift-$version.tar.gz" -o "$build_dir/thrift.tar.gz"
printf '%s  %s\n' "$sha256" "$build_dir/thrift.tar.gz" | sha256sum -c -
tar --no-same-owner -xzf "$build_dir/thrift.tar.gz" -C "$build_dir"
src="$build_dir/thrift-$version/compiler/cpp/src"
g++ -std=c++17 -O1 -I"$src" \
    "$src/thrift/main.cc" "$src/thrift/common.cc" \
    "$src/thrift/thrifty.cc" "$src/thrift/thriftl.cc" \
    "$src/thrift/audit/t_audit.cpp" \
    "$src/thrift/parse/t_typedef.cc" "$src/thrift/parse/parse.cc" \
    "$src/thrift/generate/t_generator.cc" \
    "$src/thrift/generate/validator_parser.cc" \
    "$src/thrift/generate/t_java_generator.cc" \
    -o "${1:-/usr/local/bin/thrift}"
"${1:-/usr/local/bin/thrift}" --version
