//! Private fixed-snapshot experiment; never registered as a production MCP tool.
mod graph;
mod guard;

use graph::PublishedGraph;
use guard::{CorpusGuard, Failure};
use serde_json::{json, Value};
use std::io::{self, BufRead, Read, Write};
use std::path::Path;

const MAX_PACKET: usize = 64 * 1024 * 1024;

struct Pipeline {
    guard: CorpusGuard,
    graph: Option<PublishedGraph>,
}

impl Pipeline {
    fn query(&mut self, request: &Value) -> Result<Value, Failure> {
        let graph = self.graph.as_ref().ok_or("not_loaded")?;
        self.guard.verify(&graph.files)?;
        let response = match request.get("command").and_then(Value::as_str) {
            Some("architecture") => graph.architecture(request)?,
            Some("impact") => graph.impact(request)?,
            _ => return Err("invalid_command"),
        };
        self.guard.verify(&graph.files)?;
        Ok(response)
    }

    fn handle(&mut self, request: &Value) -> Result<Value, Failure> {
        if !request.is_object() { return Err("invalid_command"); }
        match request.get("command").and_then(Value::as_str) {
            Some("ping") => Ok(json!({"protocol": 1, "scope": "fixed-snapshot-pipeline-pilot"})),
            Some("stats") => stats(),
            Some("load") => {
                let replacement = PublishedGraph::load(request.get("snapshot").ok_or("invalid_snapshot")?)?;
                self.guard.verify(&replacement.files)?;
                self.guard.verify(&replacement.files)?;
                let response = replacement.counts();
                self.graph = Some(replacement);
                Ok(response)
            }
            Some("batch") => {
                let queries = request.get("queries").and_then(Value::as_array).ok_or("invalid_batch")?;
                if queries.is_empty() || queries.len() > 1000 || queries.iter().any(|query| !query.is_object()) {
                    return Err("invalid_batch");
                }
                let mut results = Vec::with_capacity(queries.len());
                for query in queries { results.push(self.query(query)?); }
                Ok(json!({"results": results}))
            }
            _ => self.query(request),
        }
    }
}

fn stats() -> Result<Value, Failure> {
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
    // SAFETY: getrusage initializes the supplied output on success, checked here.
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) } != 0 { return Err("stats_unavailable"); }
    let usage = unsafe { usage.assume_init() };
    let rss = usage.ru_maxrss.max(0) as u64;
    #[cfg(not(target_os = "macos"))]
    let rss = rss * 1024;
    let micros = |value: libc::timeval| (value.tv_sec.max(0) as u64) * 1_000_000 + value.tv_usec.max(0) as u64;
    Ok(json!({"peak_rss_bytes": rss, "cpu_user_us": micros(usage.ru_utime), "cpu_system_us": micros(usage.ru_stime)}))
}

fn arguments() -> Result<CorpusGuard, Failure> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    let mut root = None;
    let mut mode = "metadata";
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--root" if root.is_none() && index + 1 < args.len() => { root = Some(args[index + 1].as_str()); }
            "--guard" if index + 1 < args.len() => { mode = args[index + 1].as_str(); }
            _ => return Err("invalid_request"),
        }
        index += 2;
    }
    CorpusGuard::new(Path::new(root.ok_or("invalid_root")?), mode)
}

fn main() -> io::Result<()> {
    let stdout = io::stdout();
    let mut writer = stdout.lock();
    let guard = match arguments() {
        Ok(guard) => guard,
        Err(code) => { writeln!(writer, "{}", json!({"error": code}))?; return Ok(()); }
    };
    let mut pipeline = Pipeline { guard, graph: None };
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    loop {
        let mut raw = Vec::new();
        let read = reader.by_ref().take((MAX_PACKET + 1) as u64).read_until(b'\n', &mut raw)?;
        if read == 0 { break; }
        if raw.len() > MAX_PACKET || raw.last() != Some(&b'\n') {
            writeln!(writer, "{{\"error\":\"request_budget\"}}")?;
            writer.flush()?;
            break;
        }
        let response = match serde_json::from_slice::<Value>(&raw) {
            Ok(request) => match pipeline.handle(&request) {
                Ok(response) => response,
                Err(code) => json!({"error": code}),
            },
            Err(_) => json!({"error": "invalid_json"}),
        };
        let encoded = serde_json::to_vec(&response).map_err(io::Error::other)?;
        if encoded.len() + 1 > MAX_PACKET {
            writer.write_all(b"{\"error\":\"response_budget\"}\n")?;
        } else {
            writer.write_all(&encoded)?;
            writer.write_all(b"\n")?;
        }
        writer.flush()?;
    }
    Ok(())
}
