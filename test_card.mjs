#!/usr/bin/env node
/**
 * Self-check for the card's sync rule. No Home Assistant, no browser.
 *
 *     node test_card.mjs
 *
 * Home Assistant pushes a fresh `hass` object at the card on every state
 * change, including the ones the card itself causes, so this rule is all that
 * stands between the user and losing a half-typed note.
 */

import assert from "node:assert/strict";

// Enough of a browser for the class body to load and one element to exist.
// `customElements` stays undefined, so the module skips registering itself.
globalThis.HTMLElement = class {
  attachShadow() {
    return {};
  }
};

const { ESPaperCard, nextValue, sendPlan } = await import(
  "./custom_components/espaper/www/espaper-card.js"
);

const cases = {
  "an unconfigured card survives being handed hass": () => {
    // The card picker builds a preview, sets `hass` on it, and only then calls
    // setConfig. Throwing in the setter leaves that preview loading forever.
    const card = new ESPaperCard();
    card.hass = { states: {} };
    assert.equal(typeof card.getCardSize(), "number");
  },

  "nothing new from HA leaves the box alone": () => {
    assert.equal(nextValue("a", "a", "a typed draft", true), "a typed draft");
    assert.equal(nextValue("a", "a", "a typed draft", false), "a typed draft");
  },

  "a change from elsewhere is adopted while the box is clean": () => {
    assert.equal(nextValue("new", "old", "old", false), "new");
  },

  "a change from elsewhere never eats unsaved typing": () => {
    assert.equal(nextValue("new", "old", "half a shopping list", true), "half a shopping list");
  },

  "the first update fills an empty box": () => {
    // _remote starts null, which no state can equal.
    assert.equal(nextValue("# Shopping", null, "", false), "# Shopping");
  },

  "clearing the text remotely is a change like any other": () => {
    assert.equal(nextValue("", "# Shopping", "# Shopping", false), "");
    assert.equal(nextValue("", "# Shopping", "mine", true), "mine");
  },

  "an offline panel says so, and an online one does not": () => {
    // "asleep, back in a minute" and "gone" look identical otherwise: the
    // status line sits at `pending` either way.
    const card = new ESPaperCard();
    card._status = { classList: { toggle() {} } };
    card._button = {};

    card._state = { state: "on", attributes: { online: false, upload_status: "pending" } };
    card._paint();
    assert.match(card._status.textContent, /panel offline/);

    card._state = { state: "on", attributes: { online: true, upload_status: "pending" } };
    card._paint();
    assert.doesNotMatch(card._status.textContent, /offline/);
  },

  "Send with nothing changed sends nothing": () => {
    assert.deepEqual(sendPlan("# Hello", "# Hello", 90, 90), []);
  },

  "Send with only the text changed leaves the angle alone": () => {
    assert.deepEqual(sendPlan("# Bye", "# Hello", 90, 90), [
      ["set_markdown", { markdown: "# Bye" }],
    ]);
  },

  "Send with only the angle changed leaves the text alone": () => {
    assert.deepEqual(sendPlan("# Hello", "# Hello", 180, 90), [
      ["set_rotation", { rotation: 180 }],
    ]);
  },

  "both changed go out together, markdown first": () => {
    // The whole bug: rotation used to go on its own, repainting the panel at
    // the new angle with text that had never been sent.
    assert.deepEqual(sendPlan("# Bye", "# Hello", 180, 90), [
      ["set_markdown", { markdown: "# Bye" }],
      ["set_rotation", { rotation: 180 }],
    ]);
  },
};

let failed = 0;
for (const [name, run] of Object.entries(cases)) {
  try {
    run();
    console.log(`ok  ${name}`);
  } catch (err) {
    failed += 1;
    console.error(`FAIL ${name}\n     ${err.message}`);
  }
}
console.log(failed ? `${failed} failed` : "all good");
process.exit(failed ? 1 : 0);
