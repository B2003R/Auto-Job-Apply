// Builds the two open shadow roots `shadow_form.html` leaves empty, and
// answers the press the way a component-framework ATS does.
//
// A control inside a shadow root is not associated with the light-DOM form
// around its host, so neither native submission nor native constraint
// validation applies to it: the button does nothing on its own and the
// required input is not checked by anything. Both are done here instead,
// which keeps the property the loopback fixtures all share — a press on a
// form whose gap was never filled is refused, so a submitter cannot pass
// these tests by clicking a button on a page nobody wrote to.
(() => {
  const CONFIRMATION_TEXT = "Your application was submitted. Thank you for applying.";
  const CONFIRMATION_ID = "fixture-submit-confirmation";
  const PROBLEM_ID = "fixture-submit-problem";

  const attach = (hostId) =>
    document.getElementById(hostId).attachShadow({ mode: "open" });

  const fieldRoot = attach("last-name-host");
  fieldRoot.innerHTML = `
    <label for="last_name">Last name *</label>
    <input id="last_name" name="last_name" type="text" required>
  `;

  const submitRoot = attach("submit-host");
  submitRoot.innerHTML = `<button type="submit">Submit application</button>`;

  const refuse = (form) => {
    if (document.getElementById(PROBLEM_ID)) {
      return;
    }
    const problem = document.createElement("div");
    problem.id = PROBLEM_ID;
    problem.setAttribute("role", "alert");
    problem.textContent = "Last name is required.";
    form.parentNode.insertBefore(problem, form);
  };

  const confirm = (form) => {
    const banner = document.createElement("div");
    banner.id = CONFIRMATION_ID;
    banner.setAttribute("role", "status");
    banner.setAttribute("aria-live", "polite");
    banner.textContent = CONFIRMATION_TEXT;
    form.parentNode.insertBefore(banner, form);
    form.remove();
  };

  submitRoot.querySelector("button").addEventListener("click", (event) => {
    event.preventDefault();
    const form = document.getElementById("application-form");
    const lastName = fieldRoot.querySelector("#last_name");
    if (!String(lastName.value ?? "").trim()) {
      refuse(form);
      return;
    }
    // A tick later, so the click's own event loop turn ends first and the
    // submitter observes the page changing rather than having changed.
    setTimeout(() => confirm(form), 30);
  });
})();
