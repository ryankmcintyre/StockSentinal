(function () {
    function normalizeValue(cell, sortType) {
        var rawValue = cell.dataset.sortValue || cell.textContent || "";

        if (sortType === "number") {
            var parsed = Number(rawValue);
            return Number.isNaN(parsed) ? Number.POSITIVE_INFINITY : parsed;
        }

        return rawValue.trim().toLowerCase();
    }

    function stampOriginalIndexes(tbody) {
        Array.from(tbody.rows).forEach(function (row, index) {
            if (!("originalIndex" in row.dataset)) {
                row.dataset.originalIndex = String(index);
            }
        });
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

    function resetTableOrder(tbody) {
        Array.from(tbody.rows)
            .slice()
            .sort(function (left, right) {
                return (
                    Number(left.dataset.originalIndex) -
                    Number(right.dataset.originalIndex)
                );
            })
            .forEach(function (row) {
                tbody.appendChild(row);
            });
    }

    function updateFilterState(filters, activeFilter) {
        filters.forEach(function (filter) {
            var isActive = filter.dataset.summaryFilter === activeFilter;
            filter.setAttribute("aria-pressed", isActive ? "true" : "false");
            filter.classList.toggle("is-active", isActive);
        });
    }

    function applyFilter(tbody, activeFilter) {
        Array.from(tbody.rows).forEach(function (row) {
            row.hidden =
                activeFilter !== "total" &&
                (row.dataset.verdict || "").toLowerCase() !== activeFilter;
        });
    }

    function wireSummaryFilters(table, headers) {
        var tbody = table.tBodies[0];
        var filters = Array.from(
            document.querySelectorAll("[data-summary-filter]")
        );
        var activeFilter = "total";

        function syncFilterUi() {
            updateFilterState(filters, activeFilter);
            applyFilter(tbody, activeFilter);
        }

        if (!filters.length) {
            return;
        }

        filters.forEach(function (filter) {
            filter.addEventListener("click", function () {
                activeFilter = filter.dataset.summaryFilter || "total";
                if (activeFilter === "total") {
                    updateHeaderState(headers, null, null);
                    resetTableOrder(tbody);
                }
                syncFilterUi();
            });
        });

        document.addEventListener("portfolio:rows-updated", function () {
            syncFilterUi();
        });

        syncFilterUi();
    }

    function sortTable(table) {
        var tbody = table.tBodies[0];
        var headers = Array.from(
            table.querySelectorAll("[data-sort-header='true']")
        );
        // Stamp each row with its load-order index once so the third click
        // ("reset") can restore the original order. Rows that get swapped in
        // place later carry this attribute forward, so it stays stable.
        stampOriginalIndexes(tbody);

        wireSummaryFilters(table, headers);

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

                // Read rows from the live DOM on every click so in-place row
                // replacements (after a refresh) are respected and detached,
                // stale rows are never re-appended.
                var rows = Array.from(tbody.rows);

                if (nextDirection === null) {
                    resetTableOrder(tbody);
                    return;
                }

                var columnIndex = Number(header.dataset.sortColumn);
                var sortType = header.dataset.sortType || "text";
                var sortedRows = rows.slice().sort(function (left, right) {
                    var leftValue = normalizeValue(left.cells[columnIndex], sortType);
                    var rightValue = normalizeValue(right.cells[columnIndex], sortType);

                    if (leftValue < rightValue) {
                        return nextDirection === "asc" ? -1 : 1;
                    }
                    if (leftValue > rightValue) {
                        return nextDirection === "asc" ? 1 : -1;
                    }
                    return Number(left.dataset.originalIndex) - Number(right.dataset.originalIndex);
                });

                sortedRows.forEach(function (row) {
                    tbody.appendChild(row);
                });
            });
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        var table = document.querySelector("[data-sortable-table='true']");
        if (!table) {
            return;
        }

        sortTable(table);
    });
})();
