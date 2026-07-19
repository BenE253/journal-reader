/* Theme preference: "auto" (follow the device), "light", or "dark".
   Stored in localStorage so it sticks per browser/device. Loaded in
   <head> (blocking, but tiny) so the right palette applies before the
   first paint — no white/black flash. */

(function () {
  "use strict";

  var KEY = "papershelf-theme";

  function apply(mode) {
    if (mode === "light" || mode === "dark") {
      document.documentElement.dataset.theme = mode;
    } else {
      // "auto": remove the override so the CSS media query decides.
      delete document.documentElement.dataset.theme;
    }
  }

  function current() {
    var stored = localStorage.getItem(KEY);
    return stored === "light" || stored === "dark" ? stored : "auto";
  }

  apply(current());

  // Wire up any toggle buttons on the page once it has loaded.
  // data-theme-toggle="menu" gets a text label (reader overflow menu);
  // anything else gets a compact icon (library header).
  document.addEventListener("DOMContentLoaded", function () {
    var ICONS = { auto: "◐", light: "☀︎", dark: "☾" };

    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      function refresh() {
        var mode = current();
        btn.textContent = btn.dataset.themeToggle === "menu"
          ? "Theme: " + mode
          : ICONS[mode];
      }

      btn.addEventListener("click", function () {
        var order = ["auto", "light", "dark"];
        var next = order[(order.indexOf(current()) + 1) % order.length];
        localStorage.setItem(KEY, next);
        apply(next);
        refresh();
      });

      refresh();
    });
  });
})();
