(function () {
    var THEME_STORAGE_KEY = "themePreference";
    var VALID_THEMES = ["system", "light", "dark"];

    function isValidTheme(theme) {
        return VALID_THEMES.indexOf(theme) !== -1;
    }

    function readStoredTheme() {
        try {
            var theme = window.localStorage.getItem(THEME_STORAGE_KEY);
            return isValidTheme(theme) ? theme : "system";
        } catch (_error) {
            return "system";
        }
    }

    function writeStoredTheme(theme) {
        try {
            window.localStorage.setItem(THEME_STORAGE_KEY, theme);
        } catch (_error) {
            // Ignore storage failures (privacy mode, quota, etc.).
        }
    }

    function resolveTheme(theme) {
        if (theme !== "system") {
            return theme;
        }
        if (window.matchMedia) {
            return window.matchMedia("(prefers-color-scheme: dark)").matches
                ? "dark"
                : "light";
        }
        return "light";
    }

    function applyTheme(theme) {
        var root = document.documentElement;
        root.dataset.themePreference = theme;
        root.dataset.theme = resolveTheme(theme) === "dark" ? "dark" : "light";
    }

    function syncThemeMenu(theme) {
        document.querySelectorAll("[data-theme-option]").forEach(function (option) {
            option.setAttribute(
                "aria-checked",
                option.dataset.themeOption === theme ? "true" : "false"
            );
        });
    }

    function closeThemeMenus() {
        document.querySelectorAll(".profile-menu[open]").forEach(function (menu) {
            menu.removeAttribute("open");
        });
    }

    function wireThemeOptions() {
        document.querySelectorAll("[data-theme-option]").forEach(function (option) {
            option.addEventListener("click", function () {
                var theme = option.dataset.themeOption;
                if (!isValidTheme(theme)) {
                    return;
                }
                writeStoredTheme(theme);
                applyTheme(theme);
                syncThemeMenu(theme);
                closeThemeMenus();
            });
        });
    }

    function wireDismissHandlers() {
        document.addEventListener("click", function (event) {
            document.querySelectorAll(".profile-menu[open]").forEach(function (menu) {
                if (!menu.contains(event.target)) {
                    menu.removeAttribute("open");
                }
            });
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                closeThemeMenus();
            }
        });
    }

    function wireSystemThemeListener() {
        if (!window.matchMedia) {
            return;
        }
        var mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
        var handleThemeChange = function () {
            var theme = readStoredTheme();
            if (theme === "system") {
                applyTheme(theme);
                syncThemeMenu(theme);
            }
        };
        if (mediaQuery.addEventListener) {
            mediaQuery.addEventListener("change", handleThemeChange);
            return;
        }
        if (mediaQuery.addListener) {
            mediaQuery.addListener(handleThemeChange);
        }
    }

    var theme = readStoredTheme();
    applyTheme(theme);
    syncThemeMenu(theme);
    wireThemeOptions();
    wireDismissHandlers();
    wireSystemThemeListener();
}());
