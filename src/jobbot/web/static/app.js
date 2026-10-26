// Small enhancements on top of htmx: keyboard triage, toasts, repeatable form rows.
(function () {
  "use strict";

  // --- Toasts (servers send `HX-Trigger: {"toast": "..."}`) -----------------------------
  let toastTimer = null;
  function showToast(message) {
    let el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast";
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
  }
  document.body.addEventListener("toast", (e) => showToast(e.detail.value || e.detail));
  document.body.addEventListener("htmx:responseError", (e) => {
    const text = (e.detail.xhr && e.detail.xhr.responseText) || "Something went wrong";
    showToast(text.length > 200 ? "Something went wrong" : text);
  });

  // --- Keyboard triage on the jobs list ---------------------------------------------------
  let index = -1;
  const jobs = () => Array.from(document.querySelectorAll(".job-list .job"));
  function select(i, scroll) {
    const list = jobs();
    if (!list.length) { index = -1; return; }
    index = Math.max(0, Math.min(i, list.length - 1));
    list.forEach((el, n) => el.classList.toggle("selected", n === index));
    if (scroll) list[index].scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.target.closest && e.target.closest("input, textarea, select, [contenteditable]")) return;
    const list = jobs();
    if (!list.length) return;
    if (e.key === "j") { select(index + 1, true); e.preventDefault(); return; }
    if (e.key === "k") { select(index - 1, true); e.preventDefault(); return; }
    const current = list[index];
    if (!current) return;
    const press = (action) => {
      const el = current.querySelector(`[data-action="${action}"]`);
      if (el) { el.click(); e.preventDefault(); }
    };
    if (e.key === "s") press("saved");
    else if (e.key === "a") press("applied");
    else if (e.key === "d") press("dismissed");
    else if (e.key === "o") press("open");
    else if (e.key === "Enter" && current.dataset.href) { window.location.href = current.dataset.href; }
  });
  document.body.addEventListener("htmx:afterSettle", () => {
    if (index >= 0) select(index, false);
  });
  document.addEventListener("click", (e) => {
    const card = e.target.closest && e.target.closest(".job-list .job");
    if (card) select(jobs().indexOf(card), false);
  });

  // --- Confirmations for plain (non-htmx) forms -------------------------------------------
  document.addEventListener("submit", (e) => {
    const message = e.target.dataset && e.target.dataset.confirm;
    if (message && !window.confirm(message)) e.preventDefault();
  });

  // --- Repeatable rows (education, saved answers) ----------------------------------------
  document.addEventListener("click", (e) => {
    const add = e.target.closest && e.target.closest("[data-add-row]");
    if (add) {
      e.preventDefault();
      const template = document.getElementById(add.dataset.addRow);
      const target = document.getElementById(add.dataset.target);
      if (template && target) {
        target.insertAdjacentHTML("beforeend", template.innerHTML.replaceAll("__i__", String(Date.now())));
      }
      return;
    }
    const remove = e.target.closest && e.target.closest("[data-remove-row]");
    if (remove) {
      e.preventDefault();
      const row = remove.closest(".repeat-row");
      if (row) row.remove();
    }
  });
})();
