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

// On small screens the side navigation starts closed.
if (window.matchMedia("(max-width: 619px)").matches) {
  document.querySelector(".l-navigation")?.classList.add("is-collapsed");
}
