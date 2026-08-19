// Answers a submit locally, the way an ATS answers one remotely.
//
// The fixtures are served by a loopback file server with no handler behind
// them, so a real submit would navigate to the same page and lose the form —
// which a browser test could easily mistake for success. Instead this
// cancels the navigation and produces two of the three signals
// `PlaywrightSubmitter` accepts: a visible confirmation in a status region,
// and the disappearance of the form that was submitted.
//
// Native constraint validation is left alone on purpose. A required input
// the writer failed to fill blocks the submit event exactly as it would on a
// real form, so the integration test cannot pass by clicking a button on a
// half-filled page.
(() => {
  const CONFIRMATION_TEXT = "Your application was submitted. Thank you for applying.";
  const CONFIRMATION_ID = "fixture-submit-confirmation";

  const confirm = (form) => {
    const banner = document.createElement("div");
    banner.id = CONFIRMATION_ID;
    banner.setAttribute("role", "status");
    banner.setAttribute("aria-live", "polite");
    banner.textContent = CONFIRMATION_TEXT;
    form.parentNode.insertBefore(banner, form);
    form.remove();
  };

  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (!form || form.tagName !== "FORM") {
        return;
      }
      event.preventDefault();
      // A tick later, so the click's own event loop turn ends first and the
      // submitter observes the page changing rather than having changed.
      setTimeout(() => confirm(form), 30);
    },
    true,
  );
})();
