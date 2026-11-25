// Skontio "Modern": Befehlspalette (Strg/⌘+K oder "/"). Läuft nur, wenn die Oberfläche „Modern" gewählt ist.
// Keine Inline-Skripte (CSP); Texte werden nur per textContent gesetzt.
"use strict";

(() => {
  const dialog = document.getElementById("palette");
  if (!(dialog instanceof HTMLDialogElement)) {
    return;
  }
  const input = dialog.querySelector("input");
  const list = dialog.querySelector(".palette-list");
  const form = dialog.querySelector("form");

  const actions = [
    ["Neue Rechnung", "/dokumente/neu/?typ=invoice", "Anlegen"],
    ["Neues Angebot", "/dokumente/neu/?typ=quote", "Anlegen"],
    ["Ausgabe erfassen", "/ausgaben/neu/", "Anlegen"],
    ["Neuer Kunde", "/kunden/neu/", "Anlegen"],
  ];
  const pages = () =>
    [...document.querySelectorAll(".sidebar .navlink")].map((a) => [a.textContent.trim(), a.getAttribute("href"), "Gehe zu"]);

  let items = [];
  let active = 0;

  function render() {
    const query = input.value.trim().toLowerCase();
    const all = [...actions, ...pages()];
    items = all.filter(([label]) => !query || label.toLowerCase().includes(query));
    if (query) {
      items.push([`„${input.value.trim()}“ in allen Daten suchen`, `/suche/?q=${encodeURIComponent(input.value.trim())}`, "Suche"]);
    }
    active = Math.min(active, items.length - 1);
    list.replaceChildren(
      ...items.map(([label, href, kind], index) => {
        const li = document.createElement("li");
        li.setAttribute("role", "option");
        li.id = `palette-item-${index}`;
        li.dataset.href = href;
        li.setAttribute("aria-selected", String(index === active));
        const name = document.createElement("span");
        name.textContent = label;
        const tag = document.createElement("small");
        tag.textContent = kind;
        li.append(name, tag);
        return li;
      }),
    );
    input.setAttribute("aria-activedescendant", items.length ? `palette-item-${active}` : "");
  }

  function open() {
    if (!dialog.open) {
      dialog.showModal();
    }
    input.value = "";
    active = 0;
    render();
    input.focus();
  }

  function go(index) {
    const target = items[index];
    if (target) {
      window.location.assign(target[1]);
    }
  }

  document.querySelectorAll("[data-palette-open]").forEach((button) => button.addEventListener("click", open));
  document.addEventListener("keydown", (event) => {
    const typing = event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable]");
    if ((event.key === "k" && (event.ctrlKey || event.metaKey)) || (event.key === "/" && !typing)) {
      event.preventDefault();
      open();
    }
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      dialog.close();
    }
  });
  input.addEventListener("input", () => {
    active = 0;
    render();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      render();
      list.children[active]?.scrollIntoView({ block: "nearest" });
    }
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    go(active);
  });
  list.addEventListener("click", (event) => {
    const li = event.target instanceof Element ? event.target.closest("li") : null;
    if (li) {
      go([...list.children].indexOf(li));
    }
  });
})();
