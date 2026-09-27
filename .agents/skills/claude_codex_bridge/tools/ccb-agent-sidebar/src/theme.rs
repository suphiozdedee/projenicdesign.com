use std::env;
use std::fs;
use std::hash::{DefaultHasher, Hash, Hasher};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::SystemTime;

use ratatui::prelude::{Color, Modifier, Style};
use serde_json::Value;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SidebarTheme {
    pub name: &'static str,
    pub focus: Color,
    pub selection_fg: Color,
    pub selection_bg: Option<Color>,
    pub success: Color,
    pub warning: Color,
    pub danger: Color,
    pub info: Color,
    pub neutral: Color,
    pub muted: Color,
    pub clear: Color,
    pub scrollbar_track: Color,
    pub scrollbar_thumb: Color,
}

impl SidebarTheme {
    pub fn from_profile(profile: &str) -> Self {
        match normalize_profile(profile).as_deref() {
            Some("light") => Self::light(),
            _ => Self::default_dark(),
        }
    }

    pub fn from_env() -> Self {
        let profile = env::var("CCB_SIDEBAR_THEME_PROFILE")
            .ok()
            .filter(|value| !value.trim().is_empty())
            .or_else(|| env::var("CCB_TMUX_THEME_PROFILE").ok());
        profile
            .as_deref()
            .map(Self::from_profile)
            .unwrap_or_else(Self::default_dark)
    }

    pub fn default_dark() -> Self {
        Self {
            name: "default",
            focus: Color::DarkGray,
            selection_fg: Color::Reset,
            selection_bg: None,
            success: Color::Green,
            warning: Color::Yellow,
            danger: Color::Red,
            info: Color::Blue,
            neutral: Color::Gray,
            muted: Color::DarkGray,
            clear: Color::Cyan,
            scrollbar_track: Color::DarkGray,
            scrollbar_thumb: Color::Gray,
        }
    }

    pub fn light() -> Self {
        Self {
            name: "light",
            focus: Color::Rgb(108, 111, 133),
            selection_fg: Color::Rgb(76, 79, 105),
            selection_bg: Some(Color::Rgb(204, 208, 218)),
            success: Color::Rgb(64, 160, 43),
            warning: Color::Rgb(223, 142, 29),
            danger: Color::Rgb(210, 15, 57),
            info: Color::Rgb(30, 102, 245),
            neutral: Color::Rgb(108, 111, 133),
            muted: Color::Rgb(156, 160, 176),
            clear: Color::Rgb(23, 146, 153),
            scrollbar_track: Color::Rgb(220, 224, 232),
            scrollbar_thumb: Color::Rgb(156, 160, 176),
        }
    }

    pub fn selection_style(self) -> Style {
        let base = Style::default()
            .fg(self.selection_fg)
            .add_modifier(Modifier::BOLD);
        if let Some(bg) = self.selection_bg {
            base.bg(bg)
        } else {
            Style::default().add_modifier(Modifier::REVERSED)
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ThemeFileSignature {
    modified: Option<SystemTime>,
    len: u64,
    content_hash: u64,
}

#[derive(Debug)]
pub struct RuntimeThemeResolver {
    fallback: SidebarTheme,
    config_path: Option<PathBuf>,
    last_signature: Option<ThemeFileSignature>,
    initialized: bool,
    current: SidebarTheme,
}

impl RuntimeThemeResolver {
    pub fn new(fallback_profile: &str) -> Self {
        Self::with_config_path(fallback_profile, theme_config_path())
    }

    fn with_config_path(fallback_profile: &str, config_path: Option<PathBuf>) -> Self {
        let fallback = SidebarTheme::from_profile(fallback_profile);
        let mut resolver = Self {
            fallback,
            config_path,
            last_signature: None,
            initialized: false,
            current: fallback,
        };
        resolver.refresh();
        resolver
    }

    pub fn theme(&self) -> SidebarTheme {
        self.current
    }

    pub fn refresh(&mut self) -> SidebarTheme {
        let signature = self.config_path.as_deref().and_then(theme_file_signature);
        if self.initialized && signature == self.last_signature {
            return self.current;
        }
        self.initialized = true;
        self.last_signature = signature;
        self.current = self
            .config_path
            .as_deref()
            .and_then(theme_from_config_file)
            .unwrap_or(self.fallback);
        self.current
    }
}

pub fn normalize_profile(profile: &str) -> Option<String> {
    let value = profile.trim().to_ascii_lowercase();
    match value.as_str() {
        "default" | "dark" | "contrast" | "light" => Some(value),
        _ => None,
    }
}

fn theme_config_path() -> Option<PathBuf> {
    if let Some(config_home) = env::var_os("XDG_CONFIG_HOME").filter(|value| !value.is_empty()) {
        return Some(PathBuf::from(config_home).join("ccb").join("theme.json"));
    }
    env::var_os("HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .map(|home| home.join(".config").join("ccb").join("theme.json"))
}

fn theme_file_signature(path: &Path) -> Option<ThemeFileSignature> {
    let metadata = fs::metadata(path).ok()?;
    let content = fs::read(path).ok()?;
    let mut hasher = DefaultHasher::new();
    content.hash(&mut hasher);
    Some(ThemeFileSignature {
        modified: metadata.modified().ok(),
        len: metadata.len(),
        content_hash: hasher.finish(),
    })
}

fn theme_from_config_file(path: &Path) -> Option<SidebarTheme> {
    let payload: Value = serde_json::from_str(&fs::read_to_string(path).ok()?).ok()?;
    let profile = payload
        .get("tmux_profile")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    match profile.as_str() {
        "light" => return Some(SidebarTheme::light()),
        "default" | "contrast" | "dark" => return Some(SidebarTheme::default_dark()),
        "system" => return Some(system_sidebar_theme()),
        _ => {}
    }

    let theme = payload
        .get("theme")
        .and_then(Value::as_str)
        .or_else(|| payload.get("palette").and_then(Value::as_str))
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    match theme.as_str() {
        "system" => Some(system_sidebar_theme()),
        "light" | "latte" | "solarized" | "solarized_light" | "tokyo" | "tokyo_night_light"
        | "gruvbox" | "gruvbox_light" | "rose-pine" | "rose_pine_dawn" => {
            Some(SidebarTheme::light())
        }
        "dark" | "default" | "contrast" | "nord" => Some(SidebarTheme::default_dark()),
        _ => None,
    }
}

fn system_sidebar_theme() -> SidebarTheme {
    for key in ["CCB_SYSTEM_THEME", "GTK_THEME", "QT_STYLE_OVERRIDE"] {
        if let Ok(value) = env::var(key) {
            let normalized = value.trim().to_ascii_lowercase();
            if normalized.contains("dark") {
                return SidebarTheme::default_dark();
            }
            if normalized.contains("light") || (key != "CCB_SYSTEM_THEME" && !normalized.is_empty())
            {
                return SidebarTheme::light();
            }
        }
    }

    if env::var_os("WSL_DISTRO_NAME").is_some() || env::var_os("WSL_INTEROP").is_some() {
        let output = command_output(
            "powershell.exe",
            &[
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize' -Name AppsUseLightTheme).AppsUseLightTheme",
            ],
        );
        match output.as_deref() {
            Some("0") => return SidebarTheme::default_dark(),
            Some("1") => return SidebarTheme::light(),
            _ => {}
        }
    }

    if cfg!(target_os = "macos") {
        let output = command_output("defaults", &["read", "-g", "AppleInterfaceStyle"]);
        return if output
            .as_deref()
            .is_some_and(|value| value.to_ascii_lowercase().contains("dark"))
        {
            SidebarTheme::default_dark()
        } else {
            SidebarTheme::light()
        };
    }

    if cfg!(target_os = "linux") {
        if let Some(value) = command_output(
            "gsettings",
            &["get", "org.gnome.desktop.interface", "color-scheme"],
        ) {
            let normalized = value.to_ascii_lowercase();
            if normalized.contains("dark") {
                return SidebarTheme::default_dark();
            }
            if !normalized.is_empty() && !normalized.contains("default") {
                return SidebarTheme::light();
            }
        }
        if let Some(value) = command_output(
            "gsettings",
            &["get", "org.gnome.desktop.interface", "gtk-theme"],
        ) {
            return if value.to_ascii_lowercase().contains("dark") {
                SidebarTheme::default_dark()
            } else {
                SidebarTheme::light()
            };
        }
    }

    if let Ok(value) = env::var("COLORFGBG")
        && let Some(background) = value.rsplit(';').next()
        && let Ok(index) = background.trim().parse::<u8>()
    {
        return if index < 7 {
            SidebarTheme::default_dark()
        } else {
            SidebarTheme::light()
        };
    }
    SidebarTheme::default_dark()
}

fn command_output(program: &str, args: &[&str]) -> Option<String> {
    let output = Command::new(program).args(args).output().ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn maps_light_profile_to_light_theme() {
        let theme = SidebarTheme::from_profile(" light ");

        assert_eq!(theme.name, "light");
        assert_eq!(theme.focus, Color::Rgb(108, 111, 133));
        assert_eq!(theme.selection_bg, Some(Color::Rgb(204, 208, 218)));
    }

    #[test]
    fn unknown_profile_falls_back_to_default_dark() {
        let theme = SidebarTheme::from_profile("unknown");

        assert_eq!(theme.name, "default");
        assert_eq!(theme.focus, Color::DarkGray);
    }

    #[test]
    fn saved_theme_profile_wins_over_stale_launch_fallback() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            env::temp_dir().join(format!("ccb-sidebar-theme-{}-{unique}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let path = root.join("theme.json");
        fs::write(
            &path,
            r#"{"schema_version":1,"theme":"dark","palette":"dark","tmux_profile":"default"}"#,
        )
        .unwrap();

        let resolver = RuntimeThemeResolver::with_config_path("light", Some(path));

        assert_eq!(resolver.theme().name, "default");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn missing_theme_file_uses_launch_fallback() {
        let resolver = RuntimeThemeResolver::with_config_path(
            "light",
            Some(env::temp_dir().join("ccb-sidebar-missing-theme.json")),
        );

        assert_eq!(resolver.theme().name, "light");
    }
}
