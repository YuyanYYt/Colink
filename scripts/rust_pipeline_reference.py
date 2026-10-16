"""Private Python reference for the complete fixed-snapshot Rust experiment.

This is a developer experiment, never a production backend. Parsing and binding
are common preparation; each request owns its protocol, graph, source guards,
analysis, bounded response and serialization. It does not emulate the registry,
write coordinator, ignore policy or an atomic filesystem snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import stat
import sys
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from code_context.intelligence_queries import _components, bound_result

MAX_REQUEST = 64 * 1024 * 1024
MAX_FILES = 100_000
MAX_EDGES = 1_000_000
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
ARRAY_ORDER = ("files", "edges", "cycles")


class Rejected(ValueError):
    """Only fixed, non-sensitive codes may leave the experiment process."""


def require(condition, code="invalid_snapshot"):
    if not condition:
        raise Rejected(code)


def relative_path(value):
    require(isinstance(value, str) and 0 < len(value.encode()) <= 4096, "invalid_path")
    require(
        not value.startswith("/")
        and "\\" not in value
        and ":" not in value
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
        and all(part not in {"", ".", ".."} for part in value.split("/")),
        "invalid_path",
    )
    return value


def version(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def identity(info):
    return info.st_dev, info.st_ino, info.st_mode


class CorpusGuard:
    """An immutable root capability with descriptor-relative, nofollow traversal.

    Cached hashes are reusable only under identical identity, size, timestamps
    and link count. Both modes walk every directory/file twice per query. Each
    pass also validates directory versions and the root ancestor chain.
    """

    def __init__(self, root, mode):
        self.root = Path(os.path.abspath(root))
        require(".." not in Path(root).parts, "invalid_root")
        self.mode = mode
        self.root_chain = None
        self.cache = {}
        with self.open_root():
            pass

    @contextmanager
    def open_root(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptors = []
        links = []
        try:
            current = os.open("/", flags)
            descriptors.append(current)
            chain = [identity(os.fstat(current))]
            for part in self.root.parts[1:]:
                child = os.open(part, flags, dir_fd=current)
                descriptors.append(child)
                expected = identity(os.fstat(child))
                links.append((current, part, expected))
                chain.append(expected)
                current = child
            if self.root_chain is None:
                self.root_chain = chain
            require(chain == self.root_chain, "root_changed")
            yield current
            for parent, part, expected in links:
                require(
                    identity(os.stat(part, dir_fd=parent, follow_symlinks=False)) == expected,
                    "root_changed",
                )
        except OSError:
            raise Rejected("source_changed") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def verify(self, expected):
        found = set()
        visited = 0
        total_bytes = 0
        next_cache = {}

        def walk(parent, prefix, depth):
            nonlocal visited, total_bytes
            require(depth <= 128, "source_budget")
            before = version(os.fstat(parent))
            with os.scandir(parent) as iterator:
                names = sorted(item.name for item in iterator)
            for name in names:
                visited += 1
                require(visited <= 4 * MAX_FILES, "source_budget")
                path = f"{prefix}/{name}" if prefix else name
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                require(not stat.S_ISLNK(info.st_mode), "symlink_rejected")
                if stat.S_ISDIR(info.st_mode):
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                    child = os.open(name, flags, dir_fd=parent)
                    try:
                        require(version(os.fstat(child)) == version(info), "source_changed")
                        walk(child, path, depth + 1)
                    finally:
                        os.close(child)
                    require(
                        version(os.stat(name, dir_fd=parent, follow_symlinks=False))
                        == version(info),
                        "source_changed",
                    )
                elif path.endswith((".py", ".java")):
                    require(path in expected, "inventory_changed")
                    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "source_changed")
                    size, digest = expected[path]
                    require(info.st_size == size, "source_changed")
                    total_bytes += size
                    require(total_bytes <= MAX_SOURCE_BYTES, "source_budget")
                    observed = version(info)
                    cached = self.cache.get(path)
                    if self.mode == "metadata" and cached and cached[0] == observed:
                        actual = cached[1]
                    else:
                        descriptor = os.open(
                            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent
                        )
                        try:
                            require(version(os.fstat(descriptor)) == observed, "source_changed")
                            hasher = hashlib.sha256()
                            consumed = 0
                            while block := os.read(descriptor, 65_536):
                                consumed += len(block)
                                require(consumed <= size, "source_changed")
                                hasher.update(block)
                            require(consumed == size, "source_changed")
                            require(version(os.fstat(descriptor)) == observed, "source_changed")
                            actual = hasher.hexdigest()
                        finally:
                            os.close(descriptor)
                    require(actual == digest, "source_changed")
                    require(
                        version(os.stat(name, dir_fd=parent, follow_symlinks=False)) == observed,
                        "source_changed",
                    )
                    next_cache[path] = observed, actual
                    found.add(path)
            require(version(os.fstat(parent)) == before, "source_changed")

        try:
            with self.open_root() as root:
                walk(root, "", 0)
            require(found == expected.keys(), "inventory_changed")
        except (OSError, RecursionError):
            raise Rejected("source_changed") from None
        self.cache = next_cache


def json_cost(value):
    return len(json.dumps(value, ensure_ascii=False))


def bound_architecture(result, maximum):
    """Same item removal/ties as bound_result, with reusable JSON lengths.

    Architecture uses files, not nodes, so production's edge/node pruning is
    inapplicable. Reusing lengths changes cost without changing any result.
    """
    arrays = [(key, result[key]) for key in ARRAY_ORDER]
    sizes = {key: [json_cost(item) for item in values] for key, values in arrays}
    costs = {key: 2 + sum(sizes[key]) + 2 * max(0, len(values) - 1) for key, values in arrays}
    total = json_cost(result)
    while total > maximum:
        candidates = [(key, values) for key, values in arrays if values]
        require(candidates, "response_budget")
        key, values = max(candidates, key=lambda item: costs[item[0]])
        removed = sizes[key].pop() + (2 if len(values) > 1 else 0)
        costs[key] -= removed
        total -= removed
        values.pop()
        if not result["truncated"]:
            result["truncated"] = True
            total -= 1  # JSON false -> true.
    return result


class PublishedGraph:
    def __init__(self, snapshot, implementation):
        require(isinstance(snapshot, dict) and snapshot.get("version") == 1)
        nodes = snapshot.get("nodes")
        edges = snapshot.get("edges")
        files = snapshot.get("files", snapshot.get("file_manifest"))
        require(not ("files" in snapshot and "file_manifest" in snapshot))
        require(isinstance(nodes, list) and len(nodes) <= MAX_FILES)
        require(isinstance(edges, list) and len(edges) <= MAX_EDGES)
        require(isinstance(files, list) and len(files) <= MAX_FILES)
        require(all(relative_path(path).endswith((".py", ".java")) for path in nodes))
        require(nodes == sorted(set(nodes)))
        self.names = nodes
        self.path_costs = [json_cost(path) for path in nodes]
        self.position = {path: index for index, path in enumerate(nodes)}
        self.edges = edges
        self.files = {}
        for item in files:
            require(isinstance(item, dict))
            path = relative_path(item.get("path"))
            digest = item.get("sha256")
            size = item.get("size")
            require(path in self.position and path not in self.files)
            require(
                isinstance(digest, str)
                and len(digest) == 64
                and all(char in "0123456789abcdef" for char in digest)
            )
            require(type(size) is int and 0 <= size <= MAX_FILE_BYTES)
            self.files[path] = size, digest
        require(self.files.keys() == self.position.keys())
        self.outgoing = [[] for _ in nodes]
        self.incoming = [[] for _ in nodes]
        self.all_outgoing = [[] for _ in nodes]
        self.all_incoming = [[] for _ in nodes]
        previous = None
        for edge in edges:
            require(isinstance(edge, dict))
            source, target = edge.get("source"), edge.get("target")
            require(source in self.position and target in self.position)
            require(type(edge.get("type_only")) is bool)
            require(isinstance(edge.get("kinds"), list) and isinstance(edge.get("evidence"), list))
            require(type(edge.get("evidence_count")) is int)
            pair = source, target
            require(previous is None or pair > previous)
            previous = pair
            left, right = self.position[source], self.position[target]
            self.all_outgoing[left].append(right)
            self.all_incoming[right].append(left)
            if not edge["type_only"]:
                self.outgoing[left].append(right)
                self.incoming[right].append(left)
        self.implementation = implementation
        self.edge_costs = [json_cost(edge) for edge in edges]

    def components(self, outgoing, incoming, selected):
        if self.implementation == "legacy":
            chosen = {self.names[node] for node in selected}
            adjacency = {
                self.names[node]: [
                    self.names[other] for other in outgoing[node] if other in selected
                ]
                for node in selected
            }
            return [
                [self.position[name] for name in group] for group in _components(chosen, adjacency)
            ]
        seen = bytearray(len(self.names))
        order = []
        for root in sorted(selected):
            if seen[root]:
                continue
            seen[root] = 1
            stack = [(root, 0)]
            while stack:
                node, offset = stack[-1]
                if offset < len(outgoing[node]):
                    other = outgoing[node][offset]
                    stack[-1] = node, offset + 1
                    if other in selected and not seen[other]:
                        seen[other] = 1
                        stack.append((other, 0))
                else:
                    order.append(node)
                    stack.pop()
        seen = bytearray(len(self.names))
        groups = []
        for root in reversed(order):
            if seen[root]:
                continue
            seen[root] = 1
            stack = [root]
            group = []
            while stack:
                node = stack.pop()
                group.append(node)
                for other in incoming[node]:
                    if other in selected and not seen[other]:
                        seen[other] = 1
                        stack.append(other)
            groups.append(sorted(group))
        return groups

    def architecture(self, request):
        maximum_nodes = request.get("max_nodes", 100)
        maximum_edges = request.get("max_edges", 200)
        maximum_chars = request.get("max_chars", 20_000)
        require(type(maximum_nodes) is int and 1 <= maximum_nodes <= 500, "invalid_bounds")
        require(type(maximum_edges) is int and 1 <= maximum_edges <= 1000, "invalid_bounds")
        require(type(maximum_chars) is int and 1000 <= maximum_chars <= 50_000, "invalid_bounds")
        prefix = request.get("path_prefix")
        if prefix:
            relative_path(prefix.rstrip("/"))
        require(prefix is None or isinstance(prefix, str), "invalid_path")
        include_type_only = request.get("include_type_only", False)
        require(type(include_type_only) is bool, "invalid_bounds")
        outgoing = self.all_outgoing if include_type_only else self.outgoing
        incoming = self.all_incoming if include_type_only else self.incoming
        chosen = {
            index for index, path in enumerate(self.names) if not prefix or path.startswith(prefix)
        }
        groups = self.components(outgoing, incoming, chosen)
        cycles = [group for group in groups if len(group) > 1 or group[0] in outgoing[group[0]]]
        membership = [-1] * len(self.names)
        for index, group in enumerate(groups):
            for node in group:
                membership[node] = index
        cycle_ids = {membership[group[0]] for group in cycles}
        dependencies = [set() for _ in groups]
        dependents = [set() for _ in groups]
        for source in chosen:
            for target in outgoing[source]:
                if target not in chosen:
                    continue
                left, right = membership[source], membership[target]
                if left != right:
                    dependencies[left].add(right)
                    dependents[right].add(left)
        counts = [len(values) for values in dependencies]
        queue = deque(index for index, count in enumerate(counts) if not count)
        levels = [0] * len(groups)
        while queue:
            current = queue.popleft()
            for parent in dependents[current]:
                levels[parent] = max(levels[parent], levels[current] + 1)
                counts[parent] -= 1
                if not counts[parent]:
                    queue.append(parent)
        selected = set(sorted(chosen)[:maximum_nodes])
        layer_nodes = sorted(
            selected, key=lambda node: (levels[membership[node]], self.names[node])
        )
        edge_indices = [
            index
            for index, edge in enumerate(self.edges)
            if self.position[edge["source"]] in selected
            and self.position[edge["target"]] in selected
            and (include_type_only or not edge["type_only"])
        ]
        visible_edge_count = len(edge_indices)
        edge_indices = edge_indices[:maximum_edges]
        cycle_indices = [
            index
            for index, group in enumerate(groups)
            if index in cycle_ids and selected.intersection(group)
        ]
        truncated = len(chosen) > maximum_nodes or visible_edge_count > maximum_edges
        base = {
            "files": [],
            "edges": [],
            "cycles": [],
            "total_files": len(chosen),
            "total_cycles": len(cycles),
            "truncated": truncated,
            "layer_order": "dependency_first; layer 0 = no resolved local dependencies "
            "outside its cycle group",
            "coverage": "condensed static dependency graph; layers are not business semantics",
        }
        lengths = [len(layer_nodes), len(edge_indices), len(cycle_indices)]
        if self.implementation == "indexed":
            group_costs = [
                2 + sum(self.path_costs[node] for node in group) + 2 * max(0, len(group) - 1)
                for group in groups
            ]
            file_overhead = (
                2 + sum(json_cost(key) + 2 for key in ("path", "layer", "cycle_group")) + 4
            )
            sizes = [
                [
                    file_overhead
                    + self.path_costs[node]
                    + len(str(levels[membership[node]]))
                    + (group_costs[membership[node]] if membership[node] in cycle_ids else 2)
                    for node in layer_nodes
                ],
                [self.edge_costs[index] for index in edge_indices],
                [group_costs[index] for index in cycle_indices],
            ]
            costs = [2 + sum(values) + 2 * max(0, len(values) - 1) for values in sizes]
            total = json_cost(base) - 6 + sum(costs)
            while total > maximum_chars:
                candidates = [index for index in range(3) if lengths[index]]
                require(candidates, "response_budget")
                index = max(candidates, key=lambda index: costs[index])
                removed = sizes[index][lengths[index] - 1] + (2 if lengths[index] > 1 else 0)
                lengths[index] -= 1
                costs[index] -= removed
                total -= removed
                if not base["truncated"]:
                    base["truncated"] = True
                    total -= 1
        files = [
            {
                "path": self.names[node],
                "layer": levels[membership[node]],
                "cycle_group": [self.names[index] for index in groups[membership[node]]]
                if membership[node] in cycle_ids
                else [],
            }
            for node in layer_nodes[: lengths[0]]
        ]
        result = {
            **base,
            "files": files,
            "edges": [self.edges[index] for index in edge_indices[: lengths[1]]],
            "cycles": [
                [self.names[node] for node in groups[index]]
                for index in cycle_indices[: lengths[2]]
            ],
        }
        if self.implementation == "legacy":
            try:
                return bound_result(result, maximum_chars)
            except ValueError:
                raise Rejected("response_budget") from None
        return result

    def impact(self, request):
        root = request.get("root")
        depth = request.get("depth", 5)
        require(root in self.position, "invalid_root")
        require(type(depth) is int and 0 <= depth <= 5, "invalid_depth")
        origin = self.position[root]
        distances = [-1] * len(self.names)
        distances[origin] = 0
        queue = deque([origin])
        while queue:
            node = queue.popleft()
            if distances[node] >= depth:
                continue
            for parent in self.incoming[node]:
                if distances[parent] < 0:
                    distances[parent] = distances[node] + 1
                    queue.append(parent)
        result = {
            "affected": {
                name: distances[index]
                for index, name in enumerate(self.names)
                if index != origin and distances[index] >= 0
            }
        }
        maximum = request.get("max_chars", 20_000)
        require(type(maximum) is int and 1000 <= maximum <= 50_000, "invalid_bounds")
        require(json_cost(result) <= maximum, "response_budget")
        return result


class Pipeline:
    def __init__(self, root, guard_mode, implementation):
        self.guard = CorpusGuard(root, guard_mode)
        self.graph = None
        self.implementation = implementation

    def query(self, request):
        require(self.graph is not None, "not_loaded")
        self.guard.verify(self.graph.files)
        if request.get("command") == "architecture":
            response = self.graph.architecture(request)
        elif request.get("command") == "impact":
            response = self.graph.impact(request)
        else:
            raise Rejected("invalid_command")
        self.guard.verify(self.graph.files)
        return response

    def handle(self, request):
        require(isinstance(request, dict), "invalid_command")
        command = request.get("command")
        if command == "ping":
            return {"protocol": 1, "scope": "fixed-snapshot-pipeline-pilot"}
        if command == "stats":
            usage = resource.getrusage(resource.RUSAGE_SELF)
            return {
                "peak_rss_bytes": usage.ru_maxrss,
                "cpu_user_us": round(usage.ru_utime * 1_000_000),
                "cpu_system_us": round(usage.ru_stime * 1_000_000),
            }
        if command == "load":
            replacement = PublishedGraph(request.get("snapshot"), self.implementation)
            self.guard.verify(replacement.files)
            self.guard.verify(replacement.files)
            self.graph = replacement
            return {
                "loaded": len(replacement.names),
                "edges": len(replacement.edges),
                "files": len(replacement.files),
            }
        if command == "batch":
            queries = request.get("queries")
            require(isinstance(queries, list) and 1 <= len(queries) <= 1000, "invalid_batch")
            require(all(isinstance(query, dict) for query in queries), "invalid_batch")
            return {"results": [self.query(query) for query in queries]}
        return self.query(request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--guard", choices=("metadata", "strict"), default="metadata")
    parser.add_argument("--implementation", choices=("legacy", "indexed"), default="indexed")
    args = parser.parse_args()
    pipeline = Pipeline(args.root, args.guard, args.implementation)
    while raw := sys.stdin.buffer.readline(MAX_REQUEST + 1):
        if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
            sys.stdout.buffer.write(b'{"error":"request_budget"}\n')
            sys.stdout.buffer.flush()
            break
        try:
            try:
                request = json.loads(raw)
            except ValueError:
                raise Rejected("invalid_json") from None
            response = pipeline.handle(request)
        except Rejected as exc:
            response = {"error": str(exc)}
        except (TypeError, KeyError, AttributeError, OverflowError, RecursionError):
            response = {"error": "invalid_snapshot"}
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        if len(encoded) > MAX_REQUEST:
            encoded = b'{"error":"response_budget"}\n'
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
