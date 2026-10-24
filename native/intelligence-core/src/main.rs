//! Experimental graph computation only. No file access, commands or project execution.
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::{self, BufRead, Read, Write};
use std::time::Instant;

const MAX_REQUEST: usize = 8 * 1024 * 1024;
const MAX_NODES: usize = 50_000;
const MAX_EDGES: usize = 500_000;

#[derive(Default)]
struct Graph {
    names: Vec<String>,
    outgoing: Vec<Vec<usize>>,
    incoming: Vec<Vec<usize>>,
}

impl Graph {
    fn load(request: &Value) -> Result<Self, &'static str> {
        let names = request["nodes"].as_array().ok_or("invalid_nodes")?;
        let edges = request["edges"].as_array().ok_or("invalid_edges")?;
        if names.len() > MAX_NODES || edges.len() > MAX_EDGES {
            return Err("graph_budget");
        }
        let mut graph = Graph {
            names: Vec::with_capacity(names.len()),
            outgoing: vec![Vec::new(); names.len()],
            incoming: vec![Vec::new(); names.len()],
        };
        let mut unique = BTreeSet::new();
        for name in names {
            let name = name.as_str().ok_or("invalid_node")?;
            if name.len() > 4096 || !unique.insert(name) {
                return Err("invalid_node");
            }
            graph.names.push(name.to_owned());
        }
        let mut pairs = BTreeSet::new();
        for edge in edges {
            let pair = edge.as_array().ok_or("invalid_edge")?;
            if pair.len() != 2 {
                return Err("invalid_edge");
            }
            let from = pair[0].as_u64().ok_or("invalid_edge")? as usize;
            let to = pair[1].as_u64().ok_or("invalid_edge")? as usize;
            if from >= names.len() || to >= names.len() {
                return Err("invalid_endpoint");
            }
            if pairs.insert((from, to)) {
                graph.outgoing[from].push(to);
                graph.incoming[to].push(from);
            }
        }
        for neighbors in graph.outgoing.iter_mut().chain(graph.incoming.iter_mut()) {
            neighbors.sort_unstable();
        }
        Ok(graph)
    }

    fn components(&self) -> Vec<Vec<String>> {
        let count = self.names.len();
        let mut seen = vec![false; count];
        let mut order = Vec::with_capacity(count);
        for root in 0..count {
            if seen[root] { continue; }
            seen[root] = true;
            let mut stack = vec![(root, 0)];
            while let Some((node, offset)) = stack.last_mut() {
                if *offset < self.outgoing[*node].len() {
                    let target = self.outgoing[*node][*offset];
                    *offset += 1;
                    if !seen[target] {
                        seen[target] = true;
                        stack.push((target, 0));
                    }
                } else {
                    order.push(*node);
                    stack.pop();
                }
            }
        }
        seen.fill(false);
        let mut result = Vec::new();
        for root in order.into_iter().rev() {
            if seen[root] { continue; }
            seen[root] = true;
            let mut stack = vec![root];
            let mut group = Vec::new();
            while let Some(node) = stack.pop() {
                group.push(self.names[node].clone());
                for &parent in &self.incoming[node] {
                    if !seen[parent] {
                        seen[parent] = true;
                        stack.push(parent);
                    }
                }
            }
            group.sort();
            result.push(group);
        }
        result
    }

    fn impact(&self, root: usize, depth: usize) -> Result<BTreeMap<String, usize>, &'static str> {
        if root >= self.names.len() || depth > 5 { return Err("invalid_impact"); }
        let mut distances = vec![usize::MAX; self.names.len()];
        distances[root] = 0;
        let mut queue = VecDeque::from([root]);
        while let Some(node) = queue.pop_front() {
            if distances[node] >= depth { continue; }
            for &parent in &self.incoming[node] {
                if distances[parent] == usize::MAX {
                    distances[parent] = distances[node] + 1;
                    queue.push_back(parent);
                }
            }
        }
        Ok(distances.iter().enumerate().filter_map(|(index, &distance)| {
            (index != root && distance != usize::MAX).then(|| (self.names[index].clone(), distance))
        }).collect())
    }
}

fn handle(graph: &mut Graph, request: &Value) -> Result<Value, &'static str> {
    match request["command"].as_str().ok_or("invalid_command")? {
        "load" => {
            let replacement = Graph::load(request)?;
            let count = replacement.names.len();
            *graph = replacement;
            Ok(json!({"loaded": count}))
        },
        "components" => Ok(json!({"components": graph.components()})),
        "impact" => {
            let root = request["root"].as_u64().ok_or("invalid_root")? as usize;
            let depth = request["depth"].as_u64().ok_or("invalid_depth")? as usize;
            Ok(json!({"affected": graph.impact(root, depth)?}))
        },
        "ping" => Ok(json!({"protocol": 1, "scope": "graph-pilot-only"})),
        _ => Err("invalid_command"),
    }
}

fn main() -> io::Result<()> {
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    let stdout = io::stdout();
    let mut writer = stdout.lock();
    let mut graph = Graph::default();
    loop {
        let mut raw = Vec::new();
        let read = reader.by_ref().take((MAX_REQUEST + 1) as u64).read_until(b'\n', &mut raw)?;
        if read == 0 { break; }
        if raw.len() > MAX_REQUEST {
            writeln!(writer, "{{\"error\":\"request_budget\"}}")?;
            writer.flush()?;
            break;
        }
        let tick = Instant::now();
        let response = match serde_json::from_slice::<Value>(&raw) {
            Ok(request) => match handle(&mut graph, &request) {
                Ok(mut response) => {
                    response["compute_ns"] = json!(tick.elapsed().as_nanos() as u64);
                    response
                },
                Err(code) => json!({"error": code}),
            },
            Err(_) => json!({"error": "invalid_json"}),
        };
        serde_json::to_writer(&mut writer, &response)?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }
    Ok(())
}
