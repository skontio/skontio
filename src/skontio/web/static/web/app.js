// Skontio – small client helpers (no inline scripts: the CSP forbids them).
"use strict";

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
      // offline support is optional; the app works without a service worker
    });
  });
}

// Clear cached app data on logout so a shared device keeps nothing behind.
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form instanceof HTMLFormElement && form.classList.contains("logout")) {
    navigator.serviceWorker?.controller?.postMessage("logout");
  }
});
