(function () {
    function setExpanded(toggle, content, expanded) {
        toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
        content.hidden = !expanded;
        var chevronElement = toggle.querySelector(".rules-section-chevron");
        if (chevronElement) {
            chevronElement.textContent = expanded ? "▼" : "▶";
        }
    }

    function wireRulesSectionToggles() {
        var toggles = document.querySelectorAll("[data-rules-section-toggle='true']");
        toggles.forEach(function (toggle) {
            var contentId = toggle.getAttribute("aria-controls");
            if (!contentId) {
                return;
            }

            var content = document.getElementById(contentId);
            if (!content) {
                return;
            }

            setExpanded(toggle, content, false);
            toggle.addEventListener("click", function () {
                var expanded = toggle.getAttribute("aria-expanded") === "true";
                setExpanded(toggle, content, !expanded);
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        wireRulesSectionToggles();
    });
})();
