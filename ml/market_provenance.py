"""Private, prospective evidence of executable code and immutable input bytes.

Disk hashes are deliberately separate from loaded Python code. This is evidence,
never an operational gate and never an attestation for an older process/run.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import types


VERSION = "market-input-runtime-provenance-v3"
CODE_HASH_ALGORITHM = "python-code-structure-json-v1"


def preserve_bytes(root, value: bytes, *, suffix=".bin"):
    """Content-addressed, create-only storage; callers supply private roots."""
    digest = hashlib.sha256(value).hexdigest()
    path = Path(root) / digest[:2] / (digest + suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(value)
    except FileExistsError:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Immutable provenance object does not match its name")
    return {"sha256": digest, "bytes": len(value), "path": str(path.resolve())}


def _codes(code):
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield value
            yield from _codes(value)


def _loaded_codes(module):
    """Key by live bindings, never by colliding generated co_qualname values."""
    result = {}
    def function(binding, value):
        seen = set()
        while value is not None and id(value) not in seen:
            seen.add(id(value))
            code = value.__code__ if inspect.isfunction(value) else None
            if code is not None or hasattr(value, "__wrapped__"):
                result[binding] = code
            value = getattr(value, "__wrapped__", None)
            binding += ".__wrapped__"
    def members(binding, cls, parents):
        if id(cls) in parents:
            return
        for name, member in vars(cls).copy().items():
            key = binding + "." + name
            if isinstance(member, (staticmethod, classmethod)):
                member = member.__func__
            if isinstance(member, property):
                for accessor in ("fget", "fset", "fdel"):
                    function(key + "." + accessor, getattr(member, accessor))
            elif inspect.isclass(member) and member.__module__ == module.__name__:
                members(key, member, parents | {id(cls)})
            else:
                function(key, member)
    for name, value in vars(module).copy().items():
        if inspect.isclass(value) and value.__module__ == module.__name__:
            members(name, value, set())
        else:
            function(name, value)
    return result


def _source_bindings(raw, compiled):
    variants = {}
    for code in _codes(compiled):
        variants.setdefault(code.co_qualname, []).append(code)
    bindings = {}
    def visit(node, prefix=""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = prefix + node.name
            binding = qualified
            decorators = []
            for decorator in node.decorator_list:
                expression = decorator.func if isinstance(decorator, ast.Call) else decorator
                spelling = ast.unparse(expression)
                if spelling == "property":
                    binding += ".fget"
                elif isinstance(expression, ast.Attribute) and expression.attr in {"getter", "setter", "deleter"}:
                    binding += {"getter": ".fget", "setter": ".fset", "deleter": ".fdel"}[expression.attr]
                elif spelling not in {"staticmethod", "classmethod"}:
                    decorators.append(spelling)
            first_line = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            description = bindings.setdefault(binding, {"codes": [], "decorators": set()})
            description["codes"].extend(code for code in variants.get(qualified, ()) if code.co_firstlineno == first_line)
            description["decorators"].update(decorators)
            return  # Nested function bodies are not module/class bindings.
        if isinstance(node, ast.ClassDef):
            prefix += node.name + "."
        for child in ast.iter_child_nodes(node):
            visit(child, prefix)
    visit(ast.parse(raw))
    return bindings, variants


def _same_code(candidate, loaded):
    def filenames(code, filename):
        return code.replace(co_filename=filename, co_consts=tuple(
            filenames(value, filename) if isinstance(value, types.CodeType) else value for value in code.co_consts))
    return filenames(candidate, loaded.co_filename) == loaded


def _compare_bindings(loaded, source, variants, source_filename):
    matched, uncomparable = {}, {}
    for binding, code in loaded.items():
        owner = binding.split(".__wrapped__", 1)[0]
        declared = source.get(owner)
        if code is None:
            uncomparable[binding] = "non_python_decorator_wrapper"
        elif declared is None:
            original_name = owner.removesuffix(".fget").removesuffix(".fset").removesuffix(".fdel")
            if code.co_filename == source_filename and code.co_qualname == original_name:
                # A removed/renamed source function is a real disk mismatch,
                # not a newly generated function that should be ignored.
                matched[binding] = False
            else:
                uncomparable[binding] = "generated_dynamic_or_imported_binding_absent_from_source"
        elif any(_same_code(candidate, code) for candidate in declared["codes"]):
            matched[binding] = True
        elif declared["decorators"] and variants.get(code.co_qualname) and any(code.co_qualname.startswith(name + ".<locals>.")
                                               for name in declared["decorators"]):
            # A locally defined decorator's executed wrapper is separately
            # comparable; __wrapped__ retains the decorated function body.
            matched[binding] = any(_same_code(candidate, code) for candidate in variants.get(code.co_qualname, ()))
        elif (binding + ".__wrapped__" in loaded and declared["codes"]
              and code.co_filename != declared["codes"][0].co_filename):
            # A base class can wrap methods at class creation without a
            # decorator in this source (for example sklearn set_output).
            # Keep the executed wrapper hash; compare its original below.
            uncomparable[binding] = "external_decorator_wrapper; wrapped_source_compared_separately"
        else:
            # In particular, replacing a declared function with another module's
            # function must not disappear as an imported/generated binding.
            matched[binding] = False
    return matched, uncomparable


def _code_structure(value):
    """Encode code/constants without marshal reference or interning flags.

    Only immutable compiler constants are supported. Types remain distinct,
    floating-point values use hexadecimal notation, and unordered constants are
    sorted by their own canonical typed encoding, never by an object repr.
    """
    if isinstance(value, types.CodeType):
        fields = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount",
                  "co_nlocals", "co_stacksize", "co_flags", "co_code", "co_consts",
                  "co_names", "co_varnames", "co_filename", "co_name", "co_qualname",
                  "co_firstlineno", "co_linetable", "co_exceptiontable",
                  "co_freevars", "co_cellvars")
        return ["code", {name: _code_structure(getattr(value, name)) for name in fields}]
    if value is None:
        return ["none"]
    if value is Ellipsis:
        return ["ellipsis"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is float:
        return ["float", value.hex()]
    if type(value) is complex:
        return ["complex", value.real.hex(), value.imag.hex()]
    if type(value) is str:
        return ["str", value]
    if type(value) is bytes:
        return ["bytes", value.hex()]
    if type(value) is tuple:
        return ["tuple", [_code_structure(item) for item in value]]
    if type(value) is frozenset:
        members = [_code_structure(item) for item in value]
        members.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return ["frozenset", members]
    raise TypeError("Unsupported runtime code constant type")


def _code_hash(code):
    value = {"algorithm": CODE_HASH_ALGORITHM, "code": _code_structure(code)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


def capture_runtime(module_names, *, object_root=None):
    """Fingerprint actual loaded function/method bytecode without executing disk.

    A successful comparison covers only source-comparable bindings. Generated,
    dynamic, imported and external decorator functions retain distinct hashes
    but are explicitly uncomparable, not asserted to match source. Globals and
    native libraries are outside scope; callers freeze configuration separately.
    Hash algorithm v1 uses deterministic typed code fields/constants, not
    marshal reference/interning flags. V2 receipts are never relabelled.
    Missing/unreadable modules are recorded, never imported.
    """
    modules = {}
    for name in sorted(set(module_names)):
        module = sys.modules.get(name)
        if module is None:
            modules[name] = {"status": "not_loaded"}
            continue
        loaded = _loaded_codes(module)
        entry = {"status": "captured", "loaded_functions": {
            key: _code_hash(code) for key, code in sorted(loaded.items()) if code is not None},
            "loaded_function_hash_algorithm": CODE_HASH_ALGORITHM,
            "loaded_function_key_basis": "module/class member binding; property accessors and __wrapped__ chains distinct",
            "comparison_scope": "source-comparable bindings only; uncomparable_functions are not asserted to match"}
        try:
            path = Path(inspect.getsourcefile(module) or "")
            raw = path.read_bytes()
            entry["disk_sha256_at_capture"] = hashlib.sha256(raw).hexdigest()
            compiled = compile(raw, str(path), "exec", dont_inherit=True, optimize=sys.flags.optimize)
            source, variants = _source_bindings(raw, compiled)
            matched, uncomparable = _compare_bindings(loaded, source, variants, str(path))
            entry["source_comparable_functions"] = sorted(matched)
            entry["uncomparable_functions"] = uncomparable
            entry["loaded_functions_match_disk"] = all(matched.values()) if matched else None
            entry["unmatched_functions"] = [key for key, value in matched.items() if not value]
            if object_root is not None:
                entry["source_object"] = preserve_bytes(object_root, raw, suffix=".py")
        except (OSError, TypeError, SyntaxError, ValueError) as exc:
            entry["disk_capture_error"] = type(exc).__name__
            entry["loaded_functions_match_disk"] = None
        modules[name] = entry
    value = {"schema_version": VERSION, "captured_at": datetime.now(timezone.utc).isoformat(),
             "pid": os.getpid(), "python": sys.version.split()[0], "modules": modules,
             "scope": "actual_loaded_python_functions_and_methods; configuration_separately_frozen",
             "historical_receipts_recovered": False}
    value["sha256"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value
