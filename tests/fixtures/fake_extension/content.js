(() => {
  const HOST_ID = "jobright-stub-host";
  const PANEL_ID = "jobright-stub-panel";
  const STATUS_ID = "jobright-stub-status";
  const BUTTON_ID = "jobright-stub-autofill";
  const contractBySlug = globalThis.JOBRIGHT_STUB_GAP_CONTRACT ?? {};

  function detectActiveContract() {
    for (const [slug, contract] of Object.entries(contractBySlug)) {
      if (document.querySelector(contract.detect.selector)) {
        return { slug, contract };
      }
    }
    return null;
  }

  function ensurePanel() {
    let host = document.getElementById(HOST_ID);
    if (host?.shadowRoot) {
      return host.shadowRoot;
    }

    host = document.createElement("div");
    host.id = HOST_ID;
    document.documentElement.appendChild(host);

    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host { all: initial; }
        .panel {
          position: fixed;
          top: 16px;
          right: 16px;
          z-index: 2147483647;
          background: #111827;
          color: #f9fafb;
          border-radius: 8px;
          padding: 12px;
          width: 220px;
          font: 14px/1.4 system-ui, sans-serif;
          box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
        }
        button {
          width: 100%;
          border: 0;
          border-radius: 6px;
          padding: 8px 12px;
          background: #2563eb;
          color: #fff;
          cursor: pointer;
          font: inherit;
        }
        button:disabled { opacity: 0.6; cursor: default; }
        .status { margin-top: 8px; min-height: 1.4em; }
      </style>
      <div class="panel" id="${PANEL_ID}">
        <button id="${BUTTON_ID}" type="button">Autofill</button>
        <div class="status" id="${STATUS_ID}">Ready</div>
      </div>
    `;

    shadow.getElementById(BUTTON_ID).addEventListener("click", () => {
      void runAutofill(shadow);
    });

    return shadow;
  }

  function setStatus(shadow, message) {
    shadow.getElementById(STATUS_ID).textContent = message;
  }

  function fieldKey(field) {
    return field.name || field.id || "";
  }

  function shouldSkipField(field, contract) {
    const key = fieldKey(field);
    if (!key) {
      return true;
    }
    if (
      key === contract.required_input_left_empty &&
      field.tagName === "INPUT"
    ) {
      return true;
    }
    if (
      key === contract.textarea_left_empty &&
      field.tagName === "TEXTAREA"
    ) {
      return true;
    }
    return false;
  }

  function fillField(field, contract) {
    const key = fieldKey(field);
    if (!key || shouldSkipField(field, contract)) {
      return false;
    }
    if (
      Object.prototype.hasOwnProperty.call(contract.partial_values, key)
    ) {
      field.value = contract.partial_values[key];
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    return false;
  }

  function countEmptyRequiredControls() {
    return Array.from(
      document.querySelectorAll("input, textarea, select"),
    ).filter((field) => field.required && !String(field.value ?? "").trim())
      .length;
  }

  async function runAutofill(shadow) {
    const active = detectActiveContract();
    if (!active) {
      setStatus(shadow, "No ATS gap contract matched");
      return;
    }

    const { contract } = active;
    const button = shadow.getElementById(BUTTON_ID);
    button.disabled = true;
    setStatus(shadow, "Autofill in progress…");

    const fields = Array.from(
      document.querySelectorAll("input, textarea, select"),
    );
    let filled = 0;

    for (const field of fields) {
      await new Promise((resolve) => setTimeout(resolve, 25));
      if (fillField(field, contract)) {
        filled += 1;
        setStatus(shadow, `Filled ${filled} field(s)…`);
      }
    }

    const remainingRequired = countEmptyRequiredControls();
    setStatus(
      shadow,
      `Autofill complete (${filled} filled, ${remainingRequired} required empty)`,
    );
    button.disabled = false;

    document.dispatchEvent(
      new CustomEvent("jobright-stub-complete", {
        detail: { filled, remainingRequired },
      }),
    );
  }

  ensurePanel();
})();
