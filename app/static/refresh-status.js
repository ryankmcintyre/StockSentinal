(function () {
    function markFormBusy(form) {
        var submitButtons = form.querySelectorAll("button[type='submit']");
        submitButtons.forEach(function (button) {
            button.classList.add("btn-busy");
            button.setAttribute("aria-busy", "true");
            button.disabled = true;
        });
    }

    function wireSubmitCues() {
        var forms = document.querySelectorAll("form[data-api-submit='true']");
        forms.forEach(function (form) {
            form.addEventListener("submit", function (event) {
                var confirmMessage = form.dataset.confirmMessage;
                if (confirmMessage && !window.confirm(confirmMessage)) {
                    event.preventDefault();
                    return;
                }
                markFormBusy(form);
            });
        });
    }

    function wireRefreshStatusPolling() {
        var root = document.getElementById("refresh-status-root");
        if (!root || root.dataset.refreshStatusEnabled !== "true") {
            return;
        }
        if (root.dataset.anyRefreshInProgress !== "true") {
            return;
        }

        // Keep polling until the server reports the refresh is done. The
        // ceiling is aligned with the backend stale-refresh timeout (plus a
        // margin) so the UI never gives up while a refresh is still running.
        var DEFAULT_TIMEOUT_MS = 420000; // 7 minutes
        // Poll quickly for the first few rounds to catch the common-case
        // ~1-2s refresh, then back off to reduce request volume on slow ones.
        var FAST_INTERVAL_MS = 500;
        var FAST_POLL_COUNT = 4;
        var MAX_INTERVAL_MS = 5000;

        var parsedTimeout = parseInt(root.dataset.pollTimeoutMs, 10);
        var timeoutMs =
            isNaN(parsedTimeout) || parsedTimeout <= 0
                ? DEFAULT_TIMEOUT_MS
                : parsedTimeout;
        var stopAt = Date.now() + timeoutMs;
        var pollCount = 0;
        var intervalMs = FAST_INTERVAL_MS;
        var pollInFlight = false;
        var timerId = null;

        // Track the positions that were refreshing when the page loaded so we
        // can patch each row in place as it completes.
        var pending = {};
        var rows = document.querySelectorAll(
            "tr[data-refresh-in-progress='true']"
        );
        rows.forEach(function (row) {
            var id = row.getAttribute("data-position-id");
            if (id) {
                pending[id] = true;
            }
        });

        function pendingIds() {
            return Object.keys(pending);
        }

        function stopPolling() {
            if (timerId !== null) {
                clearTimeout(timerId);
                timerId = null;
            }
        }

        function showTimeoutMessage() {
            var banner = document.getElementById("refresh-progress-banner");
            if (banner) {
                banner.textContent =
                    "Market data is still updating. Reload the page to check " +
                    "the latest status.";
            }
        }

        function hideBannerWhenDone() {
            if (pendingIds().length > 0) {
                return;
            }
            var banner = document.getElementById("refresh-progress-banner");
            if (banner && banner.parentNode) {
                banner.parentNode.removeChild(banner);
            }
        }

        function updateSummary(summary) {
            if (!summary) {
                return;
            }
            ["total", "sell", "trim", "hold"].forEach(function (key) {
                var el = document.querySelector(
                    "[data-summary-count='" + key + "']"
                );
                if (el && typeof summary[key] !== "undefined") {
                    el.textContent = summary[key];
                }
            });
        }

        function patchRows(ids) {
            if (!ids.length) {
                return Promise.resolve();
            }
            return fetch(
                "/api/positions/rows?ids=" + encodeURIComponent(ids.join(",")),
                {
                    method: "GET",
                    headers: { Accept: "application/json" },
                    cache: "no-store",
                }
            )
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("row fetch failed");
                    }
                    return response.json();
                })
                .then(function (payload) {
                    (payload.rows || []).forEach(function (item) {
                        var existing = document.querySelector(
                            "tr[data-position-id='" + item.id + "']"
                        );
                        if (existing && item.row_html) {
                            var template = document.createElement("tbody");
                            template.innerHTML = item.row_html.trim();
                            var newRow = template.firstElementChild;
                            if (newRow) {
                                // Preserve the load-order index so the sort
                                // "reset" state still restores the original
                                // order after a row is swapped in place.
                                var originalIndex =
                                    existing.getAttribute("data-original-index");
                                if (originalIndex !== null && originalIndex !== "") {
                                    newRow.setAttribute(
                                        "data-original-index",
                                        originalIndex
                                    );
                                }
                                existing.parentNode.replaceChild(
                                    newRow,
                                    existing
                                );
                            }
                        }
                    });
                    // A single authoritative summary from the batch response
                    // avoids stale counters from out-of-order per-row updates.
                    updateSummary(payload.summary);
                });
        }

        function scheduleNext() {
            if (Date.now() >= stopAt) {
                stopPolling();
                // Surface the timeout message and let the user decide when to
                // reload, rather than discarding it with an immediate reload.
                showTimeoutMessage();
                return;
            }
            timerId = setTimeout(poll, intervalMs);
            pollCount += 1;
            // Gentle backoff once the fast-poll phase is over.
            if (pollCount >= FAST_POLL_COUNT) {
                intervalMs = Math.min(intervalMs + 1000, MAX_INTERVAL_MS);
            }
        }

        function poll() {
            if (pollInFlight) {
                scheduleNext();
                return;
            }
            var ids = pendingIds();
            if (ids.length === 0) {
                stopPolling();
                hideBannerWhenDone();
                return;
            }
            pollInFlight = true;
            fetch("/api/refresh-status?ids=" + encodeURIComponent(ids.join(",")), {
                method: "GET",
                headers: { Accept: "application/json" },
                cache: "no-store",
            })
                .then(function (response) {
                    if (!response.ok) {
                        throw new Error("refresh status poll failed");
                    }
                    return response.json();
                })
                .then(function (payload) {
                    var statusById = {};
                    (payload.positions || []).forEach(function (item) {
                        statusById[String(item.id)] = item.in_progress;
                    });
                    var completed = ids.filter(function (id) {
                        // Completed if the server says not in progress, or the
                        // position no longer appears in the status response.
                        return statusById[id] !== true;
                    });
                    completed.forEach(function (id) {
                        delete pending[id];
                    });
                    // Patch all completed rows with one batch request so the
                    // server runs a single enrich-all (not one per row) and we
                    // apply a single authoritative summary.
                    return patchRows(completed)
                        .catch(function () {
                            // If the batch patch fails, the rows were already
                            // marked done so polling can stop; a manual reload
                            // will reconcile any missed update.
                        })
                        .then(function () {
                            if (pendingIds().length === 0) {
                                stopPolling();
                                hideBannerWhenDone();
                                return;
                            }
                            scheduleNext();
                        });
                })
                .catch(function () {
                    // Transient network failures are non-fatal; keep polling.
                    scheduleNext();
                })
                .finally(function () {
                    pollInFlight = false;
                });
        }

        poll();
    }

    document.addEventListener("DOMContentLoaded", function () {
        wireSubmitCues();
        wireRefreshStatusPolling();
    });
})();
