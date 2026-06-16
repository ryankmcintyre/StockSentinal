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

    // Shared poller so both the on-load (any-in-progress) path and the
    // client-initiated single-row refresh path feed the same polling loop.
    var poller = (function () {
        // Diagnostic timeline logger — flip on via ?refresh_debug=1 (one-off)
        // or localStorage.setItem('refreshDebug','1') (persistent) to see when
        // each poll fires, what the backend reported, and when the row patch
        // resolves. Off by default so production users get no console noise.
        var debugEnabled = (function () {
            try {
                if (window.location.search.indexOf("refresh_debug=1") !== -1) {
                    return true;
                }
                return window.localStorage &&
                    window.localStorage.getItem("refreshDebug") === "1";
            } catch (e) {
                return false;
            }
        })();
        var debugT0 = null;
        function debugLog() {
            if (!debugEnabled) {
                return;
            }
            if (debugT0 === null) {
                debugT0 = performance.now();
            }
            var elapsed = (performance.now() - debugT0).toFixed(0);
            var args = Array.prototype.slice.call(arguments);
            args.unshift("[refresh-debug t+" + elapsed + "ms]");
            // eslint-disable-next-line no-console
            console.log.apply(console, args);
        }

        var DEFAULT_TIMEOUT_MS = 420000; // 7 minutes
        var FAST_INTERVAL_MS = 500;
        var FAST_POLL_COUNT = 4;
        var MAX_INTERVAL_MS = 5000;

        var pending = {};
        var stopAt = 0;
        var pollCount = 0;
        var intervalMs = FAST_INTERVAL_MS;
        var pollInFlight = false;
        var timerId = null;
        var running = false;
        var progressTotal = 0;
        var progressCompleted = 0;

        function timeoutMs() {
            var root = document.getElementById("refresh-status-root");
            var parsed = root ? parseInt(root.dataset.pollTimeoutMs, 10) : NaN;
            return isNaN(parsed) || parsed <= 0 ? DEFAULT_TIMEOUT_MS : parsed;
        }

        function pendingIds() {
            return Object.keys(pending);
        }

        function setRowSpinning(id, spinning) {
            var row = document.querySelector(
                "tr[data-position-id='" + id + "']"
            );
            if (!row) {
                return;
            }
            var button = row.querySelector("[data-refresh-button='true']");
            if (!button) {
                return;
            }
            if (spinning) {
                button.classList.add("btn-refresh-spinning");
                button.setAttribute("aria-busy", "true");
                button.disabled = true;
            } else {
                button.classList.remove("btn-refresh-spinning");
                button.removeAttribute("aria-busy");
                button.disabled = false;
            }
        }

        function stopPolling() {
            if (timerId !== null) {
                clearTimeout(timerId);
                timerId = null;
            }
            running = false;
        }

        function showTimeoutMessage() {
            var banner = document.getElementById("refresh-progress-banner");
            if (banner) {
                var text = banner.querySelector("[data-refresh-progress-text]");
                if (text) {
                    text.textContent =
                        "Market data is still updating in the background.";
                } else {
                    banner.textContent =
                        "Market data is still updating in the background.";
                }
            }
        }

        function updateProgress() {
            var banner = document.getElementById("refresh-progress-banner");
            if (!banner || progressTotal <= 0) {
                return;
            }
            var completed = Math.min(progressCompleted, progressTotal);
            var percent = Math.round((completed / progressTotal) * 100);
            var text = banner.querySelector("[data-refresh-progress-text]");
            var percentText = banner.querySelector(
                "[data-refresh-progress-percent]"
            );
            var bar = banner.querySelector("[data-refresh-progressbar]");
            var fill = banner.querySelector("[data-refresh-progress-fill]");
            if (text) {
                text.textContent =
                    completed +
                    " of " +
                    progressTotal +
                    " positions refreshed.";
            }
            if (percentText) {
                percentText.textContent = percent + "% complete";
            }
            if (bar) {
                bar.setAttribute("aria-valuemax", String(progressTotal));
                bar.setAttribute("aria-valuenow", String(completed));
            }
            if (fill) {
                fill.style.width = percent + "%";
            }
        }

        function setProgressTotal(total) {
            if (progressTotal > 0 || total <= 0) {
                return;
            }
            progressTotal = total;
            progressCompleted = 0;
            updateProgress();
        }

        function completeProgressWhenDone() {
            if (pendingIds().length > 0) {
                return;
            }
            if (progressTotal > 0) {
                progressCompleted = progressTotal;
                updateProgress();
            }
            document
                .querySelectorAll("[data-refresh-all-button='true']")
                .forEach(function (button) {
                    button.disabled = false;
                    button.removeAttribute("title");
                });
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
                    document.dispatchEvent(new Event("portfolio:rows-updated"));
                });
        }

        function scheduleNext() {
            if (Date.now() >= stopAt) {
                stopPolling();
                // Surface the timeout message and let the user decide what to
                // do next, rather than discarding state with an auto-reload.
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
                completeProgressWhenDone();
                return;
            }
            pollInFlight = true;
            debugLog("poll fire ids=" + ids.join(",") + " interval=" + intervalMs + "ms");
            var pollSentAt = performance.now();
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
                    debugLog(
                        "poll response in " +
                            (performance.now() - pollSentAt).toFixed(0) +
                            "ms any_in_progress=" + payload.any_in_progress
                    );
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
                    if (completed.length > 0) {
                        progressCompleted += completed.length;
                        updateProgress();
                        debugLog("detected completed ids=" + completed.join(","));
                    }
                    // Patch all completed rows with one batch request so the
                    // server runs a single enrich-all (not one per row) and we
                    // apply a single authoritative summary. Patching re-renders
                    // the row, which clears the spinning refresh icon.
                    var patchStartedAt = performance.now();
                    return patchRows(completed)
                        .then(function () {
                            if (completed.length > 0) {
                                debugLog(
                                    "patchRows resolved in " +
                                        (performance.now() - patchStartedAt).toFixed(0) +
                                        "ms"
                                );
                            }
                        })
                        .catch(function () {
                            // If the batch patch fails, stop spinning the rows
                            // we marked done so the cue does not get stuck.
                            completed.forEach(function (id) {
                                setRowSpinning(id, false);
                            });
                        })
                        .then(function () {
                            if (pendingIds().length === 0) {
                                stopPolling();
                                completeProgressWhenDone();
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

        function add(id) {
            if (!id) {
                return;
            }
            pending[String(id)] = true;
            setRowSpinning(id, true);
        }

        function start() {
            // (Re)start the fast-poll cadence whenever new work is queued.
            progressTotal = 0;
            progressCompleted = 0;
            setProgressTotal(pendingIds().length);
            stopAt = Date.now() + timeoutMs();
            pollCount = 0;
            intervalMs = FAST_INTERVAL_MS;
            debugT0 = performance.now();
            debugLog("start ids=" + pendingIds().join(","));
            if (running) {
                return;
            }
            running = true;
            poll();
        }

        return {
            add: add,
            start: start,
            setRowSpinning: setRowSpinning,
            hasPending: function () {
                return pendingIds().length > 0;
            },
        };
    })();

    function wireRefreshStatusPolling() {
        var root = document.getElementById("refresh-status-root");
        if (!root || root.dataset.refreshStatusEnabled !== "true") {
            return;
        }
        if (root.dataset.anyRefreshInProgress !== "true") {
            return;
        }
        // Seed the poller with rows that were already refreshing on load so the
        // in-place patch + spinner cue resume correctly after a manual reload.
        var rows = document.querySelectorAll(
            "tr[data-refresh-in-progress='true']"
        );
        rows.forEach(function (row) {
            var id = row.getAttribute("data-position-id");
            if (id) {
                poller.add(id);
            }
        });
        if (poller.hasPending()) {
            poller.start();
        }
    }

    function handleSingleRefreshSubmit(form) {
        var row = form.closest("tr[data-position-id]");
        var id = row ? row.getAttribute("data-position-id") : null;
        if (!id) {
            form.submit();
            return;
        }
        poller.setRowSpinning(id, true);
        var body = new FormData(form);
        fetch(form.getAttribute("action"), {
            method: "POST",
            headers: {
                Accept: "application/json",
                "X-Requested-With": "fetch",
            },
            body: body,
            cache: "no-store",
        })
            .then(function (response) {
                if (response.status === 429) {
                    // Refresh quota exceeded: fall back to the standard
                    // redirect so the existing flash banner surfaces the
                    // message (the only case that reloads the page).
                    poller.setRowSpinning(id, false);
                    window.location.href = "/?flash=refresh_limit";
                    return;
                }
                if (!response.ok) {
                    throw new Error("refresh request failed");
                }
                // Accepted: register the id and (re)start polling so the
                // row is patched in place when the refresh completes.
                poller.add(id);
                poller.start();
            })
            .catch(function () {
                // On unexpected failure fall back to a full submit so
                // the operator still gets feedback.
                poller.setRowSpinning(id, false);
                form.submit();
            });
    }

    function wireSingleRefreshForms() {
        // Use one delegated submit listener rather than binding each form so
        // rows swapped in by patchRows() (which replaces the whole <tr>,
        // including its form) keep the async path on subsequent refreshes
        // instead of regressing to a native POST + full page reload.
        document.addEventListener("submit", function (event) {
            var form = event.target;
            if (!form || typeof form.matches !== "function") {
                return;
            }
            if (!form.matches("form[data-refresh-form='true']")) {
                return;
            }
            // Progressive enhancement: without JS the form posts normally
            // and the server returns a 303 redirect (no-JS fallback).
            event.preventDefault();
            handleSingleRefreshSubmit(form);
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        wireSubmitCues();
        wireSingleRefreshForms();
        wireRefreshStatusPolling();
    });
})();
