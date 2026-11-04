//! Read literal SSH aliases without evaluating conditions or executing commands.

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Discovery {
    pub aliases: Vec<String>,
    pub warnings: Vec<String>,
}

pub fn is_alias(value: &str) -> bool {
    value
        .as_bytes()
        .first()
        .is_some_and(|c| c.is_ascii_alphanumeric() || *c == b'_')
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
}

fn words(input: &str) -> Result<Vec<String>, &'static str> {
    let mut result = Vec::new();
    let mut value = String::new();
    let mut quote = None;
    let mut started = false;
    let mut chars = input.chars();
    while let Some(c) = chars.next() {
        if c == '\\' && quote != Some('\'') {
            let next = chars.next().ok_or("trailing escape")?;
            if quote == Some('"') && next != '"' && next != '\\' {
                value.push('\\');
            }
            value.push(next);
            started = true;
        } else if let Some(q) = quote {
            if c == q {
                quote = None;
            } else {
                value.push(c);
            }
        } else if c == '#' {
            break;
        } else if c == '"' || c == '\'' {
            quote = Some(c);
            started = true;
        } else if c.is_whitespace() {
            if started {
                result.push(std::mem::take(&mut value));
            }
            started = false;
        } else {
            value.push(c);
            started = true;
        }
    }
    if quote.is_some() {
        return Err("unterminated quote");
    }
    if started {
        result.push(value);
    }
    if result.is_empty() {
        return Err("missing argument");
    }
    Ok(result)
}

fn expand_home(path: &Path, home: &Path) -> Result<PathBuf, &'static str> {
    let value = path.to_str().ok_or("path is not UTF-8")?;
    if value == "~" {
        return Ok(home.to_path_buf());
    }
    if let Some(rest) = value.strip_prefix("~/") {
        return Ok(home.join(rest));
    }
    // shortcut: named-user tildes need a passwd lookup; warn until that use case is requested.
    if value.starts_with('~') {
        return Err("named-user home expansion is unsupported");
    }
    Ok(path.to_path_buf())
}

fn include_paths(pattern: &Path) -> Result<Vec<PathBuf>, String> {
    let value = pattern.to_str().ok_or("path is not UTF-8")?;
    // OpenSSH uses shell wildcards: consecutive stars do not mean recursive traversal.
    let mut normalized = String::new();
    let mut last_star = false;
    for c in value.chars() {
        if c != '*' || !last_star {
            normalized.push(c);
        }
        last_star = c == '*';
    }
    let options = glob::MatchOptions {
        case_sensitive: true,
        require_literal_separator: true,
        require_literal_leading_dot: true,
    };
    let mut paths = glob::glob_with(&normalized, options)
        .map_err(|error| error.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| error.to_string())?;
    paths.sort();
    Ok(paths)
}

struct Reader<'a> {
    home: &'a Path,
    result: Discovery,
    seen: HashSet<String>,
    active: HashSet<PathBuf>,
}

impl Reader<'_> {
    fn warn(&mut self, location: &str, reason: impl std::fmt::Display) {
        self.result.warnings.push(format!(
            "{location}: {reason}; host list may be incomplete."
        ));
    }

    fn read(&mut self, path: &Path, mut unconditional: bool, depth: usize) -> bool {
        let path = match fs::canonicalize(path) {
            Ok(path) => path,
            Err(error) => {
                self.warn(
                    &path.display().to_string(),
                    format!("Cannot read SSH config ({error})"),
                );
                return unconditional;
            }
        };
        let location = path.display().to_string();
        if self.active.contains(&path) {
            self.warn(&location, "Include cycle skipped");
            return unconditional;
        }
        if depth > 16 {
            self.warn(&location, "Include depth limit reached");
            return unconditional;
        }
        let contents = match fs::read_to_string(&path) {
            Ok(contents) => contents,
            Err(error) => {
                self.warn(&location, format!("Cannot read SSH config ({error})"));
                return unconditional;
            }
        };
        self.active.insert(path.clone());
        let contents = contents.replace("\r\n", "\n").replace('\r', "\n");
        for (index, line) in contents.lines().enumerate() {
            let line = line.trim_start();
            let end = line
                .find(|c: char| c.is_whitespace() || c == '=' || c == '#')
                .unwrap_or(line.len());
            let keyword = line[..end].to_ascii_lowercase();
            if !matches!(keyword.as_str(), "host" | "match" | "include") {
                continue;
            }
            let mut arguments = line[end..].trim_start();
            if let Some(rest) = arguments.strip_prefix('=') {
                arguments = rest.trim_start();
            }
            let location = format!("{}:{}", path.display(), index + 1);
            let values = match words(arguments) {
                Ok(values) => values,
                Err(error) => {
                    self.warn(&location, format!("Invalid {keyword} ({error})"));
                    if keyword != "include" {
                        unconditional = false;
                    }
                    continue;
                }
            };
            match keyword.as_str() {
                "host" => {
                    unconditional = values.len() == 1 && values[0] == "*";
                    for alias in values {
                        if alias.contains(['*', '?', '!']) {
                            continue;
                        }
                        if !is_alias(&alias) {
                            self.result
                                .warnings
                                .push(format!("{location}: Unsupported alias {alias:?} skipped."));
                        } else if self.seen.insert(alias.clone()) {
                            self.result.aliases.push(alias);
                        }
                    }
                }
                "match" => {
                    unconditional = values.len() == 1 && values[0].eq_ignore_ascii_case("all")
                }
                _ if !unconditional => self.warn(&location, "Conditional Include skipped"),
                _ => {
                    for value in values {
                        if value.contains(['$', '%']) {
                            self.warn(&location, format!("Dynamic Include {value:?} skipped"));
                            continue;
                        }
                        let paths = expand_home(Path::new(&value), self.home)
                            .map_err(str::to_string)
                            .and_then(|path| {
                                let pattern = if path.is_absolute() {
                                    path
                                } else {
                                    self.home.join(".ssh").join(path)
                                };
                                include_paths(&pattern)
                            });
                        match paths {
                            Ok(paths) => {
                                if paths.is_empty() {
                                    self.warn(
                                        &location,
                                        format!("Include {value:?} matched no files"),
                                    );
                                }
                                for included in paths {
                                    unconditional = self.read(&included, unconditional, depth + 1);
                                }
                            }
                            Err(error) => self.warn(
                                &location,
                                format!("Cannot expand Include {value:?} ({error})"),
                            ),
                        }
                    }
                }
            }
        }
        self.active.remove(&path);
        unconditional
    }
}

fn discover_with_home(config: Option<&Path>, home: &Path) -> Discovery {
    let default_config = home.join(".ssh/config");
    let config = config.unwrap_or(&default_config);
    let mut reader = Reader {
        home,
        result: Discovery::default(),
        seen: HashSet::new(),
        active: HashSet::new(),
    };
    match expand_home(config, home) {
        Ok(path) => {
            reader.read(&path, true, 0);
        }
        Err(error) => reader.warn(
            &config.display().to_string(),
            format!("Cannot read SSH config ({error})"),
        ),
    }
    reader.result
}

pub fn discover_hosts(config: Option<&Path>) -> Discovery {
    match std::env::var_os("HOME").filter(|home| !home.is_empty()) {
        Some(home) => discover_with_home(config, Path::new(&home)),
        None => Discovery {
            aliases: Vec::new(),
            warnings: vec!["HOME is not set; cannot discover SSH aliases.".into()],
        },
    }
}

#[cfg(test)]
mod tests {
    use crate::config::{Discovery, discover_with_home, is_alias};
    use std::fs;
    use std::path::PathBuf;
    use std::process::Command;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT: AtomicUsize = AtomicUsize::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "tunnel-rs-config-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            fs::create_dir(path.join(".ssh")).unwrap();
            Self(path)
        }
        fn write(&self, name: &str, contents: impl AsRef<[u8]>) {
            let path = self.0.join(".ssh").join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, contents).unwrap();
        }
        fn discover(&self) -> Discovery {
            discover_with_home(None, &self.0)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let status = Command::new("trash")
                .arg(&self.0)
                .status()
                .expect("trash must be available for fixture cleanup");
            assert!(status.success(), "could not trash config test fixture");
        }
    }

    #[test]
    fn aliases_and_syntax() {
        let f = Fixture::new();
        f.write("config", "# Host ignored\n hOsT = \"first\" second *.example !excluded first\nHost third # comment\nHost=second fourth\nHost -option bad;command \"two words\" user@host");
        let result = f.discover();
        assert_eq!(result.aliases, ["first", "second", "third", "fourth"]);
        assert_eq!(result.warnings.len(), 4);
        for valid in ["host", "mac-2", "dev.example", "_alias", "123"] {
            assert!(is_alias(valid));
        }
        for invalid in [
            "",
            "-oProxyCommand=exec",
            "a\nb",
            "host\n",
            "host\r",
            "host\u{2028}",
            "a b",
            "a/b",
            "a:b",
            "a;b",
            "a$()",
            "*",
            "!host",
            "[host]",
            "höst",
        ] {
            assert!(!is_alias(invalid), "{invalid:?}");
        }
    }

    #[test]
    fn sorted_nested_includes() {
        let f = Fixture::new();
        f.write("config", "Include \"parts with spaces/*.conf\"\nHost end\n");
        f.write("parts with spaces/02.conf", "Host second first\n");
        f.write(
            "parts with spaces/01.conf",
            "Include nested/one\nHost first\nHost *\n",
        );
        f.write("nested/one", "Host nested\nHost *\n");
        assert_eq!(
            f.discover(),
            Discovery {
                aliases: ["nested", "first", "second", "end"]
                    .map(String::from)
                    .into(),
                warnings: vec![]
            }
        );
    }

    #[test]
    fn home_and_custom_config() {
        let f = Fixture::new();
        f.write("config", "Include \"~/extra config\"\n");
        fs::write(f.0.join("extra config"), "Host home-host\n").unwrap();
        assert_eq!(f.discover().aliases, ["home-host"]);
        f.write("included", "Host from-user-ssh\n");
        fs::create_dir(f.0.join("project")).unwrap();
        fs::write(f.0.join("project/config"), "Include included\n").unwrap();
        fs::write(f.0.join("project/included"), "Host wrong\n").unwrap();
        assert_eq!(
            discover_with_home(Some(&f.0.join("project/config")), &f.0).aliases,
            ["from-user-ssh"]
        );
    }

    #[test]
    fn cycles_missing_and_depth() {
        let f = Fixture::new();
        f.write(
            "config",
            "Include config missing*.conf\nInclude chain/0\nHost usable\n",
        );
        for index in 0..18 {
            f.write(
                &format!("chain/{index}"),
                format!("Include chain/{}\n", index + 1),
            );
        }
        let result = f.discover();
        assert_eq!(result.aliases, ["usable"]);
        for reason in ["cycle", "matched no files", "depth limit"] {
            assert!(
                result
                    .warnings
                    .iter()
                    .any(|warning| warning.contains(reason))
            );
        }
    }

    #[test]
    fn conditional_includes_never_execute_match() {
        let f = Fixture::new();
        let marker = f.0.join("NEVER_EXECUTE");
        f.write("config", format!("Host selected\nInclude hidden\nMatch exec \"touch {}\"\nInclude hidden\nHost * !excluded\nInclude hidden\nMatch all\nInclude visible", marker.display()));
        f.write("hidden", "Host hidden-host\n");
        f.write("visible", "Host visible-host\n");
        let result = f.discover();
        assert_eq!(result.aliases, ["selected", "visible-host"]);
        assert_eq!(result.warnings.len(), 3);
        assert!(
            result
                .warnings
                .iter()
                .all(|warning| warning.contains("Conditional Include"))
        );
        assert!(!marker.exists());
    }

    #[test]
    fn included_context_propagates() {
        let f = Fixture::new();
        f.write(
            "config",
            "Include child\nInclude hidden\nHost *\nInclude visible\n",
        );
        f.write("child", "Host child-host\n");
        f.write("hidden", "Host hidden-host\n");
        f.write("visible", "Host visible-host\n");
        let result = f.discover();
        assert_eq!(result.aliases, ["child-host", "visible-host"]);
        assert_eq!(result.warnings.len(), 1);
        f.write("child", "Host child-host\nHost *\n");
        f.write("hidden", "Host hidden-host\nHost *\n");
        let result = f.discover();
        assert_eq!(
            result.aliases,
            ["child-host", "hidden-host", "visible-host"]
        );
        assert!(result.warnings.is_empty());
    }

    #[test]
    fn malformed_dynamic_and_named_home() {
        let f = Fixture::new();
        f.write("config", "Include ${CONFIG} %h.conf\nHost \"unterminated\nInclude hidden\nHost valid\nHost *\nInclude ~other/config\n");
        let result = f.discover();
        assert_eq!(result.aliases, ["valid"]);
        assert_eq!(result.warnings.len(), 5);
        assert!(result.warnings[4].contains("named-user home expansion is unsupported"));
    }

    #[test]
    fn missing_config_and_invalid_utf8() {
        let f = Fixture::new();
        assert!(f.discover().warnings[0].contains("Cannot read SSH config"));
        f.write("config", [255]);
        assert!(f.discover().warnings[0].contains("Cannot read SSH config"));
    }

    #[test]
    fn shell_escapes_and_comments() {
        let f = Fixture::new();
        f.write(
            "config",
            r#"Host dev\-one 'dev-two' "dev-three" dev-four#ignored"#,
        );
        assert_eq!(
            f.discover().aliases,
            ["dev-one", "dev-two", "dev-three", "dev-four"]
        );
    }

    #[test]
    fn wildcard_semantics() {
        let f = Fixture::new();
        f.write("config", "Include \"literal{a,b}\"\nHost *\nInclude \"parts/**/host\"\nHost *\nInclude classes/[!x]?.conf\n");
        f.write("literal{a,b}", "Host literal\n");
        f.write("parts/one/host", "Host shallow\n");
        f.write("parts/one/two/host", "Host deep\n");
        f.write("classes/ab.conf", "Host accepted\nHost *\n");
        f.write("classes/xb.conf", "Host excluded\n");
        f.write("classes/.b.conf", "Host hidden\n");
        f.write("classes/abc.conf", "Host too-long\n");
        assert_eq!(
            f.discover(),
            Discovery {
                aliases: ["literal", "shallow", "accepted"].map(String::from).into(),
                warnings: vec![]
            }
        );
    }
}
