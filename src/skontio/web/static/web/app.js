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

// "Teilen": hand the PDF to other apps (Web Share API, mainly the installed Android app).
// The button stays hidden where the browser cannot share files.
function canShareFiles() {
  try {
    const probe = new File([new Blob()], "probe.pdf", { type: "application/pdf" });
    return Boolean(navigator.canShare && navigator.canShare({ files: [probe] }));
  } catch {
    return false;
  }
}

document.querySelectorAll("button.share-file").forEach((button) => {
  if (!canShareFiles()) {
    return;
  }
  button.hidden = false;
  button.addEventListener("click", async () => {
    const { url, filename, title } = button.dataset;
    button.disabled = true;
    try {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const file = new File([await response.blob()], filename, { type: "application/pdf" });
      await navigator.share({ files: [file], title });
    } catch (error) {
      if (error.name !== "AbortError") {
        window.location.href = url; // fall back to a normal download
      }
    } finally {
      button.disabled = false;
    }
  });
});

// Links to a collapsed section (e.g. "Zahlung erfassen" → #zahlungen) open it first.
function openTargetSection(hash) {
  const target = hash && document.getElementById(hash.slice(1));
  if (target instanceof HTMLDetailsElement) {
    target.open = true;
  }
}
window.addEventListener("hashchange", () => openTargetSection(window.location.hash));
openTargetSection(window.location.hash);

// Copy buttons (e.g. Kennzahlen for Mein ELSTER).
document.addEventListener("click", async (event) => {
  const button = event.target instanceof Element ? event.target.closest("button.copy") : null;
  if (!button) {
    return;
  }
  try {
    await navigator.clipboard.writeText(button.dataset.copy);
    const label = button.textContent;
    button.textContent = "kopiert";
    setTimeout(() => { button.textContent = label; }, 1500);
  } catch {
    window.prompt("Wert kopieren:", button.dataset.copy);
  }
});

// Position hinzufügen: Wählt man eine Leistung aus dem Katalog, zeigt das Feld „Einheit“ deren Einheit (und ist
// gesperrt, denn die Einheit der Leistung gilt). Ohne Leistung (freie Position) ist das Feld wieder frei.
function syncUnit(itemSelect) {
  const unit = itemSelect.form?.querySelector('select[name="unit_code"]');
  if (!unit) {
    return;
  }
  const code = itemSelect.selectedOptions[0]?.dataset.unit;
  if (code) {
    unit.value = code;
    unit.disabled = true;
  } else {
    unit.disabled = false;
  }
}

document.addEventListener("change", (event) => {
  if (event.target instanceof HTMLSelectElement && event.target.name === "item") {
    syncUnit(event.target);
  }
});
const syncAllUnits = () => document.querySelectorAll('select[name="item"]').forEach(syncUnit);
document.addEventListener("htmx:afterSettle", syncAllUnits);
syncAllUnits();
