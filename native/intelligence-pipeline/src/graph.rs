//! The complete fixed-snapshot graph publication, architecture and output budget.
use crate::guard::{relative_path, source_path, ExpectedFile, Failure, MAX_FILES, MAX_FILE_BYTES, MAX_SOURCE_BYTES};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

const MAX_EDGES: usize = 1_000_000;
const LAYER_ORDER: &str = "dependency_first; layer 0 = no resolved local dependencies outside its cycle group";
const COVERAGE: &str = "condensed static dependency graph; layers are not business semantics";

pub fn json_chars(value: &Value) -> usize {
    match value {
        Value::Array(items) => array_chars(&items.iter().map(json_chars).collect::<Vec<_>>()),
        Value::Object(items) => object_chars(&items.iter().map(|(key, item)| (key.as_str(), json_chars(item))).collect::<Vec<_>>()),
        _ => serde_json::to_string(value).expect("validated JSON value").chars().count(),
    }
}

fn string_chars(value: &str) -> usize {
    serde_json::to_string(value).expect("valid UTF-8 string").chars().count()
}

fn array_chars(items: &[usize]) -> usize {
    2 + items.iter().sum::<usize>() + 2 * items.len().saturating_sub(1)
}

fn object_chars(items: &[(&str, usize)]) -> usize {
    2 + items.iter().map(|(key, size)| string_chars(key) + 2 + size).sum::<usize>()
        + 2 * items.len().saturating_sub(1)
}

fn integer(request: &Value, key: &str, default: usize, low: usize, high: usize) -> Result<usize, Failure> {
    let value = match request.get(key) {
        None => default,
        Some(value) => usize::try_from(value.as_u64().ok_or("invalid_bounds")?).map_err(|_| "invalid_bounds")?,
    };
    if !(low..=high).contains(&value) { return Err("invalid_bounds"); }
    Ok(value)
}

struct Edge {
    source: usize,
    target: usize,
    type_only: bool,
    payload: Value,
    chars: usize,
}

#[derive(Default)]
struct Adjacency {
    outgoing: Vec<Vec<usize>>,
    incoming: Vec<Vec<usize>>,
}

pub struct PublishedGraph {
    names: Vec<String>,
    position: HashMap<String, usize>,
    path_chars: Vec<usize>,
    edges: Vec<Edge>,
    normal: Adjacency,
    all: Adjacency,
    pub files: BTreeMap<String, ExpectedFile>,
}

impl PublishedGraph {
    pub fn load(snapshot: &Value) -> Result<Self, Failure> {
        if snapshot.get("version").and_then(Value::as_u64) != Some(1)
            || (snapshot.get("files").is_some() && snapshot.get("file_manifest").is_some())
        { return Err("invalid_snapshot"); }
        let nodes = snapshot.get("nodes").and_then(Value::as_array).ok_or("invalid_snapshot")?;
        let edges = snapshot.get("edges").and_then(Value::as_array).ok_or("invalid_snapshot")?;
        let entries = snapshot.get("files").or_else(|| snapshot.get("file_manifest"))
            .and_then(Value::as_array).ok_or("invalid_snapshot")?;
        if nodes.len() > MAX_FILES || entries.len() > MAX_FILES || edges.len() > MAX_EDGES {
            return Err("invalid_snapshot");
        }
        let mut names: Vec<String> = Vec::with_capacity(nodes.len());
        let mut position = HashMap::with_capacity(nodes.len());
        for node in nodes {
            let path = node.as_str().ok_or("invalid_path")?;
            relative_path(path)?;
            if !source_path(path) || names.last().is_some_and(|previous| previous.as_str() >= path) {
                return Err("invalid_snapshot");
            }
            position.insert(path.to_owned(), names.len());
            names.push(path.to_owned());
        }
        let mut files = BTreeMap::new();
        let mut total_size = 0;
        for entry in entries {
            let path = entry.get("path").and_then(Value::as_str).ok_or("invalid_path")?;
            relative_path(path)?;
            if !position.contains_key(path) || files.contains_key(path) { return Err("invalid_snapshot"); }
            let digest = entry.get("sha256").and_then(Value::as_str).ok_or("invalid_snapshot")?;
            if digest.len() != 64 || !digest.bytes().all(|ch| ch.is_ascii_digit() || (b'a'..=b'f').contains(&ch)) {
                return Err("invalid_snapshot");
            }
            let size = entry.get("size").and_then(Value::as_u64).ok_or("invalid_snapshot")?;
            if size > MAX_FILE_BYTES { return Err("invalid_snapshot"); }
            total_size += size;
            if total_size > MAX_SOURCE_BYTES { return Err("source_budget"); }
            let mut hash = [0u8; 32];
            for (index, item) in hash.iter_mut().enumerate() {
                *item = u8::from_str_radix(&digest[index * 2..index * 2 + 2], 16).map_err(|_| "invalid_snapshot")?;
            }
            files.insert(path.to_owned(), ExpectedFile { size, hash });
        }
        if files.len() != names.len() { return Err("invalid_snapshot"); }
        let mut graph = Self {
            path_chars: names.iter().map(|name| string_chars(name)).collect(),
            normal: Adjacency { outgoing: vec![vec![]; names.len()], incoming: vec![vec![]; names.len()] },
            all: Adjacency { outgoing: vec![vec![]; names.len()], incoming: vec![vec![]; names.len()] },
            names, position, files, edges: Vec::with_capacity(edges.len()),
        };
        let mut previous = None;
        for payload in edges {
            let source = payload.get("source").and_then(Value::as_str).and_then(|path| graph.position.get(path)).copied().ok_or("invalid_snapshot")?;
            let target = payload.get("target").and_then(Value::as_str).and_then(|path| graph.position.get(path)).copied().ok_or("invalid_snapshot")?;
            let type_only = payload.get("type_only").and_then(Value::as_bool).ok_or("invalid_snapshot")?;
            if previous.is_some_and(|pair| pair >= (source, target))
                || !payload.get("kinds").is_some_and(Value::is_array)
                || !payload.get("evidence").is_some_and(Value::is_array)
                || !payload.get("evidence_count").is_some_and(|count| count.as_i64().is_some())
            { return Err("invalid_snapshot"); }
            previous = Some((source, target));
            graph.all.outgoing[source].push(target);
            graph.all.incoming[target].push(source);
            if !type_only {
                graph.normal.outgoing[source].push(target);
                graph.normal.incoming[target].push(source);
            }
            graph.edges.push(Edge { source, target, type_only, payload: payload.clone(), chars: json_chars(payload) });
        }
        Ok(graph)
    }

    pub fn counts(&self) -> Value {
        json!({"loaded": self.names.len(), "edges": self.edges.len(), "files": self.files.len()})
    }

    fn components(&self, adjacency: &Adjacency, chosen: &[bool]) -> Vec<Vec<usize>> {
        let mut seen = vec![false; self.names.len()];
        let mut order = Vec::new();
        for root in 0..self.names.len() {
            if !chosen[root] || seen[root] { continue; }
            seen[root] = true;
            let mut stack = vec![(root, 0)];
            while let Some((node, offset)) = stack.last_mut() {
                if *offset < adjacency.outgoing[*node].len() {
                    let next = adjacency.outgoing[*node][*offset];
                    *offset += 1;
                    if chosen[next] && !seen[next] {
                        seen[next] = true;
                        stack.push((next, 0));
                    }
                } else {
                    order.push(*node);
                    stack.pop();
                }
            }
        }
        seen.fill(false);
        let mut groups = Vec::new();
        for root in order.into_iter().rev() {
            if seen[root] { continue; }
            seen[root] = true;
            let mut group = Vec::new();
            let mut stack = vec![root];
            while let Some(node) = stack.pop() {
                group.push(node);
                for &parent in &adjacency.incoming[node] {
                    if chosen[parent] && !seen[parent] {
                        seen[parent] = true;
                        stack.push(parent);
                    }
                }
            }
            group.sort_unstable();
            groups.push(group);
        }
        groups
    }

    pub fn architecture(&self, request: &Value) -> Result<Value, Failure> {
        let max_nodes = integer(request, "max_nodes", 100, 1, 500)?;
        let max_edges = integer(request, "max_edges", 200, 1, 1000)?;
        let max_chars = integer(request, "max_chars", 20_000, 1000, 50_000)?;
        let prefix = match request.get("path_prefix") {
            None | Some(Value::Null) => "",
            Some(Value::String(value)) => { if !value.is_empty() { relative_path(value.trim_end_matches('/'))?; } value },
            _ => return Err("invalid_path"),
        };
        let include = match request.get("include_type_only") {
            None => false,
            Some(value) => value.as_bool().ok_or("invalid_bounds")?,
        };
        let adjacency = if include { &self.all } else { &self.normal };
        let chosen = self.names.iter().map(|path| prefix.is_empty() || path.starts_with(prefix)).collect::<Vec<_>>();
        let total_files = chosen.iter().filter(|&&selected| selected).count();
        let groups = self.components(adjacency, &chosen);
        let mut membership = vec![usize::MAX; self.names.len()];
        for (index, group) in groups.iter().enumerate() {
            for &node in group { membership[node] = index; }
        }
        let cyclic = groups.iter().map(|group| group.len() > 1 || adjacency.outgoing[group[0]].binary_search(&group[0]).is_ok()).collect::<Vec<_>>();
        let total_cycles = cyclic.iter().filter(|&&cycle| cycle).count();
        let mut dependencies = vec![BTreeSet::new(); groups.len()];
        let mut dependents = vec![BTreeSet::new(); groups.len()];
        for source in 0..self.names.len() {
            if !chosen[source] { continue; }
            for &target in &adjacency.outgoing[source] {
                if !chosen[target] { continue; }
                let (left, right) = (membership[source], membership[target]);
                if left != right {
                    dependencies[left].insert(right);
                    dependents[right].insert(left);
                }
            }
        }
        let mut counts = dependencies.iter().map(BTreeSet::len).collect::<Vec<_>>();
        let mut queue = counts.iter().enumerate().filter_map(|(index, &count)| (count == 0).then_some(index)).collect::<VecDeque<_>>();
        let mut levels = vec![0usize; groups.len()];
        while let Some(current) = queue.pop_front() {
            for &parent in &dependents[current] {
                levels[parent] = levels[parent].max(levels[current] + 1);
                counts[parent] -= 1;
                if counts[parent] == 0 { queue.push_back(parent); }
            }
        }
        let mut selected = vec![false; self.names.len()];
        let mut layer_nodes = chosen.iter().enumerate().filter_map(|(index, &value)| value.then_some(index)).take(max_nodes).collect::<Vec<_>>();
        for &node in &layer_nodes { selected[node] = true; }
        layer_nodes.sort_unstable_by_key(|&node| (levels[membership[node]], node));
        let visible = self.edges.iter().enumerate().filter_map(|(index, edge)| {
            (selected[edge.source] && selected[edge.target] && (include || !edge.type_only)).then_some(index)
        }).collect::<Vec<_>>();
        let edge_indices = visible.iter().take(max_edges).copied().collect::<Vec<_>>();
        let cycle_indices = groups.iter().enumerate().filter_map(|(index, group)| {
            (cyclic[index] && group.iter().any(|&node| selected[node])).then_some(index)
        }).collect::<Vec<_>>();
        // Measure virtual rows rather than cloning a 100K-member cycle into
        // each selected file only to discard it during bounded output clipping.
        let group_chars = groups.iter().map(|group| array_chars(&group.iter().map(|&node| self.path_chars[node]).collect::<Vec<_>>())).collect::<Vec<_>>();
        let file_sizes = layer_nodes.iter().map(|&node| object_chars(&[
            ("path", self.path_chars[node]),
            ("layer", levels[membership[node]].to_string().len()),
            ("cycle_group", if cyclic[membership[node]] { group_chars[membership[node]] } else { 2 }),
        ])).collect::<Vec<_>>();
        let edge_sizes = edge_indices.iter().map(|&index| self.edges[index].chars).collect::<Vec<_>>();
        let cycle_sizes = cycle_indices.iter().map(|&index| group_chars[index]).collect::<Vec<_>>();
        let sizes = [&file_sizes, &edge_sizes, &cycle_sizes];
        let mut lengths = [file_sizes.len(), edge_sizes.len(), cycle_sizes.len()];
        let mut costs = [array_chars(&file_sizes), array_chars(&edge_sizes), array_chars(&cycle_sizes)];
        let mut truncated = total_files > max_nodes || visible.len() > max_edges;
        loop {
            let total = object_chars(&[
                ("files", costs[0]), ("edges", costs[1]), ("cycles", costs[2]),
                ("total_files", total_files.to_string().len()), ("total_cycles", total_cycles.to_string().len()),
                ("truncated", if truncated { 4 } else { 5 }),
                ("layer_order", string_chars(LAYER_ORDER)), ("coverage", string_chars(COVERAGE)),
            ]);
            if total <= max_chars { break; }
            // Fixed production insertion-order ties: files, then edges, cycles.
            let mut candidate = None;
            for index in 0..3 {
                if lengths[index] > 0 && candidate.is_none_or(|other| costs[index] > costs[other]) {
                    candidate = Some(index);
                }
            }
            let index = candidate.ok_or("response_budget")?;
            let removed = sizes[index][lengths[index] - 1] + if lengths[index] > 1 { 2 } else { 0 };
            costs[index] -= removed;
            lengths[index] -= 1;
            truncated = true;
        }
        let files = layer_nodes.iter().take(lengths[0]).map(|&node| {
            let cycle_group = if cyclic[membership[node]] {
                groups[membership[node]].iter().map(|&member| self.names[member].clone()).collect::<Vec<_>>()
            } else { Vec::new() };
            json!({"path": self.names[node], "layer": levels[membership[node]], "cycle_group": cycle_group})
        }).collect::<Vec<_>>();
        let edges = edge_indices.iter().take(lengths[1]).map(|&index| self.edges[index].payload.clone()).collect::<Vec<_>>();
        let cycles = cycle_indices.iter().take(lengths[2]).map(|&index| groups[index].iter().map(|&node| self.names[node].clone()).collect::<Vec<_>>()).collect::<Vec<_>>();
        Ok(json!({"files": files, "edges": edges, "cycles": cycles, "total_files": total_files,
            "total_cycles": total_cycles, "truncated": truncated, "layer_order": LAYER_ORDER, "coverage": COVERAGE}))
    }

    pub fn impact(&self, request: &Value) -> Result<Value, Failure> {
        let root = request.get("root").and_then(Value::as_str).and_then(|name| self.position.get(name)).copied().ok_or("invalid_root")?;
        let depth = match request.get("depth") {
            None => 5,
            Some(value) => value.as_u64().filter(|&value| value <= 5).ok_or("invalid_depth")? as usize,
        };
        let maximum = integer(request, "max_chars", 20_000, 1000, 50_000)?;
        let mut distances = vec![usize::MAX; self.names.len()];
        distances[root] = 0;
        let mut queue = VecDeque::from([root]);
        while let Some(node) = queue.pop_front() {
            if distances[node] >= depth { continue; }
            for &parent in &self.normal.incoming[node] {
                if distances[parent] == usize::MAX {
                    distances[parent] = distances[node] + 1;
                    queue.push_back(parent);
                }
            }
        }
        let affected = distances.iter().enumerate().filter_map(|(index, &distance)| {
            (index != root && distance != usize::MAX).then(|| (self.names[index].clone(), distance))
        }).collect::<BTreeMap<_, _>>();
        let result = json!({"affected": affected});
        if json_chars(&result) > maximum { return Err("response_budget"); }
        Ok(result)
    }
}
