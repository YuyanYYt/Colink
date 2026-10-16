//! Descriptor-relative source verification for one fixed private corpus root.
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::ffi::{CStr, CString};
use std::fs::File;
use std::io::Read;
use std::os::fd::{AsRawFd, FromRawFd, RawFd};
use std::os::unix::fs::MetadataExt;
use std::path::{Component, Path};

pub type Failure = &'static str;
pub const MAX_FILES: usize = 100_000;
pub const MAX_SOURCE_BYTES: u64 = 128 * 1024 * 1024;
pub const MAX_FILE_BYTES: u64 = 64 * 1024 * 1024;

#[derive(Clone)]
pub struct ExpectedFile {
    pub size: u64,
    pub hash: [u8; 32],
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Stamp {
    dev: u64,
    ino: u64,
    mode: u32,
    links: u64,
    size: u64,
    modified: (i64, i64),
    changed: (i64, i64),
}

impl Stamp {
    fn from_metadata(info: &std::fs::Metadata) -> Self {
        Self {
            dev: info.dev(),
            ino: info.ino(),
            mode: info.mode(),
            links: info.nlink(),
            size: info.size(),
            modified: (info.mtime(), info.mtime_nsec()),
            changed: (info.ctime(), info.ctime_nsec()),
        }
    }

    fn from_stat(info: &libc::stat) -> Self {
        Self {
            dev: info.st_dev as u64,
            ino: info.st_ino as u64,
            mode: info.st_mode as u32,
            links: info.st_nlink as u64,
            size: info.st_size as u64,
            modified: (info.st_mtime as i64, info.st_mtime_nsec as i64),
            changed: (info.st_ctime as i64, info.st_ctime_nsec as i64),
        }
    }

    fn identity(&self) -> (u64, u64, u32) {
        (self.dev, self.ino, self.mode)
    }
}

fn metadata(file: &File) -> Result<Stamp, Failure> {
    file.metadata()
        .map(|info| Stamp::from_metadata(&info))
        .map_err(|_| "source_changed")
}

fn stat_at(parent: RawFd, name: &CStr) -> Result<Stamp, Failure> {
    let mut info = std::mem::MaybeUninit::<libc::stat>::uninit();
    // SAFETY: parent is an owned live descriptor, name is NUL-terminated, and
    // fstatat initializes the output only on the success branch below.
    let status = unsafe {
        libc::fstatat(parent, name.as_ptr(), info.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW)
    };
    if status != 0 {
        return Err("source_changed");
    }
    Ok(Stamp::from_stat(&unsafe { info.assume_init() }))
}

fn open_at(parent: RawFd, name: &CStr, directory: bool) -> Result<File, Failure> {
    let flags = libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC
        | if directory { libc::O_DIRECTORY } else { 0 };
    // SAFETY: only a single validated path component is accepted. The returned
    // descriptor is immediately moved into File, which closes it exactly once.
    let descriptor = unsafe { libc::openat(parent, name.as_ptr(), flags) };
    if descriptor < 0 {
        return Err("source_changed");
    }
    Ok(unsafe { File::from_raw_fd(descriptor) })
}

struct Directory(*mut libc::DIR);

fn errno_address() -> *mut libc::c_int {
    #[cfg(target_os = "macos")]
    { unsafe { libc::__error() } }
    #[cfg(target_os = "linux")]
    { unsafe { libc::__errno_location() } }
}

impl Drop for Directory {
    fn drop(&mut self) {
        // SAFETY: Directory exclusively owns the successful fdopendir result.
        unsafe { libc::closedir(self.0) };
    }
}

fn directory_names(file: &File) -> Result<Vec<String>, Failure> {
    // SAFETY: fcntl duplicates a live descriptor; fdopendir assumes ownership
    // only on success, and the failure branch closes the duplicate itself.
    let descriptor = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
    if descriptor < 0 {
        return Err("source_changed");
    }
    let pointer = unsafe { libc::fdopendir(descriptor) };
    if pointer.is_null() {
        unsafe { libc::close(descriptor) };
        return Err("source_changed");
    }
    let directory = Directory(pointer);
    let mut names = Vec::new();
    loop {
        // SAFETY: the DIR pointer stays owned throughout iteration. d_name is
        // copied before calling readdir again, which may reuse the same memory.
        unsafe { *errno_address() = 0; }
        let entry = unsafe { libc::readdir(directory.0) };
        if entry.is_null() {
            if unsafe { *errno_address() } != 0 { return Err("source_changed"); }
            break;
        }
        let value = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
        if value.to_bytes() == b"." || value.to_bytes() == b".." {
            continue;
        }
        let name = value.to_str().map_err(|_| "source_unavailable")?;
        names.push(name.to_owned());
        if names.len() > MAX_FILES * 4 {
            return Err("source_budget");
        }
    }
    names.sort();
    Ok(names)
}

pub fn relative_path(value: &str) -> Result<(), Failure> {
    if value.is_empty() || value.len() > 4096 || value.starts_with('/')
        || value.contains(['\\', ':'])
        || value.chars().any(|ch| (ch as u32) < 32 || ch == '\u{7f}')
        || value.split('/').any(|part| matches!(part, "" | "." | ".."))
    {
        return Err("invalid_path");
    }
    Ok(())
}

pub fn source_path(value: &str) -> bool {
    value.ends_with(".py") || value.ends_with(".java")
}

pub struct CorpusGuard {
    components: Vec<CString>,
    root_chain: Vec<(u64, u64, u32)>,
    strict: bool,
    cache: HashMap<String, (Stamp, [u8; 32])>,
}

impl CorpusGuard {
    pub fn new(root: &Path, mode: &str) -> Result<Self, Failure> {
        if !root.is_absolute() || !matches!(mode, "metadata" | "strict") {
            return Err("invalid_root");
        }
        let mut components = Vec::new();
        for part in root.components() {
            match part {
                Component::RootDir => {}
                Component::Normal(name) => {
                    use std::os::unix::ffi::OsStrExt;
                    components.push(CString::new(name.as_bytes()).map_err(|_| "invalid_root")?);
                }
                _ => return Err("invalid_root"),
            }
        }
        let mut guard = Self {
            components,
            root_chain: Vec::new(),
            strict: mode == "strict",
            cache: HashMap::new(),
        };
        let opened = guard.open_root()?;
        guard.root_chain = opened.iter().map(metadata).collect::<Result<Vec<_>, _>>()?
            .iter().map(Stamp::identity).collect();
        guard.check_root_links(&opened)?;
        Ok(guard)
    }

    fn open_root(&self) -> Result<Vec<File>, Failure> {
        let slash = c"/";
        let mut chain = vec![open_at(libc::AT_FDCWD, slash, true)?];
        for part in &self.components {
            let parent = chain.last().ok_or("invalid_root")?;
            chain.push(open_at(parent.as_raw_fd(), part, true).map_err(|_| {
                if self.root_chain.is_empty() { "source_unavailable" } else { "root_changed" }
            })?);
        }
        if !self.root_chain.is_empty() {
            let actual = chain.iter().map(metadata).collect::<Result<Vec<_>, _>>()?
                .iter().map(Stamp::identity).collect::<Vec<_>>();
            if actual != self.root_chain {
                return Err("root_changed");
            }
        }
        Ok(chain)
    }

    fn check_root_links(&self, chain: &[File]) -> Result<(), Failure> {
        for (index, name) in self.components.iter().enumerate() {
            let actual = stat_at(chain[index].as_raw_fd(), name).map_err(|_| "root_changed")?;
            if actual.identity() != metadata(&chain[index + 1])?.identity() {
                return Err("root_changed");
            }
        }
        Ok(())
    }

    pub fn verify(&mut self, expected: &BTreeMap<String, ExpectedFile>) -> Result<(), Failure> {
        let chain = self.open_root()?;
        let root = chain.last().ok_or("invalid_root")?;
        let mut observed = HashMap::with_capacity(expected.len());
        let mut visited = 0;
        let mut bytes = 0;
        self.walk(root, "", 0, expected, &mut observed, &mut visited, &mut bytes)?;
        self.check_root_links(&chain)?;
        if observed.len() != expected.len() {
            return Err("inventory_changed");
        }
        self.cache = observed;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn walk(
        &self,
        parent: &File,
        prefix: &str,
        depth: usize,
        expected: &BTreeMap<String, ExpectedFile>,
        observed: &mut HashMap<String, (Stamp, [u8; 32])>,
        visited: &mut usize,
        bytes: &mut u64,
    ) -> Result<(), Failure> {
        if depth > 128 {
            return Err("source_budget");
        }
        let before = metadata(parent)?;
        for name in directory_names(parent)? {
            *visited += 1;
            if *visited > MAX_FILES * 4 {
                return Err("source_budget");
            }
            let path = if prefix.is_empty() { name.clone() } else { format!("{prefix}/{name}") };
            let name = CString::new(name).map_err(|_| "source_unavailable")?;
            let info = stat_at(parent.as_raw_fd(), &name)?;
            let kind = info.mode & libc::S_IFMT as u32;
            if kind == libc::S_IFLNK as u32 {
                return Err("symlink_rejected");
            }
            if kind == libc::S_IFDIR as u32 {
                let child = open_at(parent.as_raw_fd(), &name, true)?;
                if metadata(&child)? != info {
                    return Err("source_changed");
                }
                self.walk(&child, &path, depth + 1, expected, observed, visited, bytes)?;
                if stat_at(parent.as_raw_fd(), &name)? != info {
                    return Err("source_changed");
                }
            } else if source_path(&path) {
                let expected = expected.get(&path).ok_or("inventory_changed")?;
                if kind != libc::S_IFREG as u32 || info.links != 1 || info.size != expected.size {
                    return Err("source_changed");
                }
                *bytes += info.size;
                if *bytes > MAX_SOURCE_BYTES {
                    return Err("source_budget");
                }
                let cached = self.cache.get(&path).filter(|(stamp, _)| !self.strict && *stamp == info);
                let actual = if let Some((_, hash)) = cached {
                    *hash
                } else {
                    let mut file = open_at(parent.as_raw_fd(), &name, false)?;
                    if metadata(&file)? != info {
                        return Err("source_changed");
                    }
                    let mut hasher = Sha256::new();
                    let mut buffer = [0u8; 65_536];
                    let mut consumed = 0u64;
                    loop {
                        let count = file.read(&mut buffer).map_err(|_| "source_changed")?;
                        if count == 0 { break; }
                        consumed += count as u64;
                        if consumed > expected.size { return Err("source_changed"); }
                        hasher.update(&buffer[..count]);
                    }
                    if consumed != expected.size || metadata(&file)? != info {
                        return Err("source_changed");
                    }
                    hasher.finalize().into()
                };
                if actual != expected.hash || stat_at(parent.as_raw_fd(), &name)? != info {
                    return Err("source_changed");
                }
                observed.insert(path, (info, actual));
            }
        }
        if metadata(parent)? != before {
            return Err("source_changed");
        }
        Ok(())
    }
}
