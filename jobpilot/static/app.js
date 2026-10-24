(function () {
  const root = document.documentElement;
  try { const t = localStorage.getItem("jp-theme"); if (t) root.dataset.theme = t; } catch (e) {}
  document.addEventListener("click", function (ev) {
    const btn = ev.target.closest(".theme-toggle");
    if (!btn) return;
    const dark = root.dataset.theme ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("jp-theme", root.dataset.theme); } catch (e) {}
  });
  // Show a "working" overlay for slow actions (LLM calls, searches).
  document.addEventListener("submit", function (ev) {
    const msg = ev.target.dataset.busy;
    if (!msg) return;
    const overlay = document.getElementById("busy");
    overlay.querySelector(".msg").textContent = msg;
    overlay.classList.add("on");
  });
  // Live browser status on the application page.
  const box = document.getElementById("browser-status");
  if (box) {
    const appId = Number(box.dataset.app);
    const tick = async () => {
      try {
        const s = await (await fetch("/browser/status")).json();
        if (s.application_id === appId || !s.application_id) {
          box.querySelector(".state").textContent = s.state.replace(/_/g, " ");
          box.querySelector(".log").textContent = s.log.join("\n") || "No browser activity yet.";
        }
      } catch (e) {}
    };
    tick(); setInterval(tick, 2000);
  }
})();
