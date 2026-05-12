(function () {
    var tickerInput = document.getElementById("ticker");
    var companyInput = document.getElementById("company_name");
    var lookupStatus = document.getElementById("ticker_lookup_status");
    var priceStatus = document.getElementById("ticker_lookup_price");
    var picker = document.getElementById("ticker_lookup_picker");
    var pickerOptions = document.getElementById("ticker_lookup_options");
    var submitButton =
        document.getElementById("add-position-submit") ||
        document.getElementById("edit-position-submit");

    if (!tickerInput || !companyInput || !lookupStatus || !priceStatus || !picker || !pickerOptions || !submitButton) {
        console.warn("Ticker lookup UI did not initialize because required form elements were not found.");
        return;
    }

    var lastResolvedTicker = tickerInput ? tickerInput.value.trim().toUpperCase() : "";
    var requestCounter = 0;

    function setPickerVisible(visible) {
        picker.hidden = !visible;
    }

    function setPrice(price) {
        if (typeof price === "number" && !isNaN(price) && isFinite(price)) {
            priceStatus.textContent = "Current price: $" + price.toFixed(2);
            priceStatus.hidden = false;
            return;
        }

        priceStatus.textContent = "";
        priceStatus.hidden = true;
    }

    function resetLookupState() {
        lastResolvedTicker = "";
        setPickerVisible(false);
        pickerOptions.innerHTML = "";
        setPrice(null);
        submitButton.disabled = false;
    }

    function applyMatchSelection(match, ticker) {
        companyInput.value = match.name || "";
        lookupStatus.textContent = "";
        setPickerVisible(false);
        pickerOptions.innerHTML = "";
        submitButton.disabled = false;
        lastResolvedTicker = ticker;
    }

    function renderPicker(matches, ticker) {
        pickerOptions.innerHTML = "";

        matches.forEach(function (match) {
            var option = document.createElement("button");
            option.type = "button";
            option.className = "ticker-lookup-option";
            var title = document.createElement("strong");
            title.textContent = match.name || match.symbol || "";
            option.appendChild(title);

            var description = [];
            if (match.region) {
                description.push(match.region);
            }
            if (match.type) {
                description.push(match.type);
            }

            var meta = document.createElement("span");
            meta.className = "ticker-lookup-option-meta";
            meta.textContent =
                (match.symbol || "") +
                (description.length ? " — " + description.join(" · ") : "");
            option.appendChild(meta);

            option.addEventListener("click", function () {
                applyMatchSelection(match, ticker);
            });
            pickerOptions.appendChild(option);
        });

        lookupStatus.textContent = "Select the correct stock to continue.";
        setPickerVisible(true);
        submitButton.disabled = true;
    }

    tickerInput.addEventListener("blur", function () {
        var ticker = tickerInput.value.trim().toUpperCase();

        if (!ticker) {
            companyInput.value = "";
            lookupStatus.textContent = "";
            resetLookupState();
            return;
        }

        if (ticker === lastResolvedTicker && companyInput.value.trim()) {
            return;
        }

        tickerInput.value = ticker;
        companyInput.value = "";
        lookupStatus.textContent = "Looking up ticker…";
        resetLookupState();
        submitButton.disabled = true;

        requestCounter += 1;
        var requestId = requestCounter;

        fetch("/api/lookup/" + encodeURIComponent(ticker))
            .then(function (resp) {
                return resp.json().then(function (data) {
                    if (!resp.ok) {
                        throw new Error(data.error || "Lookup failed");
                    }
                    return data;
                });
            })
            .then(function (data) {
                if (requestId !== requestCounter) {
                    return;
                }

                setPrice(data.current_price);
                var matches = Array.isArray(data.matches) ? data.matches : [];

                if (matches.length > 1) {
                    renderPicker(matches, ticker);
                    return;
                }

                if (matches.length === 1) {
                    applyMatchSelection(matches[0], ticker);
                    return;
                }

                lookupStatus.textContent = "No results found for " + ticker + ".";
            })
            .catch(function (err) {
                if (requestId !== requestCounter) {
                    return;
                }

                console.warn("Ticker lookup failed:", err);
                lookupStatus.textContent = err.message || "Could not auto-detect company name. Please enter it manually.";
                submitButton.disabled = false;
            });
    });
})();
