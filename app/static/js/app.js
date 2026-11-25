// Small progressive enhancements; every page works without this file.
document.addEventListener("submit", (event) => {
  const message = event.target.dataset.confirm;
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-autosubmit]")) {
    event.target.form.submit();
  }
});

document.addEventListener("click", (event) => {
  const reveal = event.target.closest("[data-reveal]");
  if (reveal) {
    const secret = reveal.parentElement.querySelector(".secret");
    const hidden = secret.textContent === "Hidden";
    secret.textContent = hidden ? secret.dataset.secret : "Hidden";
    reveal.textContent = hidden ? "Hide" : "Show";
    return;
  }
  // The menu button on small screens opens and closes the side navigation.
  if (event.target.closest(".js-menu-toggle")) {
    document.querySelector(".l-navigation").classList.toggle("is-collapsed");
  }
});

// Add device: show an example of the chosen type's settings.
document.addEventListener("change", (event) => {
  if (!event.target.matches("[data-examples]")) return;
  const example = event.target.selectedOptions[0]?.dataset.example || "";
  const settings = document.getElementById("settings");
  if (settings) settings.placeholder = example;
});

// On small screens the side navigation starts closed.
if (window.matchMedia("(max-width: 619px)").matches) {
  document.querySelector(".l-navigation")?.classList.add("is-collapsed");
}

// Live updates: pages marked data-live reload their content when the server
// reports a change (app/views/events.py). Nothing is reloaded while you are
// typing in a form on the page, or while the tab is hidden; that happens later.
(() => {
  const main = document.querySelector("main[data-live]");
  if (!main || !window.EventSource) return;
  let typing = false;
  let waiting = false;
  let timer = null;
  let last = 0;

  main.addEventListener("input", () => {
    typing = true;
  });

  async function refresh() {
    if (document.hidden || typing) {
      waiting = true;
      return;
    }
    waiting = false;
    last = Date.now();
    let response;
    try {
      response = await fetch(location.href, { credentials: "same-origin" });
    } catch {
      return;
    }
    if (!response.ok || response.redirected) return;
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    for (const part of [".p-panel__header", ".p-panel__content"]) {
      const fresh = page.querySelector(`main ${part}`);
      const current = main.querySelector(part);
      if (fresh && current) current.replaceWith(fresh);
    }
  }

  function soon() {
    clearTimeout(timer);
    // Batch bursts of events; at most one reload every 1.5 seconds.
    timer = setTimeout(refresh, Math.max(400, 1500 - (Date.now() - last)));
  }

  const events = new EventSource(main.dataset.live);
  events.onmessage = soon;
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && waiting) soon();
  });
  main.addEventListener("focusout", () => {
    // Leaving an untouched form field is not typing.
    if (!main.querySelector("form :is(input, textarea):focus")) {
      typing = [...main.querySelectorAll("form input[type=text], form textarea")].some(
        (field) => field.value !== field.defaultValue,
      );
      if (!typing && waiting) soon();
    }
  });
  window.addEventListener("pagehide", () => events.close());
})();
