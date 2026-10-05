"""Conservative static binding: visible definitions/imports, never name guessing."""

from collections import defaultdict

from code_context.intelligence_models import (
    CALLABLE_KINDS,
    CLASS_KINDS,
    ParsedFile,
    Reference,
    Relation,
    Symbol,
    module_id,
)
from code_context.intelligence_roots import PythonSourceRoots, python_modules, select_source_roots


class Resolver:
    def __init__(self, files: dict[str, ParsedFile], roots: PythonSourceRoots | None = None):
        self.files = files
        self.python_roots = select_source_roots(files, roots or PythonSourceRoots())
        self.python_source_roots = self.python_roots.roots or ()
        self.modules: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.symbols: dict[tuple[str, str], list[Symbol]] = defaultdict(list)
        self.named: dict[tuple[str, str, str], list[Symbol]] = defaultdict(list)
        self.bindings = defaultdict(list)
        self.imports = defaultdict(list)
        self.scope_maps = {}
        self.by_id = {}
        self.roots = set()
        for path, parsed in files.items():
            self.scope_maps[path] = {scope.qualname: scope for scope in parsed.scopes}
            for binding in parsed.bindings:
                self.bindings[path, binding.scope, binding.name].append(binding)
            for binding in parsed.imports:
                self.imports[path, binding.scope, binding.local_name].append(binding)
            names = (
                python_modules(path, self.python_source_roots)
                if parsed.language == "python"
                else [parsed.module]
            )
            for name in names:
                if name:
                    self.modules[parsed.language, name].append(path)
                    self.roots.add((parsed.language, name.split(".")[0]))
            for symbol in parsed.symbols:
                self.symbols[path, symbol.qualname].append(symbol)
                self.named[path, symbol.scope, symbol.name].append(symbol)
                self.by_id[symbol.id] = symbol
                if parsed.language == "java" and symbol.kind in CLASS_KINDS:
                    full_name = ".".join(filter(None, [parsed.module, symbol.qualname]))
                    self.modules["java", full_name].append(path)
                    self.roots.add(("java", full_name.split(".")[0]))

    def topology(self) -> tuple:
        """An export/import change invalidates bindings, not unchanged parse trees."""
        return self.python_source_roots, tuple(
            (
                path,
                parsed.module,
                parsed.status,
                tuple(
                    (s.name, s.qualname, s.scope, s.kind, s.start_line, tuple(s.parameters))
                    for s in parsed.symbols
                ),
                tuple(
                    (
                        i.module,
                        i.imported_name,
                        i.local_name,
                        i.level,
                        i.scope,
                        i.type_only,
                        i.conditional,
                        i.is_static,
                    )
                    for i in parsed.imports
                ),
                tuple((b.name, b.scope, b.kind, b.annotation, b.value) for b in parsed.bindings),
            )
            for path, parsed in sorted(self.files.items())
        )

    def _module_paths(self, language: str, name: str) -> list[str]:
        return sorted(set(self.modules.get((language, name), [])))

    @staticmethod
    def _unknown(name: str, evidence: str = "no_static_binding") -> dict:
        return {"target_module": name, "resolution": "unresolved", "evidence": evidence}

    def _select(self, candidates: list[Symbol], evidence: str, arity: int | None = None) -> dict:
        if arity is not None and len(candidates) > 1:
            candidates = [s for s in candidates if len(s.parameters) == arity]
        if len(candidates) != 1:
            return {
                "resolution": "ambiguous" if candidates else "unresolved",
                "evidence": "multiple_static_targets" if candidates else "no_static_binding",
            }
        symbol = candidates[0]
        return {
            "target_path": symbol.path,
            "target_symbol_id": symbol.id,
            "target_qualname": symbol.qualname,
            "resolution": "resolved",
            "evidence": evidence,
        }

    def _module_target(self, language: str, name: str) -> dict:
        paths = self._module_paths(language, name)
        if len(paths) == 1:
            return {
                "target_path": paths[0],
                "target_symbol_id": module_id(paths[0]),
                "target_qualname": "",
                "target_module": name,
                "resolution": "resolved",
                "evidence": "module_import",
            }
        if paths:
            return {
                "target_module": name,
                "resolution": "ambiguous",
                "evidence": "multiple_module_roots",
            }
        local = (language, name.split(".")[0]) in self.roots
        # A missing import may be an ignored local file, an installed library or a typo.
        # Without compiler/environment access those cases must not be invented.
        return {
            "target_module": name,
            "resolution": "unavailable" if local else "external_or_unavailable",
            "evidence": "module_not_in_authorized_mirror",
        }

    def _import_module(self, parsed: ParsedFile, imp) -> str:
        if parsed.language != "python" or not imp.level:
            return imp.module
        names = python_modules(parsed.path, self.python_source_roots)
        name = names[-1] if names else ""
        package = name.split(".")
        if not parsed.path.endswith("/__init__.py") and parsed.path != "__init__.py":
            package = package[:-1]
        if imp.level > len(package):
            return ""
        prefix = package[: len(package) - imp.level + 1]
        return ".".join([*prefix, *([imp.module] if imp.module else [])])

    def _import_target(
        self,
        parsed: ParsedFile,
        imp,
        suffix: list[str],
        arity: int | None,
        trail: frozenset = frozenset(),
    ) -> dict:
        name = self._import_module(parsed, imp)
        if not name:
            return self._unknown(imp.module, "invalid_relative_import")
        if imp.imported_name == "*":
            return self._unknown(name, "wildcard_import")
        wanted = [imp.imported_name] if imp.imported_name else []
        wanted += suffix
        # `import a.b` binds `a`; `import a.b as x` binds the complete module.
        if parsed.language == "python" and not imp.imported_name:
            if imp.local_name == name.split(".")[0] and suffix:
                name = ".".join([imp.local_name, *suffix])
                wanted = []
                while not self._module_paths("python", name) and "." in name:
                    name, tail = name.rsplit(".", 1)
                    wanted.insert(0, tail)
        if parsed.language == "java" and imp.is_static:
            full_class = self._module_paths("java", name)
            if len(full_class) == 1:
                class_name = name.removeprefix(self.files[full_class[0]].module + ".")
                return self._export(full_class[0], [class_name, *wanted], arity, trail)
        if parsed.language == "java" and wanted:
            qualified = name + "." + wanted[0]
            full_class = self._module_paths("java", qualified)
            if len(full_class) == 1:
                class_name = qualified.removeprefix(self.files[full_class[0]].module + ".")
                return self._export(full_class[0], [class_name, *wanted[1:]], arity, trail)
        paths = self._module_paths(parsed.language, name)
        if wanted:
            submodule = ".".join([name, *wanted])
            subpaths = self._module_paths(parsed.language, submodule)
            if len(subpaths) == 1 and parsed.language == "python":
                return self._module_target(parsed.language, submodule)
        if len(paths) == 1:
            if not wanted:
                return self._module_target(parsed.language, name)
            return self._export(paths[0], wanted, arity, trail)
        if len(paths) > 1:
            return {
                "target_module": name,
                "resolution": "ambiguous",
                "evidence": "multiple_module_roots",
            }
        return self._module_target(parsed.language, ".".join([name, *wanted]))

    def _export(
        self, path: str, names: list[str], arity: int | None, trail: frozenset = frozenset()
    ) -> dict:
        key = (path, ".".join(names))
        if key in trail or len(trail) >= 8:
            return self._unknown(key[1], "cyclic_or_deep_reexport")
        candidates = self.symbols.get(key, [])
        if not candidates:
            parents = ".".join(names[:-1])
            candidates = self.named.get((path, parents, names[-1]), [])
        if candidates:
            return self._select(candidates, "visible_definition", arity)
        parsed = self.files[path]
        imports = [i for i in parsed.imports if not i.scope and i.local_name == names[0]]
        if imports:
            if len(imports) != 1 or imports[0].conditional:
                return {"resolution": "ambiguous", "evidence": "conditional_reexport_binding"}
            return self._import_target(parsed, imports[0], names[1:], arity, trail | {key})
        return {
            "target_path": path,
            "resolution": "unresolved",
            "evidence": "export_not_statically_available",
        }

    def _scopes(self, parsed: ParsedFile, scope: str) -> list[str]:
        scopes = self.scope_maps[parsed.path]
        chain, seen = [], set()
        while scope not in seen:
            seen.add(scope)
            chain.append(scope)
            current = scopes.get(scope)
            if not scope:
                break
            scope = current.parent if current else scope.rpartition(".")[0]
            # Python methods do not implicitly close over the class namespace.
            if parsed.language == "python" and current and current.kind in {"function", "method"}:
                while scope and scopes.get(scope) and scopes[scope].kind == "class":
                    scope = scopes[scope].parent
        return chain

    def _binding_target(self, parsed: ParsedFile, ref: Reference, depth: int = 0) -> dict:
        if depth >= 5:
            return self._unknown(ref.name, "dynamic_or_deep_binding")
        parts = ref.name.split(".")
        if not parts or not all(part.isidentifier() for part in parts):
            return self._unknown(ref.name, "dynamic_expression")
        base, suffix = parts[0], parts[1:]
        # Explicit self/cls/this qualifies a member, unlike an arbitrary object's name.
        if base in {"self", "cls", "this"} and suffix:
            if parsed.language == "python":
                receiver = []
                for scope in self._scopes(parsed, ref.scope):
                    receiver = self.bindings[parsed.path, scope, base]
                    if receiver:
                        break
                methods = self.symbols.get((parsed.path, receiver[0].scope), []) if receiver else []
                if (
                    not receiver
                    or any(b.kind != "parameter" for b in receiver)
                    or len(methods) != 1
                    or methods[0].kind != "method"
                    or not methods[0].parameters
                    or methods[0].parameters[0] != base
                    or any(
                        r.kind == "DECORATOR"
                        and r.source_qualname == methods[0].qualname
                        and r.name.rsplit(".", 1)[-1] == "staticmethod"
                        for r in parsed.references
                    )
                ):
                    return self._unknown(ref.name, "receiver_rebound_or_not_method_parameter")
            owners = [
                s
                for s in parsed.symbols
                if s.kind in CLASS_KINDS
                and (
                    ref.source_qualname.startswith(s.qualname + ".")
                    or ref.source_qualname == s.qualname
                )
            ]
            if owners:
                owner = max(owners, key=lambda s: len(s.qualname))
                target = self._export(parsed.path, [owner.qualname, *suffix], ref.arity)
                if target.get("resolution") == "resolved":
                    target["evidence"] = "explicit_receiver_static_member"
                return target
        chain = self._scopes(parsed, ref.scope)
        scope_map = self.scope_maps[parsed.path]
        current = scope_map.get(ref.scope)
        if current and base in current.globals:
            chain = [""]
        elif current and base in current.nonlocals:
            chain = [scope for scope in chain[1:] if scope]
        for scope in chain:
            bindings = self.bindings[parsed.path, scope, base]
            imports = self.imports[parsed.path, scope, base]
            definitions = self.named.get((parsed.path, scope, base), [])
            if bindings:
                other = [b for b in bindings if b.kind not in {"symbol", "import"}]
                if other:
                    annotations = {b.annotation or b.value for b in other}
                    if suffix and len(annotations) == 1 and all(b.annotation for b in other):
                        type_name = next(iter(annotations)).split("[")[0].split("<")[0]
                        typed = Reference(
                            type_name + "." + ".".join(suffix),
                            ref.kind,
                            ref.line,
                            scope,
                            ref.source_qualname,
                            arity=ref.arity,
                        )
                        result = self._binding_target(parsed, typed, depth + 1)
                        if result.get("resolution") == "resolved":
                            result["evidence"] = "declared_type_possible_dispatch"
                        return result
                    return self._unknown(ref.name, "local_or_parameter_shadowing")
            if imports:
                if len(imports) != 1 or (
                    imports[0].conditional and not (imports[0].type_only and ref.type_only)
                ):
                    return {"resolution": "ambiguous", "evidence": "conditional_import_binding"}
                return self._import_target(parsed, imports[0], suffix, ref.arity)
            if definitions:
                if suffix:
                    if len(definitions) == 1 and definitions[0].kind in CLASS_KINDS:
                        return self._export(
                            parsed.path, [definitions[0].qualname, *suffix], ref.arity
                        )
                    return self._unknown(ref.name, "dynamic_attribute")
                return self._select(definitions, "lexical_definition", ref.arity)
        if parsed.language == "java":
            paths = self._module_paths("java", ".".join(filter(None, [parsed.module, base])))
            if len(paths) == 1:
                return self._export(paths[0], [base, *suffix], ref.arity)
            # Fully qualified Java types need no explicit import.
            for stop in range(len(parts), 0, -1):
                qualified = ".".join(parts[:stop])
                paths = self._module_paths("java", qualified)
                if len(paths) == 1:
                    class_name = qualified.removeprefix(self.files[paths[0]].module + ".")
                    return self._export(paths[0], [class_name, *parts[stop:]], ref.arity)
            wildcard = [i for i in parsed.imports if i.imported_name == "*"]
            matches = []
            for imp in wildcard:
                candidate = self._module_paths("java", imp.module + "." + base)
                matches += candidate
            if len(set(matches)) == 1:
                return self._export(matches[0], [base, *suffix], ref.arity)
            if matches or wildcard:
                return self._unknown(ref.name, "wildcard_import_or_ambiguous_target")
        return self._unknown(ref.name, "dynamic_receiver" if suffix else "no_static_binding")

    def resolve_file(self, path: str) -> list[Relation]:
        parsed = self.files[path]
        result: list[Relation] = []
        for symbol in parsed.symbols:
            if not symbol.qualname:
                continue
            owner = self.symbols.get((path, symbol.scope), [])
            result.append(
                Relation(
                    path,
                    "CONTAINS",
                    symbol.name,
                    symbol.start_line,
                    source_symbol_id=owner[0].id if len(owner) == 1 else module_id(path),
                    source_qualname=symbol.scope,
                    target_path=path,
                    target_symbol_id=symbol.id,
                    target_qualname=symbol.qualname,
                    resolution="resolved",
                    evidence="syntax_definition",
                )
            )
        for ref in parsed.references:
            if ref.kind == "IMPORT":
                imports = [i for i in parsed.imports if i.line == ref.line]
                import_name = ref.name.lstrip(".")
                matches = [
                    i
                    for i in imports
                    if import_name == ".".join(filter(None, [i.module, i.imported_name]))
                ]
                if not matches:
                    matches = [i for i in imports if import_name == i.local_name]
                if not matches:
                    matches = [i for i in imports if import_name == i.module]
                if len(matches) == 1:
                    imp = matches[0]
                    target = self._import_target(parsed, imp, [], None)
                    # Wildcards still establish a module dependency, not symbol bindings.
                    if imp.imported_name == "*":
                        target = self._module_target(
                            parsed.language, self._import_module(parsed, imp)
                        )
                        target["evidence"] = "wildcard_module_import"
                else:
                    target = self._unknown(ref.name, "import_binding_not_unique")
            else:
                target = self._binding_target(parsed, ref)
            owner = self.symbols.get((path, ref.source_qualname), [])
            kind = ref.kind
            if kind == "CALL" and target.get("target_symbol_id"):
                matched = self.by_id.get(target["target_symbol_id"])
                if matched and matched.kind in CLASS_KINDS:
                    kind = "INSTANTIATION"
                elif matched and matched.kind not in CALLABLE_KINDS:
                    target = self._unknown(ref.name, "target_is_not_callable")
            result.append(
                Relation(
                    path,
                    kind,
                    ref.name,
                    ref.line,
                    source_symbol_id=owner[0].id if len(owner) == 1 else module_id(path),
                    source_qualname=ref.source_qualname,
                    column=ref.column,
                    type_only=ref.type_only,
                    conditional=ref.conditional,
                    **target,
                )
            )
        return result
