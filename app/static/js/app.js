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
  const button = event.target.closest("[data-reveal]");
  if (!button) return;
  const secret = button.parentElement.querySelector(".secret");
  const hidden = secret.textContent === "Hidden";
  secret.textContent = hidden ? secret.dataset.secret : "Hidden";
  button.textContent = hidden ? "Hide" : "Show";
});
