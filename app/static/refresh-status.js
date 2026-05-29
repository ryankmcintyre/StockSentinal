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
        var MIN_INTERVAL_MS = 2000;
        var MAX_INTERVAL_MS = 5000;

        var parsedTimeout = parseInt(root.dataset.pollTimeoutMs, 10);
        var timeoutMs =
            isNaN(parsedTimeout) || parsedTimeout <= 0
                ? DEFAULT_TIMEOUT_MS
                : parsedTimeout;
        var stopAt = Date.now() + timeoutMs;
        var intervalMs = MIN_INTERVAL_MS;
        var pollInFlight = false;
        var timerId = null;

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

        function scheduleNext() {
            if (Date.now() >= stopAt) {
                stopPolling();
                showTimeoutMessage();
                // Reload once so the user sees the most recent state and any
                // per-position errors, rather than a frozen "Refreshing…" UI.
                window.location.reload();
                return;
            }
            timerId = setTimeout(poll, intervalMs);
            // Gentle backoff to reduce request volume on slow refreshes.
            intervalMs = Math.min(intervalMs + 1000, MAX_INTERVAL_MS);
        }

        function poll() {
            if (pollInFlight) {
                scheduleNext();
                return;
            }
            pollInFlight = true;
            fetch("/api/refresh-status", {
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
                    if (!payload.any_in_progress) {
                        stopPolling();
                        window.location.reload();
                        return;
                    }
                    scheduleNext();
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
