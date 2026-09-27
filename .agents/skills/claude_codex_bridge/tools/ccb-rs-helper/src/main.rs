use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::io::{self, Read};
use std::path::{Path, PathBuf};

const HELPER_NAME: &str = "ccb-rs-helper";
const HELPER_VERSION: &str = env!("CARGO_PKG_VERSION");
const SCHEMA_VERSION: u32 = 1;
const CONTRACT_ECHO: &str = "contract.echo";
const NATIVE_OUTPUT_OBSERVE: &str = "native.output.observe";
const STORAGE_SCAN_INVENTORY: &str = "storage.scan.inventory";
const STORAGE_SCAN_SUMMARY: &str = "storage.scan.summary";

#[derive(Debug, Deserialize)]
struct HelperRequest {
    #[allow(dead_code)]
    schema_version: Option<u32>,
    capability: String,
    #[serde(default)]
    payload: Value,
}

#[derive(Debug, Deserialize)]
struct NativeOutputPayload {
    path: PathBuf,
}

#[derive(Debug, Deserialize)]
struct StorageScanPayload {
    roots: Vec<StorageScanRoot>,
}

#[derive(Debug, Deserialize)]
struct StorageSummaryPayload {
    roots: Vec<StorageScanRoot>,
    ccb_dir: PathBuf,
    runtime_state_root: PathBuf,
    top_entries_limit: Option<usize>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
struct StorageScanRoot {
    root_kind: String,
    path: PathBuf,
}

#[derive(Debug, Clone, Serialize)]
struct StorageInventoryRecord {
    path: String,
    relative_path: String,
    root_kind: String,
    size_bytes: u64,
    is_symlink: bool,
}

#[derive(Debug, Clone, Serialize)]
struct StorageSummaryEntry {
    path: String,
    relative_path: String,
    storage_class: String,
    size_bytes: u64,
    provider: Option<String>,
    agent: Option<String>,
    active: Option<bool>,
    is_active_version: Option<bool>,
    reachable_from_current_symlink: Option<bool>,
    reclaimable: Option<bool>,
    reason: Option<String>,
    root_kind: String,
}

#[derive(Debug, Clone, Serialize)]
struct SummaryBucket {
    bytes: u64,
    count: u64,
}

#[derive(Debug, Serialize, PartialEq)]
struct NativeOutputObservation {
    text: String,
    finished: bool,
    finish_reason: String,
    turn_ref: Option<String>,
    completed_at: Option<Value>,
    error: String,
    intermediate: bool,
}

#[derive(Debug, Serialize)]
struct ErrorBody {
    kind: &'static str,
    message: String,
}

#[derive(Debug, Serialize)]
struct ErrorEnvelope {
    schema_version: u32,
    ok: bool,
    helper: &'static str,
    error: ErrorBody,
}

fn main() {
    if let Err(err) = run() {
        emit_error(err.kind, &err.message, err.exit_code);
    }
}

fn run() -> Result<(), HelperFailure> {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        Some("--version") => {
            print_json(&json!({
                "schema_version": SCHEMA_VERSION,
                "helper": HELPER_NAME,
                "version": HELPER_VERSION,
            }));
            Ok(())
        }
        Some("--capabilities") => {
            let capabilities = [
                CONTRACT_ECHO,
                NATIVE_OUTPUT_OBSERVE,
                STORAGE_SCAN_INVENTORY,
                STORAGE_SCAN_SUMMARY,
            ];
            print_json(&json!({
                "schema_version": SCHEMA_VERSION,
                "helper": HELPER_NAME,
                "version": HELPER_VERSION,
                "capabilities": capabilities,
            }));
            Ok(())
        }
        Some("--help") | Some("-h") => {
            print_usage();
            Ok(())
        }
        Some(flag) => Err(HelperFailure::new(
            "invalid_args",
            format!("unsupported argument: {flag}"),
            2,
        )),
        None => {
            let mut stdin = String::new();
            io::stdin()
                .read_to_string(&mut stdin)
                .map_err(|err| HelperFailure::new("stdin_read_failed", err.to_string(), 1))?;
            let request: HelperRequest = serde_json::from_str(&stdin)
                .map_err(|err| HelperFailure::new("invalid_request", err.to_string(), 2))?;
            let response = handle_request(request)?;
            print_json(&response);
            Ok(())
        }
    }
}

fn handle_request(request: HelperRequest) -> Result<Value, HelperFailure> {
    let capability = request.capability.clone();
    let payload = match capability.as_str() {
        CONTRACT_ECHO => request.payload,
        NATIVE_OUTPUT_OBSERVE => {
            let payload: NativeOutputPayload = serde_json::from_value(request.payload)
                .map_err(|err| HelperFailure::new("invalid_payload", err.to_string(), 2))?;
            json!(observe_native_output(&payload.path))
        }
        STORAGE_SCAN_INVENTORY => {
            let payload: StorageScanPayload = serde_json::from_value(request.payload)
                .map_err(|err| HelperFailure::new("invalid_payload", err.to_string(), 2))?;
            json!(scan_storage_inventory(&payload.roots))
        }
        STORAGE_SCAN_SUMMARY => {
            let payload: StorageSummaryPayload = serde_json::from_value(request.payload)
                .map_err(|err| HelperFailure::new("invalid_payload", err.to_string(), 2))?;
            scan_storage_summary(&payload)
        }
        _ => {
            return Err(HelperFailure::new(
                "unsupported_capability",
                format!("unsupported capability: {}", request.capability),
                2,
            ));
        }
    };
    Ok(json!({
        "schema_version": SCHEMA_VERSION,
        "ok": true,
        "capability": capability,
        "payload": payload,
    }))
}

fn scan_storage_inventory(roots: &[StorageScanRoot]) -> Vec<StorageInventoryRecord> {
    let mut records = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for root in roots {
        if !root.path.exists() {
            continue;
        }
        walk_storage_root(
            &root.path,
            &root.path,
            &root.root_kind,
            &mut seen,
            &mut records,
        );
    }
    records
}

fn walk_storage_root(
    root: &Path,
    current: &Path,
    root_kind: &str,
    seen: &mut std::collections::HashSet<String>,
    records: &mut Vec<StorageInventoryRecord>,
) {
    let Ok(read_dir) = std::fs::read_dir(current) else {
        return;
    };
    let mut dirs: Vec<PathBuf> = Vec::new();
    for entry in read_dir.flatten() {
        let path = entry.path();
        let Ok(metadata) = std::fs::symlink_metadata(&path) else {
            continue;
        };
        let file_type = metadata.file_type();
        if file_type.is_dir() && !file_type.is_symlink() {
            dirs.push(path);
            continue;
        }
        let identity = scan_identity(&path, &metadata);
        if !seen.insert(identity) {
            continue;
        }
        records.push(StorageInventoryRecord {
            relative_path: relative_display(root, &path),
            path: path.to_string_lossy().into_owned(),
            root_kind: root_kind.to_string(),
            size_bytes: metadata.len(),
            is_symlink: file_type.is_symlink(),
        });
    }
    dirs.sort();
    for dir in dirs {
        walk_storage_root(root, &dir, root_kind, seen, records);
    }
}

#[cfg(unix)]
fn scan_identity(_path: &Path, metadata: &std::fs::Metadata) -> String {
    use std::os::unix::fs::MetadataExt;
    format!("inode:{}:{}", metadata.dev(), metadata.ino())
}

#[cfg(not(unix))]
fn scan_identity(path: &Path, _metadata: &std::fs::Metadata) -> String {
    format!("path:{}", path.to_string_lossy())
}

fn relative_display(root: &Path, path: &Path) -> String {
    path.strip_prefix(root)
        .map(|relative| relative.to_string_lossy().into_owned())
        .unwrap_or_else(|_| path.to_string_lossy().into_owned())
}

fn scan_storage_summary(payload: &StorageSummaryPayload) -> Value {
    let mut entries: Vec<StorageSummaryEntry> = scan_storage_inventory(&payload.roots)
        .iter()
        .map(|record| classify_storage_record(payload, record))
        .collect();
    let total_bytes = entries.iter().map(|entry| entry.size_bytes).sum::<u64>();
    let total_count = entries.len() as u64;
    let mut by_class: BTreeMap<String, SummaryBucket> = BTreeMap::new();
    let mut by_provider: BTreeMap<String, SummaryBucket> = BTreeMap::new();
    let mut by_agent: BTreeMap<String, SummaryBucket> = BTreeMap::new();
    for entry in &entries {
        accumulate_summary_bucket(&mut by_class, &entry.storage_class, entry.size_bytes);
        if let Some(provider) = &entry.provider {
            accumulate_summary_bucket(&mut by_provider, provider, entry.size_bytes);
        }
        if let Some(agent) = &entry.agent {
            accumulate_summary_bucket(&mut by_agent, agent, entry.size_bytes);
        }
    }
    entries.sort_by(|left, right| {
        right
            .size_bytes
            .cmp(&left.size_bytes)
            .then_with(|| left.relative_path.cmp(&right.relative_path))
    });
    if let Some(limit) = payload.top_entries_limit {
        entries.truncate(limit);
    }
    json!({
        "total_bytes": total_bytes,
        "total_count": total_count,
        "by_class": by_class,
        "by_provider": by_provider,
        "by_agent": by_agent,
        "entries": entries,
    })
}

fn accumulate_summary_bucket(
    buckets: &mut BTreeMap<String, SummaryBucket>,
    key: &str,
    size_bytes: u64,
) {
    let bucket = buckets
        .entry(key.to_string())
        .or_insert(SummaryBucket { bytes: 0, count: 0 });
    bucket.bytes += size_bytes;
    bucket.count += 1;
}

fn classify_storage_record(
    payload: &StorageSummaryPayload,
    record: &StorageInventoryRecord,
) -> StorageSummaryEntry {
    let parts = relative_parts(&record.relative_path);
    if record.is_symlink
        && !is_allowed_provider_secret_symlink(&parts)
        && !is_marked_projected_symlink(Path::new(&record.path))
    {
        if let Some(reason) = unsafe_symlink_reason(
            Path::new(&record.path),
            &payload.ccb_dir,
            &payload.runtime_state_root,
        ) {
            return storage_entry(
                record,
                "unknown",
                None,
                None,
                None,
                None,
                None,
                None,
                Some(&reason),
            );
        }
    }
    classify_storage_relative(record, &parts)
}

fn classify_storage_relative(
    record: &StorageInventoryRecord,
    parts: &[String],
) -> StorageSummaryEntry {
    if parts.is_empty() {
        return storage_entry(record, "unknown", None, None, None, None, None, None, None);
    }
    let first = parts[0].as_str();
    if first == "ccb.config" {
        return storage_entry(
            record,
            "authority",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        );
    }
    if first == "ccb_memory.md" {
        return storage_entry(
            record,
            "user_content",
            None,
            None,
            None,
            None,
            None,
            None,
            Some("project_shared_memory"),
        );
    }
    if first == "runtime-root.json" || first == "runtime-root-ref.json" {
        return storage_entry(
            record,
            "authority",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        );
    }
    if first.starts_with('.') && first.ends_with("-session") {
        return provider_session_file_entry(record, first);
    }
    if parts.len() >= 2 && first == "ccbd" {
        return classify_ccbd_record(record, parts);
    }
    if parts.len() >= 3 && first == "agents" {
        return classify_agent_record(record, parts);
    }
    if parts.len() >= 3 && first == "provider-profiles" {
        return classify_provider_home_record(record, &parts[2], &parts[1], &parts[3..]);
    }
    if parts.len() == 2 && first == "state" && parts[1] == "memory.seed.json" {
        return storage_entry(
            record,
            "authority",
            None,
            None,
            None,
            None,
            None,
            None,
            Some("project_memory_seed"),
        );
    }
    if parts.len() == 3 && first == "runtime" && parts[1] == "memory" && parts[2].ends_with(".md") {
        let agent = parts[2].trim_end_matches(".md");
        return storage_entry(
            record,
            "runtime_ephemeral",
            None,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("project_memory_bundle"),
        );
    }
    if parts.len() >= 5 && first == "runtime" && parts[1] == "skills" {
        return storage_entry(
            record,
            "projected_config",
            Some(&parts[3]),
            Some(&parts[2]),
            None,
            None,
            None,
            None,
            Some("provider_skill_instruction"),
        );
    }
    if parts.len() >= 2 && first == "shared-cache" {
        return storage_entry(
            record,
            "rebuildable_cache",
            Some(&parts[1]),
            None,
            None,
            None,
            None,
            Some(false),
            Some("shared_cache"),
        );
    }
    if first == "workspaces" {
        return storage_entry(
            record,
            "workspace",
            None,
            None,
            None,
            None,
            None,
            None,
            Some("agent_workspace"),
        );
    }
    if first == "history" {
        return storage_entry(
            record,
            "user_content",
            None,
            None,
            None,
            None,
            None,
            None,
            Some("project_history"),
        );
    }
    storage_entry(record, "unknown", None, None, None, None, None, None, None)
}

fn classify_ccbd_record(record: &StorageInventoryRecord, parts: &[String]) -> StorageSummaryEntry {
    let Some(name) = parts.last() else {
        return storage_entry(record, "unknown", None, None, None, None, None, None, None);
    };
    let top = parts.get(1).map(String::as_str).unwrap_or("");
    if parts.len() == 2 && ccbd_authority_file(name) {
        return storage_entry(
            record,
            "authority",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        );
    }
    if ccbd_runtime_dir(top)
        || name.ends_with(".pid")
        || name.ends_with(".sock")
        || name.ends_with(".lock")
    {
        return storage_entry(
            record,
            "runtime_ephemeral",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        );
    }
    if matches!(
        top,
        "mailboxes" | "messages" | "attempts" | "replies" | "executions" | "snapshots"
    ) {
        return storage_entry(
            record,
            "authority",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        );
    }
    if name.ends_with(".jsonl") || name.ends_with(".log") {
        return storage_entry(
            record,
            "authority",
            None,
            None,
            None,
            None,
            None,
            None,
            Some("ccbd_event_log"),
        );
    }
    if name.ends_with(".json") {
        return storage_entry(
            record,
            "authority",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        );
    }
    storage_entry(record, "unknown", None, None, None, None, None, None, None)
}

fn classify_agent_record(record: &StorageInventoryRecord, parts: &[String]) -> StorageSummaryEntry {
    let agent = parts[1].as_str();
    let Some(name) = parts.last().map(String::as_str) else {
        return storage_entry(
            record,
            "unknown",
            None,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    };
    if parts.len() == 3 && agent_authority_file(name) {
        return storage_entry(
            record,
            "authority",
            None,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if parts.len() == 3 && name == "memory.md" {
        return storage_entry(
            record,
            "user_content",
            None,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("agent_private_memory"),
        );
    }
    if parts.len() == 3 && name.ends_with(".jsonl") {
        return storage_entry(
            record,
            "authority",
            None,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("agent_event_log"),
        );
    }
    if parts.len() >= 4 && parts[2] == "provider-runtime" {
        return storage_entry(
            record,
            "runtime_ephemeral",
            Some(&parts[3]),
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if parts.len() >= 5 && parts[2] == "provider-state" {
        let provider = parts[3].as_str();
        let remainder = if parts[4] == "home" {
            &parts[5..]
        } else {
            &parts[4..]
        };
        return classify_provider_home_record(record, provider, agent, remainder);
    }
    if parts.len() >= 3 && parts[2] == "logs" {
        return storage_entry(
            record,
            "runtime_ephemeral",
            None,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("agent_log"),
        );
    }
    storage_entry(
        record,
        "unknown",
        None,
        Some(agent),
        None,
        None,
        None,
        None,
        None,
    )
}

fn classify_provider_home_record(
    record: &StorageInventoryRecord,
    provider: &str,
    agent: &str,
    remainder: &[String],
) -> StorageSummaryEntry {
    let provider = provider.trim().to_ascii_lowercase();
    let provider_ref = if provider.is_empty() {
        None
    } else {
        Some(provider.as_str())
    };
    if remainder.is_empty() {
        return storage_entry(
            record,
            "projected_config",
            provider_ref,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    let name = remainder.last().map(String::as_str).unwrap_or("");
    if provider_secret_filename(name) {
        return storage_entry(
            record,
            "secret",
            provider_ref,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("provider_secret"),
        );
    }
    if name.ends_with(".ccb-projection.json") {
        return storage_entry(
            record,
            "projected_config",
            provider_ref,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("projected_asset_marker"),
        );
    }
    match provider.as_str() {
        "codex" => classify_codex_home(record, provider_ref, agent, remainder),
        "claude" => classify_claude_home(record, provider_ref, agent, remainder),
        "gemini" => classify_gemini_home(record, provider_ref, agent, remainder),
        "opencode" => classify_opencode_home(record, provider_ref, agent, remainder),
        "kimi" => classify_kimi_home(record, provider_ref, agent, remainder),
        "mimo" => classify_mimo_home(record, provider_ref, agent, remainder),
        "droid" => classify_droid_home(record, provider_ref, agent, remainder),
        "qwen" | "cursor" | "copilot" | "crush" | "kiro" | "pi" => {
            classify_native_cli_home(record, provider_ref, agent, remainder)
        }
        _ => storage_entry(
            record,
            "unknown",
            provider_ref,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        ),
    }
}

fn classify_codex_home(
    record: &StorageInventoryRecord,
    provider: Option<&str>,
    agent: &str,
    remainder: &[String],
) -> StorageSummaryEntry {
    let first = remainder.first().map(String::as_str).unwrap_or("");
    let name = remainder.last().map(String::as_str).unwrap_or("");
    if first == "sessions"
        || codex_session_name(name)
        || matches!(first, "log" | "logs" | "shell_snapshots")
    {
        return storage_entry(
            record,
            "session",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if first == ".tmp" && remainder.get(1).map(String::as_str) == Some("plugins") {
        return storage_entry(
            record,
            "startup_authority_bundle",
            provider,
            Some(agent),
            None,
            None,
            None,
            Some(false),
            Some("codex_plugin_bundle"),
        );
    }
    if first == ".tmp" && name == "plugins.sha" {
        return storage_entry(
            record,
            "startup_authority_bundle",
            provider,
            Some(agent),
            None,
            None,
            None,
            Some(false),
            Some("codex_plugin_bundle_manifest"),
        );
    }
    if name == "config.toml" || matches!(first, "skills" | "commands") {
        return storage_entry(
            record,
            "projected_config",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if matches!(first, ".tmp" | ".cache") {
        return storage_entry(
            record,
            "rebuildable_cache",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    storage_entry(
        record,
        "unknown",
        provider,
        Some(agent),
        None,
        None,
        None,
        None,
        None,
    )
}

fn classify_claude_home(
    record: &StorageInventoryRecord,
    provider: Option<&str>,
    agent: &str,
    remainder: &[String],
) -> StorageSummaryEntry {
    let first = remainder.first().map(String::as_str).unwrap_or("");
    let name = remainder.last().map(String::as_str).unwrap_or("");
    if name == ".claude.json" {
        return storage_entry(
            record,
            "session",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("claude_trust_authority"),
        );
    }
    if remainder.starts_with(&["Library".to_string(), "Keychains".to_string()]) {
        return storage_entry(
            record,
            "secret",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("macos_keychain_link"),
        );
    }
    if remainder.starts_with(&[
        ".local".to_string(),
        "share".to_string(),
        "claude".to_string(),
        "versions".to_string(),
    ]) {
        let is_active_version = claude_version_active(Path::new(&record.path), remainder);
        return storage_entry(
            record,
            "rebuildable_cache",
            provider,
            Some(agent),
            Some(false),
            Some(is_active_version),
            Some(is_active_version),
            if is_active_version { Some(false) } else { None },
            Some(if is_active_version {
                "active_claude_version_cache"
            } else {
                "claude_version_cache"
            }),
        );
    }
    if first == ".local" && remainder.get(1).map(String::as_str) == Some("bin") && name == "claude"
    {
        return storage_entry(
            record,
            "rebuildable_cache",
            provider,
            Some(agent),
            Some(true),
            Some(false),
            Some(true),
            Some(false),
            Some("claude_current_binary_link"),
        );
    }
    if first == ".claude"
        && matches!(
            remainder.get(1).map(String::as_str),
            Some("projects" | "session-env" | "tasks")
        )
    {
        return storage_entry(
            record,
            "session",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if first == ".claude"
        && (matches!(name, "settings.json" | "CLAUDE.md")
            || matches!(
                remainder.get(1).map(String::as_str),
                Some("skills" | "commands")
            ))
    {
        return storage_entry(
            record,
            "projected_config",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if matches!(first, ".cache" | ".npm") {
        return storage_entry(
            record,
            "rebuildable_cache",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    storage_entry(
        record,
        "unknown",
        provider,
        Some(agent),
        None,
        None,
        None,
        None,
        None,
    )
}

fn classify_gemini_home(
    record: &StorageInventoryRecord,
    provider: Option<&str>,
    agent: &str,
    remainder: &[String],
) -> StorageSummaryEntry {
    let first = remainder.first().map(String::as_str).unwrap_or("");
    let name = remainder.last().map(String::as_str).unwrap_or("");
    if first == ".gemini" && remainder.get(1).map(String::as_str) == Some("tmp") {
        return storage_entry(
            record,
            "session",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if first == ".gemini" && matches!(name, "settings.json" | "trustedFolders.json") {
        return storage_entry(
            record,
            "projected_config",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if first == ".npm" {
        return storage_entry(
            record,
            "rebuildable_cache",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("npm_cache"),
        );
    }
    if first == ".cache"
        && matches!(
            remainder.get(1).map(String::as_str),
            Some("node-gyp" | "vscode-ripgrep")
        )
    {
        return storage_entry(
            record,
            "rebuildable_cache",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("tool_cache"),
        );
    }
    if first == ".gemini" {
        return storage_entry(
            record,
            "session",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    storage_entry(
        record,
        "unknown",
        provider,
        Some(agent),
        None,
        None,
        None,
        None,
        None,
    )
}

fn classify_opencode_home(
    record: &StorageInventoryRecord,
    provider: Option<&str>,
    agent: &str,
    remainder: &[String],
) -> StorageSummaryEntry {
    let first = remainder.first().map(String::as_str).unwrap_or("");
    let name = remainder.last().map(String::as_str).unwrap_or("");
    if name == "opencode.json" {
        return storage_entry(
            record,
            "projected_config",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if matches!(first, ".cache" | ".tmp") {
        return storage_entry(
            record,
            "rebuildable_cache",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    storage_entry(
        record,
        "unknown",
        provider,
        Some(agent),
        None,
        None,
        None,
        None,
        None,
    )
}

fn classify_kimi_home(
    record: &StorageInventoryRecord,
    provider: Option<&str>,
    agent: &str,
    remainder: &[String],
) -> StorageSummaryEntry {
    let first = remainder.first().map(String::as_str).unwrap_or("");
    if matches!(first, "inherited-skills" | "role-skills") {
        return storage_entry(
            record,
            "projected_config",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    storage_entry(
        record,
        "unknown",
        provider,
        Some(agent),
        None,
        None,
        None,
        None,
        None,
    )
}

fn classify_mimo_home(
    record: &StorageInventoryRecord,
    provider: Option<&str>,
    agent: &str,
    remainder: &[String],
) -> StorageSummaryEntry {
    let first = remainder.first().map(String::as_str).unwrap_or("");
    let name = remainder.last().map(String::as_str).unwrap_or("");
    if name == "mimocode.json" {
        return storage_entry(
            record,
            "projected_config",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if matches!(first, "data" | "state") {
        return storage_entry(
            record,
            "session",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if first == "cache" {
        return storage_entry(
            record,
            "rebuildable_cache",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    storage_entry(
        record,
        "unknown",
        provider,
        Some(agent),
        None,
        None,
        None,
        None,
        None,
    )
}

fn classify_droid_home(
    record: &StorageInventoryRecord,
    provider: Option<&str>,
    agent: &str,
    remainder: &[String],
) -> StorageSummaryEntry {
    let first = remainder.first().map(String::as_str).unwrap_or("");
    if first == "sessions" {
        return storage_entry(
            record,
            "session",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if first == "skills" {
        return storage_entry(
            record,
            "projected_config",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    storage_entry(
        record,
        "unknown",
        provider,
        Some(agent),
        None,
        None,
        None,
        None,
        None,
    )
}

fn classify_native_cli_home(
    record: &StorageInventoryRecord,
    provider: Option<&str>,
    agent: &str,
    remainder: &[String],
) -> StorageSummaryEntry {
    let first = remainder.first().map(String::as_str).unwrap_or("");
    let name = remainder.last().map(String::as_str).unwrap_or("");
    if matches!(first, "inherited-skills" | "role-skills") {
        return storage_entry(
            record,
            "projected_config",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if matches!(
        first,
        ".cache" | ".npm" | ".tmp" | "cache" | "node_modules" | "tmp"
    ) {
        return storage_entry(
            record,
            "rebuildable_cache",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            None,
        );
    }
    if matches!(
        first,
        ".config"
            | ".crush"
            | ".cursor"
            | ".kiro"
            | ".local"
            | ".pi"
            | ".qwen"
            | "data"
            | "logs"
            | "session"
            | "sessions"
            | "state"
    ) || name.ends_with(".db")
        || name.ends_with(".jsonl")
        || name.ends_with(".log")
        || name.ends_with(".sqlite")
        || name.ends_with(".sqlite-shm")
        || name.ends_with(".sqlite-wal")
    {
        return storage_entry(
            record,
            "session",
            provider,
            Some(agent),
            None,
            None,
            None,
            None,
            Some("native_cli_provider_state"),
        );
    }
    storage_entry(
        record,
        "session",
        provider,
        Some(agent),
        None,
        None,
        None,
        None,
        Some("native_cli_provider_owned_state"),
    )
}

fn storage_entry(
    record: &StorageInventoryRecord,
    storage_class: &str,
    provider: Option<&str>,
    agent: Option<&str>,
    active: Option<bool>,
    is_active_version: Option<bool>,
    reachable_from_current_symlink: Option<bool>,
    reclaimable: Option<bool>,
    reason: Option<&str>,
) -> StorageSummaryEntry {
    StorageSummaryEntry {
        path: record.path.clone(),
        relative_path: record.relative_path.clone(),
        storage_class: storage_class.to_string(),
        size_bytes: record.size_bytes,
        provider: provider.map(str::to_string),
        agent: agent.map(str::to_string),
        active,
        is_active_version,
        reachable_from_current_symlink,
        reclaimable,
        reason: reason.map(str::to_string),
        root_kind: record.root_kind.clone(),
    }
}

fn relative_parts(relative_path: &str) -> Vec<String> {
    Path::new(relative_path)
        .components()
        .filter_map(|component| match component {
            std::path::Component::Normal(part) => Some(part.to_string_lossy().into_owned()),
            _ => None,
        })
        .collect()
}

fn ccbd_authority_file(name: &str) -> bool {
    matches!(
        name,
        "keeper.json"
            | "lease.json"
            | "lifecycle.json"
            | "restore-report.json"
            | "shutdown-intent.json"
            | "shutdown-report.json"
            | "start-policy.json"
            | "startup-report.json"
            | "state.json"
    )
}

fn ccbd_runtime_dir(name: &str) -> bool {
    matches!(name, "heartbeats" | "leases" | "cursors")
}

fn agent_authority_file(name: &str) -> bool {
    matches!(
        name,
        "agent.json" | "runtime.json" | "helper.json" | "restore.json" | "provider.json"
    )
}

fn provider_secret_filename(name: &str) -> bool {
    matches!(
        name,
        ".credentials.json" | ".env" | "auth.json" | "google_accounts.json" | "oauth_creds.json"
    )
}

fn codex_session_name(name: &str) -> bool {
    matches!(
        name,
        ".ccb-session-namespace.json"
            | "history.jsonl"
            | "logs_2.sqlite"
            | "logs_2.sqlite-shm"
            | "logs_2.sqlite-wal"
            | "state_5.sqlite"
            | "state_5.sqlite-shm"
            | "state_5.sqlite-wal"
    )
}

fn provider_session_file_entry(
    record: &StorageInventoryRecord,
    filename: &str,
) -> StorageSummaryEntry {
    let provider_agent = filename.trim_matches('.');
    let parts: Vec<&str> = provider_agent.split('-').collect();
    let provider = parts.first().copied().filter(|value| !value.is_empty());
    let agent = if parts.len() >= 3 {
        parts.get(1).copied()
    } else {
        None
    };
    storage_entry(
        record, "session", provider, agent, None, None, None, None, None,
    )
}

fn unsafe_symlink_reason(path: &Path, ccb_dir: &Path, runtime_state_root: &Path) -> Option<String> {
    let Ok(target) = path.canonicalize() else {
        return Some("symlink_target_missing".to_string());
    };
    if path_within(&target, ccb_dir) || path_within(&target, runtime_state_root) {
        return None;
    }
    Some("symlink_out_of_bounds".to_string())
}

fn path_within(path: &Path, root: &Path) -> bool {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    path.starts_with(root)
}

fn is_allowed_provider_secret_symlink(parts: &[String]) -> bool {
    parts.len() >= 7
        && parts[0] == "agents"
        && parts[2] == "provider-state"
        && parts[3] == "claude"
        && parts[4] == "home"
        && parts[5] == "Library"
        && parts[6] == "Keychains"
}

fn is_marked_projected_symlink(path: &Path) -> bool {
    let marker_path = PathBuf::from(format!("{}.ccb-projection.json", path.to_string_lossy()));
    let Ok(text) = std::fs::read_to_string(marker_path) else {
        return false;
    };
    let Ok(payload) = serde_json::from_str::<Value>(&text) else {
        return false;
    };
    if payload.get("record_type").and_then(Value::as_str) != Some("ccb_projected_asset") {
        return false;
    }
    let label = payload.get("label").and_then(Value::as_str).unwrap_or("");
    if !projected_symlink_label_allowed(label) {
        return false;
    }
    let source = payload
        .get("source")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if source.is_empty() {
        return false;
    }
    let Ok(source_path) = Path::new(source).canonicalize() else {
        return false;
    };
    let Ok(target_path) = path.canonicalize() else {
        return false;
    };
    source_path == target_path
}

fn projected_symlink_label_allowed(label: &str) -> bool {
    matches!(
        label,
        "claude-binary-versions"
            | "claude-inherited-skills"
            | "claude-inherited-commands"
            | "codex-inherited-skills"
            | "codex-inherited-commands"
            | "codex-plugin-bundle"
            | "droid-inherited-skills"
            | "kimi-inherited-skills"
            | "mimo-inherited-skills"
    ) || label.starts_with("codex-role-skill:")
        || label.starts_with("claude-role-skill:")
        || label.starts_with("kimi-role-skill:")
}

fn claude_version_active(path: &Path, remainder: &[String]) -> bool {
    if remainder.len() < 6 {
        return false;
    }
    let Some(version) = remainder.get(4) else {
        return false;
    };
    let mut home = path.to_path_buf();
    for _ in remainder {
        if !home.pop() {
            return false;
        }
    }
    let link = home.join(".local").join("bin").join("claude");
    let version_root = home
        .join(".local")
        .join("share")
        .join("claude")
        .join("versions")
        .join(version);
    let Ok(target) = link.canonicalize() else {
        return false;
    };
    let version_root = version_root
        .canonicalize()
        .unwrap_or_else(|_| version_root.to_path_buf());
    target.starts_with(version_root)
}

fn observe_native_output(path: &Path) -> NativeOutputObservation {
    if !path.is_file() {
        return NativeOutputObservation::empty();
    }

    let bytes = match std::fs::read(path) {
        Ok(bytes) => bytes,
        Err(err) => {
            let mut observation = NativeOutputObservation::empty();
            observation.error = format!("read_stdout_failed:{err}");
            return observation;
        }
    };
    let content = String::from_utf8_lossy(&bytes);
    let mut chunks: Vec<String> = Vec::new();
    let mut finished = false;
    let mut finish_reason = String::new();
    let mut turn_ref: Option<String> = None;
    let mut completed_at: Option<Value> = None;
    let mut error = String::new();
    let mut intermediate = false;

    for line in content.lines() {
        let stripped = line.trim();
        if stripped.is_empty() {
            continue;
        }
        let Ok(event) = serde_json::from_str::<Value>(stripped) else {
            continue;
        };
        if !event.is_object() {
            continue;
        }
        if is_error_event(&event) {
            error = event_text(&event)
                .or_else(|| event_reason(&event))
                .unwrap_or_else(|| "native_cli_error".to_string());
            continue;
        }
        if is_tool_event(&event) {
            intermediate = true;
            if let Some(reason) = event_reason(&event) {
                if !reason.is_empty() {
                    finish_reason = reason;
                }
            }
            continue;
        }
        if let Some(text) = assistant_text(&event) {
            if !text.is_empty() {
                chunks.push(text);
                if turn_ref.is_none() {
                    turn_ref = event_ref(&event);
                }
                if completed_at.is_none() {
                    completed_at = event_time(&event);
                }
            }
        }
        if is_final_event(&event) {
            finished = true;
            finish_reason = event_reason(&event)
                .filter(|reason| !reason.is_empty())
                .unwrap_or_else(|| {
                    if finish_reason.is_empty() {
                        "completed".to_string()
                    } else {
                        finish_reason.clone()
                    }
                });
            if turn_ref.is_none() {
                turn_ref = event_ref(&event);
            }
            if completed_at.is_none() {
                completed_at = event_time(&event);
            }
        }
    }

    NativeOutputObservation {
        text: chunks.concat(),
        finished,
        finish_reason,
        turn_ref,
        completed_at,
        error,
        intermediate,
    }
}

impl NativeOutputObservation {
    fn empty() -> Self {
        Self {
            text: String::new(),
            finished: false,
            finish_reason: String::new(),
            turn_ref: None,
            completed_at: None,
            error: String::new(),
            intermediate: false,
        }
    }
}

fn assistant_text(event: &Value) -> Option<String> {
    if is_user_event(event) {
        return None;
    }
    if !(is_assistant_event(event) || is_final_event(event)) {
        return None;
    }
    event_text(event)
}

fn is_user_event(event: &Value) -> bool {
    nested_text_value(event, &["role", "sender", "author"])
        .unwrap_or_default()
        .trim()
        .eq_ignore_ascii_case("user")
}

fn is_assistant_event(event: &Value) -> bool {
    let role = nested_text_value(event, &["role", "sender", "author"])
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    if matches!(role.as_str(), "assistant" | "agent" | "model") {
        return true;
    }
    let event_type = event_type(event);
    [
        "assistant",
        "agent_message",
        "message_delta",
        "content_delta",
        "text",
    ]
    .iter()
    .any(|token| event_type.contains(token))
}

fn is_final_event(event: &Value) -> bool {
    if is_tool_event(event) {
        return false;
    }
    let haystack = [
        event_type(event),
        event_reason(event)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .replace('-', "_"),
        nested_text_value(event, &["status", "state"])
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .replace('-', "_"),
    ]
    .into_iter()
    .filter(|item| !item.is_empty())
    .collect::<Vec<_>>()
    .join(" ");
    if haystack.is_empty() {
        return false;
    }
    [
        "final",
        "result",
        "completion",
        "completed",
        "done",
        "finished",
        "turn_end",
        "end_turn",
    ]
    .iter()
    .any(|token| haystack.contains(token))
}

fn is_tool_event(event: &Value) -> bool {
    let haystack = [
        event_type(event),
        event_reason(event)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .replace('-', "_"),
        nested_text_value(event, &["role", "status", "state", "name"])
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .replace('-', "_"),
    ]
    .into_iter()
    .filter(|item| !item.is_empty())
    .collect::<Vec<_>>()
    .join(" ");
    haystack.contains("tool")
        || haystack.contains("permission")
        || haystack.contains("function_call")
}

fn is_error_event(event: &Value) -> bool {
    let haystack = [
        event_type(event),
        event_reason(event)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .replace('-', "_"),
        nested_text_value(event, &["status", "state"])
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase()
            .replace('-', "_"),
    ]
    .into_iter()
    .filter(|item| !item.is_empty())
    .collect::<Vec<_>>()
    .join(" ");
    [
        "error",
        "failed",
        "failure",
        "permission_denied",
        "unauthorized",
        "auth_failed",
    ]
    .iter()
    .any(|token| haystack.contains(token))
}

fn event_type(event: &Value) -> String {
    nested_text_value(event, &["type", "event", "kind", "name"])
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase()
        .replace('-', "_")
}

fn event_text(event: &Value) -> Option<String> {
    match event {
        Value::String(text) => Some(text.clone()).filter(|text| !text.is_empty()),
        Value::Array(items) => {
            let joined = items
                .iter()
                .filter_map(event_text)
                .collect::<Vec<_>>()
                .join("");
            Some(joined).filter(|text| !text.is_empty())
        }
        Value::Object(map) => {
            for key in [
                "merged_text",
                "final_answer",
                "answer",
                "reply",
                "text",
                "output",
                "response",
            ] {
                if let Some(value) = map.get(key) {
                    if let Some(text) = event_text(value) {
                        return Some(text);
                    }
                }
            }
            if let Some(value) = map.get("content") {
                if let Some(text) = event_text(value) {
                    return Some(text);
                }
            }
            for key in ["payload", "message", "delta", "part", "result", "data"] {
                if let Some(value) = map.get(key) {
                    if let Some(text) = event_text(value) {
                        return Some(text);
                    }
                }
            }
            None
        }
        _ => None,
    }
}

fn event_reason(event: &Value) -> Option<String> {
    let Some(map) = event.as_object() else {
        return None;
    };
    for key in ["reason", "finish_reason", "stop_reason", "status", "state"] {
        if let Some(Value::String(value)) = map.get(key) {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                return Some(trimmed.to_string());
            }
        }
    }
    for key in ["payload", "properties", "part", "message", "result", "data"] {
        if let Some(value) = map.get(key) {
            if let Some(reason) = event_reason(value) {
                return Some(reason);
            }
        }
    }
    None
}

fn event_ref(event: &Value) -> Option<String> {
    let Some(map) = event.as_object() else {
        return None;
    };
    for key in [
        "id",
        "message_id",
        "messageID",
        "session_id",
        "sessionID",
        "turn_id",
        "request_id",
    ] {
        if let Some(Value::String(value)) = map.get(key) {
            let trimmed = value.trim();
            if !trimmed.is_empty() {
                return Some(trimmed.to_string());
            }
        }
    }
    for key in ["payload", "message", "part", "result", "data"] {
        if let Some(value) = map.get(key) {
            if let Some(reference) = event_ref(value) {
                return Some(reference);
            }
        }
    }
    None
}

fn event_time(event: &Value) -> Option<Value> {
    let Some(map) = event.as_object() else {
        return None;
    };
    for key in [
        "completed_at",
        "time",
        "timestamp",
        "created_at",
        "updated_at",
    ] {
        if let Some(value) = map.get(key) {
            if !value.is_null() {
                return Some(value.clone());
            }
        }
    }
    for key in ["payload", "message", "part", "result", "data"] {
        if let Some(value) = map.get(key) {
            if let Some(time) = event_time(value) {
                return Some(time);
            }
        }
    }
    None
}

fn nested_text_value(event: &Value, keys: &[&str]) -> Option<String> {
    match event {
        Value::Array(items) => {
            for item in items {
                if let Some(value) = nested_text_value(item, keys) {
                    return Some(value);
                }
            }
            None
        }
        Value::Object(map) => {
            for key in keys {
                if let Some(Value::String(value)) = map.get(*key) {
                    if !value.is_empty() {
                        return Some(value.clone());
                    }
                }
            }
            for key in ["payload", "message", "part", "result", "data"] {
                if let Some(value) = map.get(key) {
                    if let Some(nested) = nested_text_value(value, keys) {
                        return Some(nested);
                    }
                }
            }
            None
        }
        _ => None,
    }
}

fn print_json(value: &Value) {
    println!(
        "{}",
        serde_json::to_string(value).expect("static helper envelope should serialize")
    );
}

fn print_usage() {
    println!(
        "{HELPER_NAME} {}\n\nUsage:\n  {HELPER_NAME} --version\n  {HELPER_NAME} --capabilities\n  {HELPER_NAME} < request.json",
        env!("CARGO_PKG_VERSION")
    );
}

fn emit_error(kind: &'static str, message: &str, exit_code: i32) -> ! {
    let envelope = ErrorEnvelope {
        schema_version: SCHEMA_VERSION,
        ok: false,
        helper: HELPER_NAME,
        error: ErrorBody {
            kind,
            message: message.to_string(),
        },
    };
    eprintln!(
        "{}",
        serde_json::to_string(&envelope).expect("static helper error envelope should serialize")
    );
    std::process::exit(exit_code);
}

#[derive(Debug)]
struct HelperFailure {
    kind: &'static str,
    message: String,
    exit_code: i32,
}

impl HelperFailure {
    fn new(kind: &'static str, message: String, exit_code: i32) -> Self {
        Self {
            kind,
            message,
            exit_code,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::Write;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    static TEMP_COUNTER: AtomicUsize = AtomicUsize::new(0);

    fn temp_test_dir(name: &str) -> PathBuf {
        let serial = TEMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!(
            "ccb_rs_helper_{name}_{}_{}_{}",
            std::process::id(),
            nanos,
            serial
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn capabilities_include_only_positive_storage_and_native_capabilities() {
        let capabilities = [
            CONTRACT_ECHO,
            NATIVE_OUTPUT_OBSERVE,
            STORAGE_SCAN_INVENTORY,
            STORAGE_SCAN_SUMMARY,
        ];
        assert_eq!(capabilities.len(), 4);
        assert!(capabilities.contains(&CONTRACT_ECHO));
        assert!(capabilities.contains(&NATIVE_OUTPUT_OBSERVE));
        assert!(capabilities.contains(&STORAGE_SCAN_INVENTORY));
        assert!(capabilities.contains(&STORAGE_SCAN_SUMMARY));
    }

    #[test]
    fn storage_scan_inventory_handles_files_symlinks_and_dedup() {
        let root = temp_test_dir("inventory");
        let nested = root.join("nested");
        std::fs::create_dir_all(&nested).unwrap();
        std::fs::write(root.join("a.txt"), b"abc").unwrap();
        std::fs::write(nested.join("b.txt"), b"hello").unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(root.join("a.txt"), root.join("a-link.txt")).unwrap();

        let payload = StorageScanPayload {
            roots: vec![StorageScanRoot {
                root_kind: "project".to_string(),
                path: root.clone(),
            }],
        };

        let records = scan_storage_inventory(&payload.roots);
        assert!(records.iter().any(|record| record.relative_path == "a.txt"));
        assert!(
            records
                .iter()
                .any(|record| record.relative_path == "nested/b.txt")
        );
        assert!(records.iter().all(|record| record.root_kind == "project"));
        assert!(records.iter().map(|record| record.size_bytes).sum::<u64>() >= 8);
        #[cfg(unix)]
        assert!(records.iter().any(|record| record.is_symlink));
    }

    #[test]
    fn storage_scan_summary_classifies_and_limits_entries() {
        let ccb_dir = temp_test_dir("summary");
        let ccbd = ccb_dir.join("ccbd");
        let agents = ccb_dir.join("agents/talk1/provider-state/codex/home/sessions");
        std::fs::create_dir_all(&ccbd).unwrap();
        std::fs::create_dir_all(&agents).unwrap();
        std::fs::write(ccb_dir.join("ccb.config"), b"agents = talk1").unwrap();
        std::fs::write(ccbd.join("state.json"), b"{}").unwrap();
        std::fs::write(agents.join("session.jsonl"), b"abcd").unwrap();

        let payload = StorageSummaryPayload {
            roots: vec![StorageScanRoot {
                root_kind: "project".to_string(),
                path: ccb_dir.clone(),
            }],
            ccb_dir: ccb_dir.clone(),
            runtime_state_root: ccb_dir.join("runtime-state"),
            top_entries_limit: Some(2),
        };

        let summary = scan_storage_summary(&payload);
        assert!(summary["total_bytes"].as_u64().unwrap() >= 6);
        assert_eq!(summary["entries"].as_array().unwrap().len(), 2);
        assert!(
            summary["by_class"]
                .as_object()
                .unwrap()
                .contains_key("authority")
        );
        assert!(
            summary["by_class"]
                .as_object()
                .unwrap()
                .contains_key("session")
        );
    }

    #[test]
    fn handle_request_returns_storage_scan_inventory_payload() {
        let root = temp_test_dir("handle_inventory");
        std::fs::write(root.join("a.txt"), b"abc").unwrap();

        let response = handle_request(HelperRequest {
            schema_version: Some(SCHEMA_VERSION),
            capability: STORAGE_SCAN_INVENTORY.to_string(),
            payload: json!({
                "roots": [{"root_kind": "project", "path": root}],
            }),
        })
        .unwrap();

        assert_eq!(response["schema_version"], SCHEMA_VERSION);
        assert_eq!(response["ok"], true);
        assert_eq!(response["capability"], STORAGE_SCAN_INVENTORY);
        assert_eq!(response["payload"][0]["root_kind"], "project");
        assert_eq!(response["payload"][0]["size_bytes"], 3);
    }

    #[test]
    fn handle_request_returns_storage_scan_summary_payload() {
        let ccb_dir = temp_test_dir("handle_summary");
        std::fs::write(ccb_dir.join("ccb.config"), b"agents = talk1").unwrap();

        let response = handle_request(HelperRequest {
            schema_version: Some(SCHEMA_VERSION),
            capability: STORAGE_SCAN_SUMMARY.to_string(),
            payload: json!({
                "roots": [{"root_kind": "project", "path": ccb_dir}],
                "ccb_dir": ccb_dir,
                "runtime_state_root": ccb_dir.join("runtime-state"),
                "top_entries_limit": 10,
            }),
        })
        .unwrap();

        assert_eq!(response["ok"], true);
        assert_eq!(response["capability"], STORAGE_SCAN_SUMMARY);
        assert!(response["payload"]["total_bytes"].as_u64().unwrap() > 0);
        assert!(
            response["payload"]["by_class"]
                .as_object()
                .unwrap()
                .contains_key("authority")
        );
    }

    #[test]
    fn native_output_observe_handles_final_tool_and_error_events() {
        let root = temp_test_dir("native");
        let path = root.join("events.jsonl");
        let mut file = std::fs::File::create(&path).unwrap();
        writeln!(
            file,
            "{}",
            json!({"type": "tool", "reason": "tool_call", "text": "ignore"})
        )
        .unwrap();
        writeln!(
            file,
            "{}",
            json!({"id": "2", "role": "assistant", "type": "message_delta", "text": "hello "})
        )
        .unwrap();
        writeln!(
            file,
            "{}",
            json!({"id": "3", "role": "assistant", "type": "final", "status": "completed", "text": "world", "completed_at": "now"})
        )
        .unwrap();
        writeln!(file, "{}", json!({"type": "error", "message": "boom"})).unwrap();

        let observation = observe_native_output(&path);
        assert_eq!(observation.text, "hello world");
        assert!(observation.finished);
        assert_eq!(observation.finish_reason, "completed");
        assert_eq!(observation.turn_ref.as_deref(), Some("2"));
        assert_eq!(observation.completed_at, Some(json!("now")));
        assert_eq!(observation.error, "boom");
        assert!(observation.intermediate);
    }

    #[test]
    fn native_output_observe_missing_file_returns_empty_observation() {
        let root = temp_test_dir("native_missing");
        let observation = observe_native_output(&root.join("missing.jsonl"));
        assert_eq!(observation, NativeOutputObservation::empty());
    }

    #[test]
    fn handle_request_returns_native_output_observation() {
        let root = temp_test_dir("handle_native");
        let path = root.join("events.jsonl");
        std::fs::write(
            &path,
            format!(
                "{}\n",
                json!({"id": "10", "role": "assistant", "type": "final", "text": "ok"})
            ),
        )
        .unwrap();

        let response = handle_request(HelperRequest {
            schema_version: Some(SCHEMA_VERSION),
            capability: NATIVE_OUTPUT_OBSERVE.to_string(),
            payload: json!({"path": path}),
        })
        .unwrap();

        assert_eq!(response["ok"], true);
        assert_eq!(response["capability"], NATIVE_OUTPUT_OBSERVE);
        assert_eq!(response["payload"]["finished"], true);
        assert_eq!(response["payload"]["text"], "ok");
        assert_eq!(response["payload"]["turn_ref"], "10");
    }
}
