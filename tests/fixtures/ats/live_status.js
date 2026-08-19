// Runs the standing thank-you panel of `live_status.html`, and answers the
// press. Nothing here posts anywhere: every mode cancels the submission, so
// the only thing under test is what the page *says* afterwards.
//
// Modes, from the query string:
//
//   tick        the panel rewrites its own text on a timer. The press is
//               swallowed, so nothing about this application is confirmed.
//   rebuild     the panel is thrown away and built again on a timer, so the
//               node is new every time while its id is not. The press is
//               swallowed too.
//   anonymous   the panel has no id of its own, and rewrites its text on a
//               timer. Nothing about the markup identifies it, so whatever
//               identity a reader gives it has to be one it can find again.
//   ghost       both at once: no id, thrown away and rebuilt on a timer, and
//               worded differently every time. Nothing about the node
//               survives, so the only thing left to recognise it by is where
//               in the document it is.
//   optimistic  the panel counts this application the instant the button is
//               pressed, before anybody has accepted it. Still swallowed.
//   confirms    the panel ticks, and the press fills the neutral status
//               region with a real confirmation — the ordinary case, which
//               has to keep working.
(() => {
  const CONFIRMATION_TEXT = "Your application was submitted. Thank you for applying.";
  const TICK_MS = 60;

  const mode = new URLSearchParams(location.search).get("mode") || "tick";
  const wrapper = document.getElementById("applied-count-wrapper");
  const form = document.getElementById("application-form");
  const status = document.getElementById("form-status");

  let count = 1;

  if (mode === "anonymous" || mode === "ghost") {
    document.getElementById("applied-count").removeAttribute("id");
  }

  const wording = () =>
    `Thank you for applying to ${count} role${count === 1 ? "" : "s"} this month.`;

  const panel = () => wrapper.querySelector('[role="status"]');

  const inPlace = () => {
    panel().textContent = wording();
  };

  // The same panel, in a node that did not exist a moment ago: a framework
  // rerendering its subtree rather than editing a text node. The id is the
  // only thing the two nodes share, which is exactly what has to be enough
  // for the panel to still count as one region.
  const rebuilt = () => {
    wrapper.innerHTML =
      `<div id="applied-count" role="status" aria-live="polite">${wording()}</div>`;
  };

  // And the same panel with nothing at all to recognise it by: no id, and a
  // node that is new every time. Its position in the document is the only
  // thing that holds still.
  const ghosted = () => {
    wrapper.innerHTML =
      `<div role="status" aria-live="polite">${wording()}</div>`;
  };

  const repaint =
    mode === "rebuild" ? rebuilt : mode === "ghost" ? ghosted : inPlace;

  const bump = () => {
    count += 1;
    repaint();
  };

  if (mode !== "optimistic") {
    setInterval(bump, TICK_MS);
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (mode === "optimistic") {
      // The page congratulating itself. Nothing has been accepted.
      bump();
      return;
    }
    if (mode === "confirms") {
      // A tick later, so the submitter observes the region changing rather
      // than having changed.
      setTimeout(() => {
        status.textContent = CONFIRMATION_TEXT;
      }, 30);
    }
  });
})();
