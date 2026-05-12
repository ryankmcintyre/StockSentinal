(function () {
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

    function sortTable(table) {
        var tbody = table.tBodies[0];
        var headers = Array.from(
            table.querySelectorAll("[data-sort-header='true']")
        );
        var originalRows = Array.from(tbody.rows);

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

                if (nextDirection === null) {
                    originalRows.forEach(function (row) {
                        tbody.appendChild(row);
                    });
                    return;
                }

                var columnIndex = Number(header.dataset.sortColumn);
                var sortType = header.dataset.sortType || "text";
                var sortedRows = originalRows.slice().sort(function (left, right) {
                    var leftValue = normalizeValue(left.cells[columnIndex], sortType);
                    var rightValue = normalizeValue(right.cells[columnIndex], sortType);

                    if (leftValue < rightValue) {
                        return nextDirection === "asc" ? -1 : 1;
                    }
                    if (leftValue > rightValue) {
                        return nextDirection === "asc" ? 1 : -1;
                    }
                    return originalRows.indexOf(left) - originalRows.indexOf(right);
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
