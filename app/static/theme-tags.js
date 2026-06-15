(function () {
    function getCsrfToken(form) {
        var input = form.querySelector('input[name="csrf_token"]');
        return input ? input.value : "";
    }

    function appendTheme(picker, theme) {
        var options = picker.querySelector("[data-theme-options]");
        if (!options) {
            return;
        }
        var label = document.createElement("label");
        label.className = "theme-checkbox";

        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.name = "theme_ids";
        checkbox.value = String(theme.id);
        checkbox.checked = true;

        var text = document.createElement("span");
        text.textContent = theme.name;

        label.appendChild(checkbox);
        label.appendChild(text);
        options.appendChild(label);

        var emptyHint = picker.querySelector("[data-theme-empty-hint]");
        if (emptyHint) {
            emptyHint.remove();
        }
    }

    async function createTheme(picker) {
        var form = picker.closest("form");
        var input = picker.querySelector("[data-theme-new-input]");
        var button = picker.querySelector("[data-theme-create-button]");
        var status = picker.querySelector("[data-theme-create-status]");
        var name = input ? input.value.trim() : "";
        if (!form || !input || !name) {
            return;
        }
        if (button) {
            button.disabled = true;
        }
        if (status) {
            status.textContent = "Creating…";
        }
        try {
            var response = await fetch("/themes", {
                method: "POST",
                headers: {
                    "accept": "application/json",
                    "content-type": "application/json",
                    "x-csrf-token": getCsrfToken(form),
                    "x-requested-with": "fetch"
                },
                body: JSON.stringify({ name: name })
            });
            var payload = await response.json();
            if (!response.ok) {
                throw new Error(payload.error || "Could not create theme");
            }
            appendTheme(picker, payload);
            input.value = "";
            if (status) {
                status.textContent = "Created " + payload.name + ".";
            }
        } catch (error) {
            if (status) {
                status.textContent = error.message;
            }
        } finally {
            if (button) {
                button.disabled = false;
            }
        }
    }

    document.querySelectorAll("[data-theme-picker]").forEach(function (picker) {
        var input = picker.querySelector("[data-theme-new-input]");
        var button = picker.querySelector("[data-theme-create-button]");
        if (button) {
            button.addEventListener("click", function () {
                createTheme(picker);
            });
        }
        if (input) {
            input.addEventListener("keydown", function (event) {
                if (event.key === "Enter") {
                    event.preventDefault();
                    createTheme(picker);
                }
            });
        }
    });
}());
