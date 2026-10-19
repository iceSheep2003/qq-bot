/**
 * Interactive primitives for the owner console.
 *
 * These exist because the ad-hoc markup they replace kept getting interaction
 * details wrong in the same three ways: no keyboard path, no focus handling,
 * and no state that survives a re-render. A custom element puts that behaviour
 * in one place instead of in every string template.
 *
 * Light DOM on purpose: the console styles everything from app.css, and shadow
 * roots would cut these components off from the design layer.
 *
 * No build step, no dependency — the console is served as static files by
 * http.server, so anything that needs bundling is out of scope.
 */

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

/* ------------------------------------------------------------------- modal */
/* The platform's own <dialog>, not a hand-built panel: showModal() gives the
   focus trap, the Esc handler, the ::backdrop and the top layer for free. The
   only thing added here is a title bar and a content setter. */
class KModal extends HTMLElement {
  connectedCallback() {
    if (this._built) return;
    this._built = true;
    const title = this.getAttribute("title") || "";
    const content = [...this.children];
    this.textContent = "";
    const dialog = document.createElement("dialog");
    dialog.className = "k-modal";
    dialog.setAttribute("aria-label", title || "详情");
    dialog.innerHTML = `<header class="k-modal-head"><h2>${title}</h2><button class="k-modal-close" type="button">关闭</button></header><div class="k-modal-body"></div>`;
    const body = dialog.querySelector(".k-modal-body");
    content.forEach((node) => body.appendChild(node));
    this.append(dialog);
    this._dialog = dialog;
    this._body = body;
    dialog.querySelector(".k-modal-close").addEventListener("click", () => this.close());
    dialog.addEventListener("click", (event) => {
      // A click on the dialog element itself is a click on the backdrop.
      if (event.target === dialog) this.close();
    });
    dialog.addEventListener("close", () => this.dispatchEvent(new CustomEvent("k-close", { bubbles: true })));
  }

  get isOpen() {
    return Boolean(this._dialog?.open);
  }

  open(title) {
    if (title) this._dialog.querySelector("h2").textContent = title;
    if (!this._dialog.open) this._dialog.showModal();
  }

  close() {
    if (this._dialog?.open) this._dialog.close();
  }

  setContent(html) {
    this._body.innerHTML = html;
    this._body.scrollTop = 0;
  }
}

/* ------------------------------------------------------------------- pager */
class KPager extends HTMLElement {
  static get observedAttributes() {
    return ["total", "offset", "limit", "pages"];
  }

  connectedCallback() {
    this.classList.add("k-pager");
    this._render();
  }

  attributeChangedCallback() {
    if (this._built) this._render();
  }

  _num(name, fallback) {
    const value = Number(this.getAttribute(name));
    return Number.isFinite(value) ? value : fallback;
  }

  _render() {
    this._built = true;
    const total = this._num("total", 0);
    const offset = this._num("offset", 0);
    const limit = Math.max(1, this._num("limit", 10));
    const pages = Math.max(1, this._num("pages", 1));
    const page = Math.min(pages, Math.floor(offset / limit) + 1);
    const from = total ? offset + 1 : 0;
    const to = Math.min(total, offset + limit);
    const unit = this.getAttribute("unit") || "条";
    this.innerHTML = `<span class="k-pager-range">${from}–${to} / 共 ${total} ${unit}</span>
      <span class="k-pager-gap"></span>
      <button class="k-pager-btn" type="button" data-step="-1" ${page <= 1 ? "disabled" : ""} aria-label="上一页">上一页</button>
      <span class="k-pager-count" aria-live="polite">${page} / ${pages}</span>
      <button class="k-pager-btn" type="button" data-step="1" ${page >= pages ? "disabled" : ""} aria-label="下一页">下一页</button>`;
    this.querySelectorAll("[data-step]").forEach((button) => {
      button.addEventListener("click", () => {
        const next = page + Number(button.dataset.step);
        if (next < 1 || next > pages) return;
        this.dispatchEvent(new CustomEvent("k-page", {
          bubbles: true,
          detail: { page: next, offset: (next - 1) * limit },
        }));
      });
    });
  }
}

/* ------------------------------------------------------------------ switch */
class KSwitch extends HTMLElement {
  static get observedAttributes() {
    return ["checked", "disabled", "label"];
  }

  connectedCallback() {
    if (this._built) return;
    this._built = true;
    this.classList.add("k-switch");
    this.innerHTML = `<button class="k-switch-track" type="button" role="switch" aria-checked="false"><span class="k-switch-thumb"></span></button><span class="k-switch-label"></span>`;
    this._button = this.querySelector("button");
    this._label = this.querySelector(".k-switch-label");
    this._button.addEventListener("click", () => {
      if (this.hasAttribute("disabled")) return;
      const next = !this.hasAttribute("checked");
      this.toggleAttribute("checked", next);
      this.dispatchEvent(new CustomEvent("k-toggle", { bubbles: true, detail: { checked: next } }));
    });
    this._sync();
  }

  attributeChangedCallback() {
    if (this._built) this._sync();
  }

  _sync() {
    const on = this.hasAttribute("checked");
    this._button.setAttribute("aria-checked", on ? "true" : "false");
    this._button.disabled = this.hasAttribute("disabled");
    this._label.textContent = this.getAttribute("label") || "";
    this._label.classList.toggle("hidden", !this.getAttribute("label"));
  }
}

/* ------------------------------------------------------------------- empty */
class KEmpty extends HTMLElement {
  connectedCallback() {
    if (this._built) return;
    this._built = true;
    const title = this.getAttribute("title") || "";
    const detail = this.getAttribute("detail") || "";
    this.classList.add("k-empty");
    this.innerHTML = `<b>${title}</b>${detail ? `<p>${detail}</p>` : ""}`;
  }
}

if (!customElements.get("k-modal")) customElements.define("k-modal", KModal);
if (!customElements.get("k-pager")) customElements.define("k-pager", KPager);
if (!customElements.get("k-switch")) customElements.define("k-switch", KSwitch);
if (!customElements.get("k-empty")) customElements.define("k-empty", KEmpty);
