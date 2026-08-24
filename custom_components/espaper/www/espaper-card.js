/**
 * A Lovelace card that edits the panel's Markdown in a real textarea.
 *
 * Home Assistant's text entity is single-line -- `TextMode` offers only `text`
 * and `password`, and the frontend renders an <input> -- and its state is
 * capped at 255 characters. Neither limit is fixable from a card built on the
 * entity-row API, so this card goes around the entity: it reads the
 * `full_markdown` attribute and writes through the `espaper.set_markdown`
 * service, which has no cap.
 *
 *     type: custom:espaper-card
 *     entity: text.espaper_markdown
 *
 * The integration serves this file and loads it into the frontend itself, so
 * there is no Lovelace resource to add. Vanilla custom element on purpose: a
 * card this small does not justify a build step.
 */

const DOMAIN = "espaper";
const DEFAULT_ROWS = 10;

// `HTMLElement` only exists in a browser. Standing in for it outside one keeps
// this module importable by test_card.mjs.
const Base = typeof HTMLElement !== "undefined" ? HTMLElement : class {};

/**
 * Decide what the textarea should hold after an update from Home Assistant.
 *
 * The `hass` setter fires on every state change, including the ones this card
 * causes, so a naive assignment would eat whatever the user is halfway through
 * typing. Incoming text wins only when it is actually new *and* nothing local
 * would be lost.
 */
export function nextValue(remote, lastRemote, current, dirty) {
  if (remote === lastRemote) return current; // nothing new from HA
  if (dirty) return current; // don't clobber what is being typed
  return remote;
}

/** "3 minutes ago", or "never" for a panel that has not been written yet. */
function ago(iso) {
  if (!iso) return "never";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (!Number.isFinite(seconds)) return "never";
  if (seconds < 60) return "just now";
  const [amount, unit] =
    seconds < 3600
      ? [seconds / 60, "minute"]
      : seconds < 86400
        ? [seconds / 3600, "hour"]
        : [seconds / 86400, "day"];
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
    -Math.round(amount),
    unit,
  );
}

export class ESPaperCard extends Base {
  constructor() {
    super();
    this._remote = null;
    this._dirty = false;
    this._external = false; // changed elsewhere while we held unsaved edits
    this._sending = false;
    this._error = null;
    this.attachShadow({ mode: "open" });
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("espaper-card: an `entity` is required");
    }
    this._config = { rows: DEFAULT_ROWS, ...config };
    this._build();
    // `hass` may already have arrived (see the setter), and nothing has been
    // painted with it yet.
    if (this._hass) this._update();
  }

  set hass(hass) {
    this._hass = hass;
    // Home Assistant hands a card `hass` before `setConfig` on several paths --
    // the card picker's preview is one -- and until setConfig there is no
    // config to read and no DOM to paint. Throwing here leaves that preview
    // spinning forever, so wait to be configured instead.
    if (this._config) this._update();
  }

  getCardSize() {
    // Also reachable before setConfig, and for the same reason.
    return 2 + Math.ceil((this._config?.rows ?? DEFAULT_ROWS) / 3);
  }

  static getStubConfig(hass) {
    // Any entity carrying full_markdown is one of ours.
    const found = Object.keys(hass?.states ?? {}).find(
      (id) => hass.states[id].attributes.full_markdown !== undefined,
    );
    return { entity: found ?? "text.espaper_markdown" };
  }

  _build() {
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { display: flex; flex-direction: column; }
        textarea {
          width: 100%;
          box-sizing: border-box;
          margin: 0;
          padding: 12px 16px;
          border: none;
          resize: vertical;
          background: transparent;
          color: var(--primary-text-color);
          font-family: var(--code-font-family, ui-monospace, monospace);
          font-size: 14px;
          line-height: 1.5;
        }
        textarea:focus { outline: none; }
        .footer {
          display: flex;
          align-items: center;
          gap: 12px;
          padding: 8px 16px;
          border-top: 1px solid var(--divider-color);
        }
        .status {
          flex: 1;
          min-width: 0;
          font-size: 12px;
          color: var(--secondary-text-color);
          overflow-wrap: anywhere;
        }
        .status.error { color: var(--error-color); }
        button {
          flex: none;
          padding: 8px 16px;
          border: none;
          border-radius: 4px;
          background: var(--primary-color);
          color: var(--text-primary-color, #fff);
          font: inherit;
          font-size: 14px;
          font-weight: 500;
          cursor: pointer;
        }
        button[disabled] { opacity: 0.5; cursor: default; }
      </style>
      <ha-card>
        <textarea spellcheck="false"></textarea>
        <div class="footer">
          <div class="status"></div>
          <button type="button">Send</button>
        </div>
      </ha-card>
    `;
    this._card = this.shadowRoot.querySelector("ha-card");
    this._textarea = this.shadowRoot.querySelector("textarea");
    this._status = this.shadowRoot.querySelector(".status");
    this._button = this.shadowRoot.querySelector("button");

    if (this._config.title) this._card.setAttribute("header", this._config.title);
    this._textarea.rows = this._config.rows;

    this._textarea.addEventListener("input", () => {
      this._dirty = this._textarea.value !== this._remote;
      this._external = false;
      this._error = null;
      this._paint();
    });
    this._textarea.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        this._send();
      }
    });
    this._button.addEventListener("click", () => this._send());
  }

  _update() {
    const state = this._hass?.states[this._config.entity];
    if (!state) {
      this._status.textContent = `${this._config.entity} not found`;
      this._status.classList.add("error");
      this._button.disabled = true;
      return;
    }
    if (state.state !== "unavailable") {
      // Never read `state`: text.py truncates it at MAX_STATE_LENGTH.
      const remote = state.attributes.full_markdown ?? "";
      this._textarea.value = nextValue(
        remote,
        this._remote,
        this._textarea.value,
        this._dirty,
      );
      if (remote !== this._remote) this._external = this._dirty;
      this._remote = remote;
    }
    this._state = state;
    this._paint();
  }

  _paint() {
    const attrs = this._state?.attributes ?? {};
    const notes = [];
    if (this._sending) notes.push("sending…");
    else if (this._dirty) notes.push("unsaved changes");
    if (this._external) notes.push("changed elsewhere");

    const problem = this._error ?? attrs.last_error;
    if (problem) {
      notes.push(problem);
    } else if (this._state?.state === "unavailable") {
      notes.push("unavailable");
    } else {
      notes.push(`${attrs.upload_status ?? "idle"} · panel updated ${ago(attrs.last_upload)}`);
    }

    this._status.textContent = notes.join(" · ");
    this._status.classList.toggle("error", Boolean(problem));
    this._button.disabled = this._sending || !this._dirty;
  }

  async _send() {
    if (this._sending || !this._hass) return;
    const sending = this._textarea.value;
    this._sending = true;
    this._error = null;
    this._paint();
    try {
      await this._hass.callService(
        DOMAIN,
        "set_markdown",
        { markdown: sending },
        { entity_id: this._config.entity },
      );
      // Adopt it locally too, so the state update this causes is not treated
      // as a change from elsewhere.
      this._remote = sending;
      this._dirty = this._textarea.value !== sending;
      this._external = false;
    } catch (err) {
      this._error = err?.message || String(err);
    } finally {
      this._sending = false;
      this._paint();
    }
  }
}

if (typeof customElements !== "undefined" && !customElements.get("espaper-card")) {
  customElements.define("espaper-card", ESPaperCard);
  (window.customCards = window.customCards || []).push({
    type: "espaper-card",
    name: "ESPaper",
    description: "Edit the Markdown shown on a BLE e-paper panel.",
  });
}
