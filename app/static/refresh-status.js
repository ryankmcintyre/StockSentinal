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

        var stopAt = Date.now() + 120000;
        var pollInFlight = false;

        function poll() {
            if (pollInFlight) {
                return;
            }
            if (Date.now() > stopAt) {
                clearInterval(timerId);
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
                        clearInterval(timerId);
                        window.location.reload();
                    }
                })
                .catch(function () {
                    // Keep polling until timeout; transient network failures are non-fatal.
                })
                .finally(function () {
                    pollInFlight = false;
                });
        }

        var timerId = setInterval(poll, 2000);
        poll();
    }

    document.addEventListener("DOMContentLoaded", function () {
        wireSubmitCues();
        wireRefreshStatusPolling();
    });
})();
