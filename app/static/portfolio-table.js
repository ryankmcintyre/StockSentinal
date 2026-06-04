(function () {
    var SORT_STATE_STORAGE_KEY = "portfolioTableSortState";
    var FOCUS_POSITION_STORAGE_KEY = "portfolioFocusPositionId";

    function readSessionStorage(key) {
        try {
            return window.sessionStorage.getItem(key);
        } catch (_error) {
            return null;
        }
    }

    function writeSessionStorage(key, value) {
        try {
            window.sessionStorage.setItem(key, value);
        } catch (_error) {
            // Ignore storage failures (privacy mode, quota, etc.).
        }
    }

    function removeSessionStorage(key) {
        try {
            window.sessionStorage.removeItem(key);
        } catch (_error) {
            // Ignore storage failures (privacy mode, quota, etc.).
        }
    }

    function normalizeValue(cell, sortType) {
        var rawValue = cell.dataset.sortValue || cell.textContent || "";

        if (sortType === "number") {
            var parsed = Number(rawValue);
            return Number.isNaN(parsed) ? Number.POSITIVE_INFINITY : parsed;
        }

        return rawValue.trim().toLowerCase();
    }

    function updateHeaderState(headers, activeHeader, direction) {
        headers.forEach(function (header) {
            var th = header.closest("th");
            var indicator = header.querySelector(".table-sort-indicator");
            var isActive = header === activeHeader && direction !== null;

            header.dataset.sortDirection = isActive ? direction : "";
            th.setAttribute(
                "aria-sort",
                isActive
                    ? direction === "asc"
                        ? "ascending"
                        : "descending"
                    : "none"
            );
            indicator.textContent = isActive ? (direction === "asc" ? "↑" : "↓") : "";
        });
    }

    function persistSortState(activeHeader, direction) {
        if (!activeHeader || !direction) {
            removeSessionStorage(SORT_STATE_STORAGE_KEY);
            return;
        }
        writeSessionStorage(
            SORT_STATE_STORAGE_KEY,
            JSON.stringify({
                column: activeHeader.dataset.sortColumn,
                direction: direction,
            })
        );
    }

    function loadSortState(headers) {
        var raw = readSessionStorage(SORT_STATE_STORAGE_KEY);
        if (!raw) {
            return null;
        }
        try {
            var parsed = JSON.parse(raw);
            if (!parsed || (parsed.direction !== "asc" && parsed.direction !== "desc")) {
                removeSessionStorage(SORT_STATE_STORAGE_KEY);
                return null;
            }
            var header = headers.find(function (candidate) {
                return candidate.dataset.sortColumn === parsed.column;
            });
            if (!header) {
                removeSessionStorage(SORT_STATE_STORAGE_KEY);
                return null;
            }
            return {
                header: header,
                direction: parsed.direction,
            };
        } catch (_error) {
            removeSessionStorage(SORT_STATE_STORAGE_KEY);
            return null;
        }
    }

    function sortRows(tbody, header, direction) {
        // Read rows from the live DOM on every sort so in-place row
        // replacements (after a refresh) are respected and detached, stale
        // rows are never re-appended.
        var rows = Array.from(tbody.rows);

        if (direction === null) {
            rows.slice()
                .sort(function (left, right) {
                    return (
                        Number(left.dataset.originalIndex) -
                        Number(right.dataset.originalIndex)
                    );
                })
                .forEach(function (row) {
                    tbody.appendChild(row);
                });
            return;
        }

        var columnIndex = Number(header.dataset.sortColumn);
        var sortType = header.dataset.sortType || "text";
        var sortedRows = rows.slice().sort(function (left, right) {
            var leftValue = normalizeValue(left.cells[columnIndex], sortType);
            var rightValue = normalizeValue(right.cells[columnIndex], sortType);

            if (leftValue < rightValue) {
                return direction === "asc" ? -1 : 1;
            }
            if (leftValue > rightValue) {
                return direction === "asc" ? 1 : -1;
            }
            return Number(left.dataset.originalIndex) - Number(right.dataset.originalIndex);
        });

        sortedRows.forEach(function (row) {
            tbody.appendChild(row);
        });
    }

    function focusPositionFromStorage(table) {
        var focusPositionId = readSessionStorage(FOCUS_POSITION_STORAGE_KEY);
        if (!focusPositionId) {
            return;
        }
        removeSessionStorage(FOCUS_POSITION_STORAGE_KEY);
        var row = Array.from(
            table.querySelectorAll("tr[data-position-id]")
        ).find(function (candidate) {
            return (
                candidate.getAttribute("data-position-id") === focusPositionId
            );
        });
        if (!row) {
            return;
        }
        row.setAttribute("tabindex", "-1");
        row.addEventListener("blur", function handleBlur() {
            row.removeAttribute("tabindex");
            row.removeEventListener("blur", handleBlur);
        });
        try {
            row.focus({ preventScroll: true });
        } catch (_error) {
            row.focus();
        }
        row.scrollIntoView({ block: "center", inline: "nearest" });
    }

    function rememberFocusedPositionOnSubmit(table) {
        table.addEventListener("submit", function (event) {
            var target = event.target;
            if (!target || typeof target.closest !== "function") {
                return;
            }
            var form = target.closest("form");
            if (!form) {
                return;
            }
            if (form.getAttribute("data-focus-restore-on-redirect") !== "true") {
                removeSessionStorage(FOCUS_POSITION_STORAGE_KEY);
                return;
            }
            var row = form.closest("tr[data-position-id]");
            if (!row || !row.dataset.positionId) {
                return;
            }
            writeSessionStorage(FOCUS_POSITION_STORAGE_KEY, row.dataset.positionId);
        });
    }

    function sortTable(table) {
        var tbody = table.tBodies[0];
        var headers = Array.from(
            table.querySelectorAll("[data-sort-header='true']")
        );
        // Stamp each row with its load-order index once so the third click
        // ("reset") can restore the original order. Rows that get swapped in
        // place later carry this attribute forward, so it stays stable.
        Array.from(tbody.rows).forEach(function (row, index) {
            if (!("originalIndex" in row.dataset)) {
                row.dataset.originalIndex = String(index);
            }
        });

        headers.forEach(function (header) {
            header.addEventListener("click", function () {
                var currentDirection = header.dataset.sortDirection || "";
                var nextDirection =
                    currentDirection === ""
                        ? "asc"
                        : currentDirection === "asc"
                          ? "desc"
                          : null;

                updateHeaderState(headers, header, nextDirection);
                sortRows(tbody, header, nextDirection);
                persistSortState(header, nextDirection);
            });
        });

        var savedSort = loadSortState(headers);
        if (savedSort) {
            updateHeaderState(headers, savedSort.header, savedSort.direction);
            sortRows(tbody, savedSort.header, savedSort.direction);
        }

        rememberFocusedPositionOnSubmit(table);
        focusPositionFromStorage(table);
    }

    document.addEventListener("DOMContentLoaded", function () {
        var table = document.querySelector("[data-sortable-table='true']");
        if (!table) {
            return;
        }

        sortTable(table);
    });
})();
