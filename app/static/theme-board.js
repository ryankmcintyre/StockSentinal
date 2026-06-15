/**
 * theme-board.js — Drag/drop heatmap board for Theme/Sector/Industry page.
 *
 * Interactions:
 *   • Drag a position chip from the tray or a theme card onto a drop-zone
 *     to add that position to the target theme (preserves existing tags).
 *   • Click the × button on a chip inside a theme card to remove that
 *     position–theme association.
 *   • Search box filters both the position tray and theme card chips.
 *   • Verdict filter buttons filter visible chips.
 *   • "+ New Theme" button opens an inline <dialog>.
 */

(function () {
    "use strict";

    // -------------------------------------------------------------------------
    // Constants
    // -------------------------------------------------------------------------

    /**
     * Human-readable labels for heat levels 0–5.
     * NOTE: This mapping mirrors _compute_heat_level() in app/main.py.
     * If the heat-level algorithm changes in Python, update this array too.
     */
    var HEAT_LABELS = ["", "Low", "Moderate", "Elevated", "High", "Critical"];

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    function getCsrfToken() {
        var meta = document.querySelector('input[name="csrf_token"]');
        return meta ? meta.value : "";
    }

    /** POST to a JSON endpoint with CSRF header; returns parsed JSON or throws. */
    async function postJson(url) {
        var csrfToken = getCsrfToken();
        var response = await fetch(url, {
            method: "POST",
            headers: {
                "accept": "application/json",
                "content-type": "application/json",
                "x-csrf-token": csrfToken,
                "x-requested-with": "fetch",
            },
            body: JSON.stringify({}),
        });
        var data = response.json ? await response.json() : {};
        if (!response.ok) {
            throw new Error((data && (data.detail || data.error)) || "Request failed");
        }
        return data;
    }

    /**
     * Show a transient toast notification to the user.
     * @param {string} message  Text to display.
     * @param {"error"|"success"} type  Visual style.
     */
    function showToast(message, type) {
        var container = document.getElementById("theme-toast-container");
        if (!container) {
            container = document.createElement("div");
            container.id = "theme-toast-container";
            container.setAttribute("role", "alert");
            container.setAttribute("aria-live", "polite");
            container.setAttribute("aria-atomic", "true");
            document.body.appendChild(container);
        }
        var toast = document.createElement("div");
        toast.className = "theme-toast theme-toast-" + (type || "error");
        toast.textContent = message;
        container.appendChild(toast);
        // Fade out and remove after 4 s
        setTimeout(function () {
            toast.classList.add("theme-toast-fade");
            setTimeout(function () { toast.remove(); }, 400);
        }, 4000);
    }

    // -------------------------------------------------------------------------
    // Drag / Drop state
    // -------------------------------------------------------------------------

    var dragState = {
        positionId: null,
        ticker: null,
        verdict: null,
        company: null,
        sourceThemeId: null,  // null means dragging from tray
    };

    function onDragStart(event, chip) {
        dragState.positionId = chip.dataset.positionId;
        dragState.ticker = chip.dataset.ticker;
        dragState.verdict = chip.dataset.verdict;
        dragState.company = chip.dataset.company || "";
        dragState.sourceThemeId = chip.dataset.sourceTheme || null;

        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("text/plain", dragState.ticker);
        chip.classList.add("dragging");
    }

    function onDragEnd(event, chip) {
        chip.classList.remove("dragging");
        document.querySelectorAll(".theme-chips-zone.drag-over").forEach(function (zone) {
            zone.classList.remove("drag-over");
        });
    }

    function onDragEnterZone(event, zone) {
        event.preventDefault();
        zone.classList.add("drag-over");
    }

    function onDragOverZone(event) {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
    }

    function onDragLeaveZone(event, zone) {
        if (!zone.contains(event.relatedTarget)) {
            zone.classList.remove("drag-over");
        }
    }

    async function onDropZone(event, zone) {
        event.preventDefault();
        zone.classList.remove("drag-over");

        var themeId = zone.dataset.themeId;
        var positionId = dragState.positionId;
        if (!themeId || !positionId) {
            return;
        }

        try {
            await postJson("/themes/" + themeId + "/positions/" + positionId);
            addChipToZone(zone, positionId, dragState.ticker, dragState.verdict, themeId);
            updateTrayChipThemes(positionId, themeId, true);
            updateHeatIndicator(zone.closest(".theme-heat-card"));
        } catch (err) {
            console.error("Failed to add position to theme:", err.message);
            showToast("Could not add position to theme: " + (err.message || "Please try again."));
        }
    }

    // -------------------------------------------------------------------------
    // Chip management
    // -------------------------------------------------------------------------

    function makeChip(positionId, ticker, verdict, themeId) {
        var chip = document.createElement("div");
        chip.className = "theme-position-chip";
        chip.draggable = true;
        chip.dataset.positionId = positionId;
        chip.dataset.ticker = ticker;
        chip.dataset.verdict = verdict;
        chip.dataset.sourceTheme = themeId;
        chip.title = ticker;

        var badge = document.createElement("span");
        badge.className = "verdict verdict-" + verdict + " verdict-xs";
        badge.setAttribute("aria-hidden", "true");
        badge.textContent = verdict.toUpperCase();

        var tickerSpan = document.createElement("span");
        tickerSpan.textContent = ticker;

        var removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "chip-remove-btn";
        removeBtn.dataset.removePosition = positionId;
        removeBtn.dataset.removeTheme = themeId;
        removeBtn.setAttribute("aria-label", "Remove " + ticker + " from theme");
        removeBtn.setAttribute("title", "Remove " + ticker + " from theme");
        removeBtn.textContent = "×";

        chip.appendChild(badge);
        chip.appendChild(tickerSpan);
        chip.appendChild(removeBtn);

        wireChipEvents(chip);
        return chip;
    }

    function addChipToZone(zone, positionId, ticker, verdict, themeId) {
        // Avoid duplicate chips
        var existing = zone.querySelector('[data-position-id="' + positionId + '"]');
        if (existing) {
            return;
        }
        var hint = zone.querySelector(".drop-hint");
        if (hint) {
            hint.remove();
        }
        var chip = makeChip(positionId, ticker, verdict, themeId);
        zone.appendChild(chip);
        // Update count badge
        var card = zone.closest(".theme-heat-card");
        if (card) {
            updatePositionCount(card);
        }
    }

    function removeChipFromZone(chip) {
        var zone = chip.closest(".theme-chips-zone");
        chip.remove();
        if (zone && zone.querySelectorAll(".theme-position-chip").length === 0) {
            var hint = document.createElement("span");
            hint.className = "drop-hint";
            hint.setAttribute("aria-hidden", "true");
            hint.textContent = "Drop positions here";
            zone.appendChild(hint);
        }
        if (zone) {
            var card = zone.closest(".theme-heat-card");
            if (card) {
                updatePositionCount(card);
                updateHeatIndicator(card);
            }
        }
    }

    function updatePositionCount(card) {
        var countEl = card.querySelector("[data-position-count]");
        if (!countEl) {
            return;
        }
        var chips = card.querySelectorAll(".theme-chips-zone .theme-position-chip");
        countEl.textContent = chips.length;
    }

    function updateHeatIndicator(card) {
        if (!card) {
            return;
        }
        var chips = Array.from(card.querySelectorAll(".theme-chips-zone .theme-position-chip"));
        var sell = chips.filter(function (c) { return c.dataset.verdict === "sell"; }).length;
        var trim = chips.filter(function (c) { return c.dataset.verdict === "trim"; }).length;
        var total = chips.length;
        // NOTE: This algorithm must stay in sync with _compute_heat_level() in app/main.py.
        var heat = 0;
        if (total > 0) {
            if (sell > 0) {
                heat = sell / total >= 0.5 ? 5 : 4;
            } else if (trim > 0) {
                heat = trim / total >= 0.5 ? 3 : 2;
            } else {
                heat = 1;
            }
        }
        card.dataset.heat = heat;
        var indicator = card.querySelector(".theme-heat-indicator");
        if (indicator) {
            indicator.style.setProperty("--heat", heat);
        }
        // Update heat bar row aria-label for accessibility
        var heatBarRow = card.querySelector(".heat-bar-row");
        if (heatBarRow) {
            heatBarRow.setAttribute("aria-label", "Heat level " + heat + " of 5");
        }
    }

    /** Update the data-themes attribute on a tray chip after a tag change. */
    function updateTrayChipThemes(positionId, themeId, added) {
        var trayChips = document.querySelectorAll(
            '#tray-list [data-position-id="' + positionId + '"], ' +
            '#untagged-section [data-position-id="' + positionId + '"]'
        );
        trayChips.forEach(function (chip) {
            var themes = (chip.dataset.themes || "").split(",").filter(Boolean);
            if (added) {
                if (!themes.includes(String(themeId))) {
                    themes.push(String(themeId));
                }
            } else {
                themes = themes.filter(function (id) { return id !== String(themeId); });
            }
            chip.dataset.themes = themes.join(",");
        });
    }

    // -------------------------------------------------------------------------
    // Remove button
    // -------------------------------------------------------------------------

    async function handleRemoveClick(btn) {
        var positionId = btn.dataset.removePosition;
        var themeId = btn.dataset.removeTheme;
        if (!positionId || !themeId) {
            return;
        }
        btn.disabled = true;
        try {
            await postJson("/themes/" + themeId + "/positions/" + positionId + "/remove");
            var chip = btn.closest(".theme-position-chip");
            if (chip) {
                removeChipFromZone(chip);
            }
            updateTrayChipThemes(positionId, themeId, false);
        } catch (err) {
            console.error("Failed to remove position from theme:", err.message);
            btn.disabled = false;
            showToast("Could not remove position from theme: " + (err.message || "Please try again."));
        }
    }

    // -------------------------------------------------------------------------
    // Wire events on a chip
    // -------------------------------------------------------------------------

    function wireChipEvents(chip) {
        chip.addEventListener("dragstart", function (e) { onDragStart(e, chip); });
        chip.addEventListener("dragend", function (e) { onDragEnd(e, chip); });

        var removeBtn = chip.querySelector(".chip-remove-btn");
        if (removeBtn) {
            removeBtn.addEventListener("click", function () {
                handleRemoveClick(removeBtn);
            });
        }

        wireAssignButton(chip);
    }

    // -------------------------------------------------------------------------
    // Keyboard-accessible assign menu (tray chips)
    // -------------------------------------------------------------------------

    /** Close all open assign menus and reset their toggle buttons. */
    function closeAssignMenus() {
        document.querySelectorAll(".chip-assign-menu").forEach(function (menu) {
            if (typeof menu._cleanup === "function") {
                menu._cleanup();
            }
            var chip = menu.closest(".tray-chip");
            var btn = chip && chip.querySelector(".chip-assign-btn");
            if (btn) {
                btn.setAttribute("aria-expanded", "false");
            }
            menu.remove();
        });
    }

    /**
     * Open a listbox dropdown anchored to `btn` that lets the keyboard user
     * assign `chip` (a tray-chip) to any existing theme.
     */
    function showAssignMenu(btn, chip) {
        closeAssignMenus();

        var positionId = chip.dataset.positionId;
        var ticker = chip.dataset.ticker;
        var verdict = chip.dataset.verdict;
        var assignedThemes = (chip.dataset.themes || "").split(",").filter(Boolean);

        // Collect available themes from the DOM
        var themes = [];
        document.querySelectorAll(".theme-heat-card").forEach(function (card) {
            var themeId = card.dataset.themeId;
            var nameEl = card.querySelector("[data-theme-name-label]");
            if (themeId && nameEl) {
                themes.push({ id: themeId, name: nameEl.textContent.trim() });
            }
        });

        if (themes.length === 0) {
            showToast("No themes yet. Click + New Theme to create one first.", "error");
            return;
        }

        var menu = document.createElement("ul");
        menu.className = "chip-assign-menu";
        menu.setAttribute("role", "listbox");
        menu.setAttribute("aria-label", "Assign " + ticker + " to a theme");

        themes.forEach(function (theme) {
            var isAssigned = assignedThemes.includes(String(theme.id));
            var item = document.createElement("li");
            item.className = "chip-assign-menu-item" + (isAssigned ? " chip-assign-menu-item-assigned" : "");
            item.setAttribute("role", "option");
            item.setAttribute("aria-selected", isAssigned ? "true" : "false");
            item.setAttribute("tabindex", "0");
            item.textContent = theme.name + (isAssigned ? " \u2713" : "");
            item.dataset.themeId = theme.id;

            async function doAssign() {
                if (isAssigned) {
                    closeAssignMenus();
                    btn.focus();
                    return;
                }
                closeAssignMenus();
                var zone = document.querySelector('.theme-chips-zone[data-theme-id="' + theme.id + '"]');
                try {
                    await postJson("/themes/" + theme.id + "/positions/" + positionId);
                    if (zone) {
                        addChipToZone(zone, positionId, ticker, verdict, theme.id);
                        var card = zone.closest(".theme-heat-card");
                        if (card) {
                            updateHeatIndicator(card);
                        }
                    }
                    updateTrayChipThemes(positionId, theme.id, true);
                    btn.focus();
                } catch (err) {
                    console.error("Failed to assign position to theme:", err.message);
                    showToast("Could not assign position to theme: " + (err.message || "Please try again."), "error");
                    btn.focus();
                }
            }

            item.addEventListener("click", doAssign);
            item.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();  // prevent Space-scroll and Enter-click duplication
                    doAssign();
                } else if (e.key === "Escape") {
                    e.stopPropagation();
                    closeAssignMenus();
                    btn.focus();
                } else if (e.key === "ArrowDown") {
                    e.preventDefault();
                    var next = item.nextElementSibling;
                    if (next) { next.focus(); }
                } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    var prev = item.previousElementSibling;
                    if (prev) { prev.focus(); } else { btn.focus(); }
                }
            });

            menu.appendChild(item);
        });

        btn.setAttribute("aria-expanded", "true");
        chip.appendChild(menu);

        // Focus the first menu item
        var firstItem = menu.querySelector(".chip-assign-menu-item");
        if (firstItem) {
            firstItem.focus();
        }

        // Close when focus leaves the menu (handles Tab and any other focus loss)
        menu.addEventListener("focusout", function (e) {
            // relatedTarget is the element receiving focus; if it's outside the menu, close
            setTimeout(function () {
                if (!menu.contains(document.activeElement)) {
                    closeAssignMenus();
                }
            }, 0);
        });

        // Close on click outside (bubble phase)
        function onOutsideClick(e) {
            if (!menu.contains(e.target) && e.target !== btn) {
                closeAssignMenus();
            }
        }
        document.addEventListener("click", onOutsideClick);
        menu._cleanup = function () {
            document.removeEventListener("click", onOutsideClick);
        };
    }

    /** Wire the assign button on a tray chip (no-op if no button present). */
    function wireAssignButton(chip) {
        var btn = chip.querySelector(".chip-assign-btn");
        if (!btn) {
            return;
        }
        btn.addEventListener("click", function (e) {
            e.stopPropagation();
            var isExpanded = btn.getAttribute("aria-expanded") === "true";
            if (isExpanded) {
                closeAssignMenus();
                btn.focus();
            } else {
                showAssignMenu(btn, chip);
            }
        });
    }

    // -------------------------------------------------------------------------
    // Wire events on a drop zone
    // -------------------------------------------------------------------------

    function wireDropZone(zone) {
        zone.addEventListener("dragenter", function (e) { onDragEnterZone(e, zone); });
        zone.addEventListener("dragover", onDragOverZone);
        zone.addEventListener("dragleave", function (e) { onDragLeaveZone(e, zone); });
        zone.addEventListener("drop", function (e) { onDropZone(e, zone); });
    }

    // -------------------------------------------------------------------------
    // Search & filter
    // -------------------------------------------------------------------------

    var currentFilter = "all";
    var currentSearch = "";

    function normalise(str) {
        return (str || "").toLowerCase().trim();
    }

    function applyFilters() {
        var search = normalise(currentSearch);

        // Filter tray chips
        document.querySelectorAll("#tray-list .tray-chip").forEach(function (chip) {
            var verdict = chip.dataset.verdict || "";
            var ticker = normalise(chip.dataset.ticker);
            var company = normalise(chip.dataset.company);
            var verdictMatch = currentFilter === "all" || verdict === currentFilter;
            var searchMatch = !search || ticker.includes(search) || company.includes(search);
            chip.style.display = verdictMatch && searchMatch ? "" : "none";
        });

        // Filter theme card chips
        document.querySelectorAll(".theme-heat-card").forEach(function (card) {
            var themeName = normalise(card.dataset.themeName);
            var themeSearchMatch = !search || themeName.includes(search);
            var cardHasVisible = false;

            card.querySelectorAll(".theme-position-chip").forEach(function (chip) {
                var verdict = chip.dataset.verdict || "";
                var ticker = normalise(chip.dataset.ticker);
                var verdictMatch = currentFilter === "all" || verdict === currentFilter;
                var chipSearchMatch = !search || ticker.includes(search) || themeSearchMatch;
                var show = verdictMatch && chipSearchMatch;
                chip.style.display = show ? "" : "none";
                if (show) {
                    cardHasVisible = true;
                }
            });

            // Show/hide the card itself based on theme name match or chip visibility
            card.style.display = (themeSearchMatch || cardHasVisible) ? "" : "none";
        });

        // Update tray count
        var trayCount = document.getElementById("tray-count");
        if (trayCount) {
            var visible = document.querySelectorAll("#tray-list .tray-chip:not([style*='display: none'])").length;
            trayCount.textContent = visible;
        }
    }

    // -------------------------------------------------------------------------
    // Rename toggle
    // -------------------------------------------------------------------------

    function wireRenameToggle(card) {
        var toggleBtn = card.querySelector("[data-rename-toggle]");
        var renameForm = card.querySelector("[data-rename-form]");
        var cancelBtn = card.querySelector("[data-rename-cancel]");
        var titleRow = card.querySelector(".theme-card-title-row");
        var headerActions = card.querySelector(".theme-card-header-actions");

        if (!toggleBtn || !renameForm) {
            return;
        }

        toggleBtn.addEventListener("click", function () {
            var isOpen = renameForm.style.display !== "none";
            renameForm.style.display = isOpen ? "none" : "";
            if (titleRow) {
                titleRow.style.display = isOpen ? "" : "none";
            }
            if (headerActions) {
                headerActions.style.display = isOpen ? "" : "none";
            }
            if (!isOpen) {
                var input = renameForm.querySelector("[data-rename-input]");
                if (input) {
                    input.focus();
                    input.select();
                }
            }
        });

        if (cancelBtn) {
            cancelBtn.addEventListener("click", function () {
                renameForm.style.display = "none";
                if (titleRow) {
                    titleRow.style.display = "";
                }
                if (headerActions) {
                    headerActions.style.display = "";
                }
            });
        }
    }

    // -------------------------------------------------------------------------
    // New Theme dialog
    // -------------------------------------------------------------------------

    function wireNewThemeDialog() {
        var openBtn = document.getElementById("new-theme-btn");
        var dialog = document.getElementById("new-theme-dialog");
        var cancelBtn = document.getElementById("cancel-new-theme");
        var form = document.getElementById("new-theme-form");
        var errorEl = document.getElementById("new-theme-error");
        var nameInput = document.getElementById("new-theme-name");

        if (!openBtn || !dialog) {
            return;
        }

        openBtn.addEventListener("click", function () {
            if (errorEl) {
                errorEl.style.display = "none";
            }
            if (nameInput) {
                nameInput.value = "";
            }
            dialog.showModal();
            if (nameInput) {
                nameInput.focus();
            }
        });

        if (cancelBtn) {
            cancelBtn.addEventListener("click", function () {
                dialog.close();
            });
        }

        dialog.addEventListener("click", function (e) {
            if (e.target === dialog) {
                dialog.close();
            }
        });

        if (!form) {
            return;
        }

        form.addEventListener("submit", async function (e) {
            e.preventDefault();
            var name = nameInput ? nameInput.value.trim() : "";
            if (!name) {
                return;
            }

            var submitBtn = form.querySelector('button[type="submit"]');
            if (submitBtn) {
                submitBtn.disabled = true;
            }

            try {
                var csrfToken = getCsrfToken();
                var response = await fetch("/themes", {
                    method: "POST",
                    headers: {
                        "accept": "application/json",
                        "content-type": "application/json",
                        "x-csrf-token": csrfToken,
                        "x-requested-with": "fetch",
                    },
                    body: JSON.stringify({ name: name }),
                });
                var data = await response.json();
                if (!response.ok) {
                    throw new Error((data && data.error) || "Could not create theme");
                }
                // Add the new theme card to the grid and close the dialog
                appendNewThemeCard(data.id, data.name);
                dialog.close();
                // Hide the empty state if it was showing
                var emptyState = document.querySelector(".theme-board-empty");
                if (emptyState) {
                    emptyState.style.display = "none";
                }
            } catch (err) {
                if (errorEl) {
                    errorEl.textContent = err.message;
                    errorEl.style.display = "";
                }
            } finally {
                if (submitBtn) {
                    submitBtn.disabled = false;
                }
            }
        });
    }

    function appendNewThemeCard(themeId, themeName) {
        var grid = document.getElementById("theme-grid");
        if (!grid) {
            return;
        }

        var card = document.createElement("article");
        card.className = "theme-heat-card";
        card.dataset.themeId = themeId;
        card.dataset.heat = "0";
        card.dataset.themeName = themeName.toLowerCase();

        card.innerHTML =
            '<div class="theme-heat-indicator" aria-hidden="true" style="--heat:0;"></div>' +
            '<div class="theme-card-header">' +
              '<div class="theme-card-title-row">' +
                '<span class="theme-card-name" data-theme-name-label>' + escapeHtml(themeName) + '</span>' +
                '<span class="theme-count" data-position-count>0</span>' +
              '</div>' +
              '<div class="theme-card-header-actions">' +
                '<button class="btn btn-small theme-rename-toggle-btn" data-rename-toggle title="Rename theme" aria-label="Rename ' + escapeHtml(themeName) + '" type="button">✎</button>' +
                '<form method="post" action="/themes/' + themeId + '/delete" class="inline-form" onsubmit="return confirm(\'Delete theme &quot;' + escapeHtml(themeName) + '&quot;? Positions will remain.\');">' +
                  '<input type="hidden" name="csrf_token" value="">' +
                  '<button type="submit" class="btn btn-small btn-danger" title="Delete theme">✕</button>' +
                '</form>' +
              '</div>' +
            '</div>' +
            '<div class="heat-bar-row" aria-live="polite" aria-label="Heat level 0 of 5">' +
              '<span class="heat-bar-label">Heat:</span>' +
              '<span class="heat-blocks" aria-hidden="true">' +
                '<span class="heat-block"></span>' +
                '<span class="heat-block"></span>' +
                '<span class="heat-block"></span>' +
                '<span class="heat-block"></span>' +
                '<span class="heat-block"></span>' +
              '</span>' +
            '</div>' +
            '<form method="post" action="/themes/' + themeId + '/rename" class="theme-rename-inline" data-rename-form style="display:none;">' +
              '<input type="hidden" name="csrf_token" value="">' +
              '<input type="text" name="name" value="' + escapeHtml(themeName) + '" maxlength="80" aria-label="New name for ' + escapeHtml(themeName) + '" data-rename-input>' +
              '<button type="submit" class="btn btn-small">Save</button>' +
              '<button type="button" class="btn btn-small" data-rename-cancel>Cancel</button>' +
            '</form>' +
            '<div class="theme-chips-zone" data-drop-zone data-theme-id="' + themeId + '" aria-label="Drop positions onto ' + escapeHtml(themeName) + '">' +
              '<span class="drop-hint" aria-hidden="true">Drop positions here</span>' +
            '</div>';

        grid.appendChild(card);

        // Populate CSRF token from the page's first hidden csrf_token input so the
        // value is always current even after the card was created dynamically.
        card.querySelectorAll("form").forEach(function (form) {
            form.addEventListener("submit", function () {
                var csrfInput = form.querySelector('[name="csrf_token"]');
                if (csrfInput) {
                    csrfInput.value = getCsrfToken();
                }
            });
        });

        // Wire the new card
        wireRenameToggle(card);
        var zone = card.querySelector(".theme-chips-zone");
        if (zone) {
            wireDropZone(zone);
        }
    }

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // -------------------------------------------------------------------------
    // Init
    // -------------------------------------------------------------------------

    function init() {
        // Wire existing chips
        document.querySelectorAll(".tray-chip[draggable], .theme-position-chip[draggable]").forEach(wireChipEvents);

        // Wire existing drop zones
        document.querySelectorAll("[data-drop-zone]").forEach(wireDropZone);

        // Wire rename toggles on existing cards
        document.querySelectorAll(".theme-heat-card").forEach(wireRenameToggle);

        // Search input
        var searchInput = document.getElementById("theme-board-search");
        if (searchInput) {
            searchInput.addEventListener("input", function () {
                currentSearch = searchInput.value;
                applyFilters();
            });
        }

        // Verdict filter buttons
        document.querySelectorAll(".verdict-filter-btn").forEach(function (btn) {
            btn.addEventListener("click", function () {
                document.querySelectorAll(".verdict-filter-btn").forEach(function (b) {
                    b.classList.remove("active");
                    b.setAttribute("aria-pressed", "false");
                });
                btn.classList.add("active");
                btn.setAttribute("aria-pressed", "true");
                currentFilter = btn.dataset.filter || "all";
                applyFilters();
            });
        });

        // New theme dialog
        wireNewThemeDialog();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
}());
