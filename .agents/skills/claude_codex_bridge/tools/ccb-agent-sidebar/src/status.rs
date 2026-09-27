use ratatui::style::Color;

use crate::theme::SidebarTheme;

pub fn activity_symbol(state: &str) -> &'static str {
    match state {
        "active" => "●",
        "pending" => "◐",
        "idle" => "○",
        "failed" => "✕",
        "offline" => "·",
        _ => "·",
    }
}

pub fn activity_color(state: &str, explicit: Option<&str>) -> Color {
    activity_color_with_theme(state, explicit, SidebarTheme::default_dark())
}

pub fn activity_color_with_theme(
    state: &str,
    explicit: Option<&str>,
    theme: SidebarTheme,
) -> Color {
    if let Some(color) = explicit.and_then(|value| parse_activity_color(value, theme)) {
        return color;
    }
    fallback_activity_color(state, theme)
}

fn parse_activity_color(color: &str, theme: SidebarTheme) -> Option<Color> {
    match color.trim().to_ascii_lowercase().as_str() {
        "green" => Some(theme.success),
        "yellow" => Some(theme.warning),
        "blue" => Some(theme.info),
        "red" => Some(theme.danger),
        "gray" | "grey" => Some(theme.neutral),
        "darkgray" | "dark_gray" | "dark-grey" | "darkgrey" => Some(theme.muted),
        _ => None,
    }
}

fn fallback_activity_color(state: &str, theme: SidebarTheme) -> Color {
    match state {
        "active" => theme.success,
        "pending" => theme.warning,
        "idle" => theme.info,
        "failed" => theme.danger,
        "offline" => theme.muted,
        _ => theme.neutral,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maps_phase1_states_to_fixed_symbols() {
        assert_eq!(activity_symbol("active"), "●");
        assert_eq!(activity_symbol("pending"), "◐");
        assert_eq!(activity_symbol("idle"), "○");
        assert_eq!(activity_symbol("failed"), "✕");
        assert_eq!(activity_symbol("offline"), "·");
    }

    #[test]
    fn uses_project_view_color_when_present() {
        assert_eq!(activity_color("idle", Some("green")), Color::Green);
        assert_eq!(activity_color("idle", Some(" DARK_GRAY ")), Color::DarkGray);
    }

    #[test]
    fn falls_back_to_state_color_for_unknown_or_missing_project_view_color() {
        assert_eq!(activity_color("active", None), Color::Green);
        assert_eq!(activity_color("failed", Some("unknown")), Color::Red);
    }

    #[test]
    fn light_theme_maps_semantic_colors_to_light_palette() {
        let theme = SidebarTheme::light();

        assert_eq!(
            activity_color_with_theme("pending", Some("yellow"), theme),
            Color::Rgb(223, 142, 29)
        );
        assert_eq!(
            activity_color_with_theme("idle", None, theme),
            Color::Rgb(30, 102, 245)
        );
    }
}
