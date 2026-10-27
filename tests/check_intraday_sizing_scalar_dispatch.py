"""Offline portable checks for the owned technical-sizing exact-builtin scalar dispatch patch.

Uses only Python's standard library. Extracted _clean functions run with explicit
fake pandas/NumPy scalar types, preserving their transform calls. This is scoped
semantic evidence; the package does not import production or contact a service.
--installed-root accepts a sealed generation containing manifest.json and source/.
"""
from __future__ import annotations

import argparse
import ast
from collections import UserDict
from collections.abc import Mapping as ABCMapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import inspect
import textwrap
import typing
import json
import math
from pathlib import Path
import sys
from types import MappingProxyType, SimpleNamespace
from typing import Mapping


TARGET = 'ml/intraday/technical_sizing.py'
PREFIX = 'intraday-sizing-scalar-dispatch'
CHECKS = []


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    CHECKS.append(name)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def function_source(raw):
    text = raw.decode('utf-8')
    functions = [n for n in ast.parse(text).body
                 if isinstance(n, ast.FunctionDef) and n.name == '_clean']
    if len(functions) != 1:
        raise AssertionError('one installed _clean function')
    n = functions[0]
    return '\n'.join(text.splitlines()[n.lineno - 1:n.end_lineno]) + '\n'


def verify_package(root):
    folder = root / 'docs' / 'development' / 'patches'
    meta = json.loads((folder / (PREFIX + '.json')).read_text(encoding='utf-8'))
    patch = (folder / (PREFIX + '.patch')).read_bytes()
    check('metadata schema', meta['schema'] == PREFIX + '-v1')
    check('one exact target', meta['target'] == TARGET)
    check('patch exact digest', sha(patch) == meta['patch_sha256'])
    lines = patch.decode().splitlines()
    check('patch exact headers', lines[:2] == ['--- a/' + TARGET, '+++ b/' + TARGET])
    check('one owned hunk', sum(x.startswith('@@ ') for x in lines) == meta['expected_hunks'] == 1)
    check('no additional target headers', sum(x.startswith('--- ') for x in lines) == sum(x.startswith('+++ ') for x in lines) == 1)
    added = [x[1:] for x in lines if x.startswith('+') and not x.startswith('+++')]
    removed = [x[1:] for x in lines if x.startswith('-') and not x.startswith('---')]
    expected = ['    scalar_type = type(value)', '    if scalar_type is str or scalar_type is int or scalar_type is bool:', '        return value', '    if scalar_type is float:', '        return value if math.isfinite(value) else None']
    check('only five exact additions', added == expected)
    check('no source removals', removed == [])
    before, after = meta['clean_before']['source'], meta['clean_after']['source']
    check('before clean digest', sha(before.encode()) == meta['clean_before']['sha256'])
    check('after clean digest', sha(after.encode()) == meta['clean_after']['sha256'])
    insertion = '\n'.join(expected) + '\n'
    anchor = '    if isinstance(value, (list, tuple)):\n'
    check('exact source insertion', before.count(anchor) == 1 and after == before.replace(anchor, insertion + anchor, 1))
    btree, atree = ast.parse(before), ast.parse(after)
    check('one extracted function each', len(btree.body) == len(atree.body) == 1 and isinstance(btree.body[0], ast.FunctionDef) and isinstance(atree.body[0], ast.FunctionDef))
    check('extract name retained', btree.body[0].name == atree.body[0].name == '_clean')
    check('mapping predicate identical', ast.dump(btree.body[0].body[1], include_attributes=False) == ast.dump(atree.body[0].body[1], include_attributes=False))
    del atree.body[0].body[2:5]
    check('all other clean AST identical', ast.dump(btree, include_attributes=False) == ast.dump(atree, include_attributes=False))
    check('full source hashes recorded', all(len(meta[k]) == 64 and int(meta[k],16) >= 0 for k in ('before_sha256','after_sha256')))
    check('imports retained', meta['imports']['retain'] == ['from collections.abc import Mapping as _Mapping', 'from typing import Mapping, Sequence'])
    check('builtin identity AST uses is only', all(isinstance(op, ast.Is) for node in ast.walk(ast.parse('def test(value):\n' + insertion)) if isinstance(node, ast.Compare) for op in node.ops))
    return meta, before, after


class FakeTimestamp:
    def __init__(self, value):
        self.value = value

    def to_pydatetime(self):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeGeneric:
    def __init__(self, value):
        self.value = value

    def item(self):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class CustomMapping(ABCMapping):
    def __init__(self, value):
        self.value = value

    def __getitem__(self, key):
        return self.value[key]

    def __iter__(self):
        return iter(self.value)

    def __len__(self):
        return len(self.value)


class RegisteredMapping:
    def __init__(self, value):
        self.value = value

    def __getitem__(self, key):
        return self.value[key]

    def __iter__(self):
        return iter(self.value)

    def __len__(self):
        return len(self.value)

    def items(self):
        return self.value.items()


ABCMapping.register(RegisteredMapping)


class DuckUnregistered:
    def items(self):
        raise AssertionError('unregistered duck must remain untouched')


class BadItems(CustomMapping):
    def items(self):
        raise LookupError('retained items failure')


class BadKey:
    def __str__(self):
        raise ValueError('retained key failure')


class SpoofObject:
    def __init__(self):
        self.class_reads = 0

    @property
    def __class__(self):
        self.class_reads += 1
        return dict


class AliasMeta(type):
    @property
    def __class__(cls):
        return type(Mapping)

    @property
    def __origin__(cls):
        return dict


class AliasShaped(metaclass=AliasMeta):
    def items(self):
        return {'alias': 1}.items()


class RaisingMeta(type):
    @property
    def __class__(cls):
        raise RuntimeError('retained metaclass class failure')


class RaisingMetaObject(metaclass=RaisingMeta):
    pass


class PlainMeta(type):
    pass


class PlainMetaObject(metaclass=PlainMeta):
    pass


def predicate(source, value):
    test = ast.parse(source).body[0].body[1].test
    return eval(compile(ast.Expression(body=test), '<portable dispatch predicate>', 'eval'), {'value': value, 'Mapping': Mapping, '_Mapping': ABCMapping})


def run_dispatch_edges(before, after):
    check('ordinary built-in metaclass selects fast path', type(type({})) is type)
    check('custom metaclass selects original fallback', type(type(AliasShaped())) is not type)
    spoof = SpoofObject()
    check('original typing predicate ignores instance class spoof', predicate(before, spoof) is False and spoof.class_reads == 0)
    check('guarded predicate ignores instance class spoof', predicate(after, spoof) is False and spoof.class_reads == 0)
    alias = AliasShaped()
    check('alias-shaped metaclass counterexample retained', predicate(before, alias) is True and predicate(after, alias) is True and issubclass(type(alias), ABCMapping) is False)
    values = []
    for source in (before, after):
        try:
            predicate(source, RaisingMetaObject())
        except BaseException as exc:
            values.append((type(exc), str(exc)))
    check('raising metaclass fallback retained', values == [(RuntimeError, 'retained metaclass class failure')] * 2)


NA, NAT = object(), object()


def instant(value):
    if not isinstance(value, datetime):
        raise TypeError('retained instant transform failure')
    return value


def compile_clean(source):
    namespace = {'Mapping': Mapping, '_Mapping': ABCMapping,
                 'pd': SimpleNamespace(Timestamp=FakeTimestamp, NA=NA, NaT=NAT),
                 'np': SimpleNamespace(generic=FakeGeneric),
                 'datetime': datetime, 'instant': instant, 'math': math}
    exec(compile(ast.parse(source), '<portable extracted _clean>', 'exec'), namespace)
    return namespace['_clean']


def run_semantics(before, after):
    old, new = compile_clean(before), compile_clean(after)
    dt = datetime(2026, 10, 27, 12, 15, tzinfo=timezone.utc)
    marker, duck = object(), DuckUnregistered()
    spoof, plain_meta = SpoofObject(), PlainMetaObject()
    cases = [
        ('spoofed instance class', spoof, spoof), ('plain custom metaclass', plain_meta, plain_meta),
        ('alias-shaped metaclass mapping', AliasShaped(), {'alias': 1}),
        ('none', None, None), ('missing', NA, None), ('not-a-time', NAT, None),
        ('false', False, False), ('true', True, True), ('zero', 0, 0),
        ('negative integer', -11, -11), ('finite float', 1.25, 1.25),
        ('nan', float('nan'), None), ('positive infinity', float('inf'), None),
        ('negative infinity', -float('inf'), None), ('negative zero', -0.0, -0.0),
        ('unicode string', 'é›ªðŸ“Š', 'é›ªðŸ“Š'), ('bytes', b'bytes', b'bytes'),
        ('decimal retained', Decimal('1.25'), Decimal('1.25')),
        ('unknown object retained', marker, marker), ('unregistered duck retained', duck, duck),
        ('empty list', [], []), ('empty tuple', (), []), ('empty dict', {}, {}),
        ('nested sequence', [1, (2, float('nan')), [None, True]], [1, [2, None], [None, True]]),
        ('numeric keys', {10: 'ten', 2: 'two', -1: 'minus'}, {'10': 'ten', '2': 'two', '-1': 'minus'}),
        ('unicode keys', {'é›ª': 1, 'ðŸ“Š': 2}, {'é›ª': 1, 'ðŸ“Š': 2}),
        ('string collision preserves last', {1: 'first', '1': 'last'}, {'1': 'last'}),
        ('null key', {None: None}, {'None': None}), ('bool key', {True: 3}, {'True': 3}),
        ('datetime UTC', dt, dt.isoformat()),
        ('datetime naive', dt.replace(tzinfo=None), dt.replace(tzinfo=None).isoformat()),
        ('datetime offset', dt.astimezone(timezone(timedelta(hours=-7))), dt.astimezone(timezone(timedelta(hours=-7))).isoformat()),
        ('timestamp transform', FakeTimestamp(dt), dt.isoformat()),
        ('generic int', FakeGeneric(3), 3), ('generic missing', FakeGeneric(NA), None),
        ('generic list', FakeGeneric((1, float('inf'))), [1, None]),
        ('generic timestamp', FakeGeneric(FakeTimestamp(dt)), dt.isoformat()),
        ('nested generic', FakeGeneric(FakeGeneric({'x': float('nan')})), {'x': None}),
    ]
    for label, maker in [('dict', dict), ('UserDict', UserDict), ('proxy', MappingProxyType),
                         ('custom ABC', CustomMapping), ('registered ABC', RegisteredMapping)]:
        value = maker({10: (FakeGeneric(1), NA), 'é›ª': {'nested': FakeTimestamp(dt)}})
        expected = {'10': [1, None], 'é›ª': {'nested': dt.isoformat()}}
        cases.append((label + ' nested mapping', value, expected))
        cases.append((label + ' empty mapping', maker({}), {}))
        check(label + ' isinstance equivalence', isinstance(value, Mapping) == isinstance(value, ABCMapping) is True)
    for name, value, expected in cases:
        b, a = old(value), new(value)
        check('semantics: ' + name, b == expected and a == expected and b == a)
    check('mapping order exact', list(new({10: 1, 2: 2, 'é›ª': 3})) == ['10', '2', 'é›ª'])
    errors = [
        ('metaclass error', RaisingMetaObject(), RuntimeError, 'retained metaclass class failure'),
        ('items error', BadItems({}), LookupError, 'retained items failure'),
        ('key error', {BadKey(): 1}, ValueError, 'retained key failure'),
        ('timestamp method error', FakeTimestamp(RuntimeError('retained timestamp failure')), RuntimeError, 'retained timestamp failure'),
        ('generic method error', FakeGeneric(ArithmeticError('retained scalar failure')), ArithmeticError, 'retained scalar failure'),
        ('instant transform error', FakeTimestamp('invalid'), TypeError, 'retained instant transform failure'),
        ('nested items error', {'x': [BadItems({})]}, LookupError, 'retained items failure'),
    ]
    for name, value, error_type, message in errors:
        observed = []
        for fn in (old, new):
            try:
                fn(value)
            except BaseException as exc:
                observed.append((type(exc), str(exc)))
        check('errors: ' + name, observed == [(error_type, message), (error_type, message)])
    cyclic = []; cyclic.append(cyclic)
    failures = []
    for fn in (old, new):
        try:
            fn(cyclic)
        except RecursionError:
            failures.append(True)
    check('cyclic input still rejects with RecursionError', failures == [True, True])
    return len(cases), len(errors) + 1


def verify_installed(root, meta, after):
    manifest_raw = (root / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_raw)
    check('installed manifest content address', sha(manifest_raw) == root.name)
    check('installed manifest exact target digest', manifest['source_files'][TARGET] == meta['after_sha256'])
    raw = (root / 'source' / TARGET).read_bytes()
    check('installed full source exact bytes', sha(raw) == meta['after_sha256'] and len(raw) == meta['after_bytes'])
    check('installed extracted clean exact source', function_source(raw) == after)
    tree = ast.parse(raw.decode())
    imports = [ast.unparse(n) for n in tree.body if isinstance(n, ast.ImportFrom)]
    for line in meta['imports']['retain']:
        check('installed retained import ' + line, imports.count(line) == 1)



class EqualMeta(type):
    equal_calls = 0
    def __eq__(cls, other):
        EqualMeta.equal_calls += 1
        return True
    __hash__ = type.__hash__


class RecursiveList(list, metaclass=EqualMeta):
    pass


class StringSubclass(str):
    pass


class IntegerSubclass(int):
    pass


class FloatSubclass(float):
    pass


def run_scalar_edges(before, after):
    old, new = compile_clean(before), compile_clean(after)
    for value in [StringSubclass('text'), IntegerSubclass(8), FloatSubclass(1.25), FloatSubclass(float('nan')), FloatSubclass(float('inf'))]:
        observed = [fn(value) for fn in (old,new)]
        check('scalar subclass ' + repr(value), observed[0] == observed[1] and (type(observed[0]) is type(observed[1])))
    values = [RecursiveList([NA, (1, float('inf'))]), RecursiveList([FakeGeneric(3)])]
    for index, value in enumerate(values):
        left, right = old(value), new(value)
        check('custom equality list ' + str(index), left == right and type(right) is list)
    # Registration is global to this isolated offline process and happens last.
    for scalar, value in [(str,'text'),(int,8),(bool,True),(float,1.25)]:
        ABCMapping.register(scalar)
        errors=[]
        for fn in (old,new):
            try:
                fn(value)
            except BaseException as exc:
                errors.append((type(exc),str(exc)))
        check('registered builtin mapping ' + scalar.__name__, len(errors)==2 and errors[0]==errors[1] and errors[0][0] is AttributeError)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed-root', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    meta, before, after = verify_package(root)
    run_dispatch_edges(before, after)
    case_count, error_count = run_semantics(before, after)
    run_scalar_edges(before, after)
    if args.installed_root:
        verify_installed(args.installed_root.resolve(), meta, after)
    print(json.dumps({'passed': len(CHECKS), 'failed': 0,
                      'semantic_cases': case_count, 'error_cases': error_count,
                      'installed_static_checks': 6 if args.installed_root else 0,
                      'network_attempts': 0, 'production_imports': 0,
                      'scope': 'Pure standard-library extracted _clean scalar dispatch semantics and owned patch/AST; optional installed bytes/manifest only. No whole sizing or live recovery claim.'}))


if __name__ == '__main__':
    main()
