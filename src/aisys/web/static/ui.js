/* Shared UI helpers. Data is only ever inserted into the page as text, never as HTML (XSS-safe by construction). */
window.UI = (() => {
  const $ = id => document.getElementById(id);

  /** h("div", {class: "x", onclick: fn}, child, "text", [more]) -> element */
  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") el.className = v;
      else if (k.startsWith("on") && typeof v === "function") el[k] = v;
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    for (const k of kids.flat(Infinity)) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k)));
    return el;
  }

  /** Replace an element's children, ignoring null/undefined/false. (Element.replaceChildren would print the word "null".) */
  function fill(parent, ...kids) {
    parent.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false).map(k => k.nodeType ? k : document.createTextNode(String(k))));
    return parent;
  }

  class ApiError extends Error {
    constructor(message, status, problems) { super(message); this.status = status; this.problems = problems || []; }
  }

  /** fetch JSON; throws ApiError with a human message and any per-field problems. */
  async function api(url, opts = {}) {
    const init = {credentials: "same-origin", ...opts};
    if (opts.json !== undefined) {
      init.method = opts.method || "POST";
      init.headers = {"Content-Type": "application/json", ...(opts.headers || {})};
      init.body = JSON.stringify(opts.json);
    }
    const r = await fetch(url, init);
    const isJson = r.headers.get("content-type")?.includes("json");
    const body = isJson ? await r.json() : null;
    if (!r.ok) {
      const d = body?.detail;
      // FastAPI schema errors arrive as a list [{loc: [..., field], msg}]; turn them into per-field problems
      const schema = Array.isArray(d) ? d.map(e => ({field: String(e.loc?.[e.loc.length - 1] ?? ""), code: "invalid", message: e.msg})) : [];
      const msg = typeof d === "string" ? d : d?.message || (schema.length && "Please check the highlighted fields") || "Request failed (" + r.status + ")";
      throw new ApiError(msg, r.status, d?.problems || schema);
    }
    return body;
  }

  function say(id, kind, text) { const n = $(id); n.className = "notice show " + kind; n.textContent = text; }
  function clear(id) { const n = $(id); n.className = "notice"; n.textContent = ""; }
  const fmt = n => (n ?? 0).toLocaleString();
  const when = iso => iso ? new Date(iso).toLocaleString([], {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit"}) : "";
  const short = uid => uid && uid.length > 14 ? uid.slice(0, 6) + "…" + uid.slice(-6) : uid;

  const STATE = {active: ["good", "In use"], removed: ["", "Removed · reusable"], retired: ["warn", "Retired"], unknown: ["", "Unknown"]};
  /** badge for a tag's state: active (on an item), removed (off an item, reusable) or retired (out of service) */
  function stateBadge(state) { const [cls, label] = STATE[state] || ["", state]; return h("span", {class: "badge " + cls}, label); }

  const END = {
    remove: {label: "Remove tag", help: "Take the tag off this item. It can be used on another item later."},
    retire: {label: "Retire tag", help: "The tag is damaged or lost. It goes out of service and can never be linked again."},
  };

  /**
   * Inline panel for taking a tag off an item. The person chooses REMOVE (reusable) or RETIRE (permanent).
   * `modes` limits the choices (a tag that is already removed can only be retired).
   * Calls onDone(result) after success, onCancel() if dismissed. Shared by the catalogue, tagging and registry pages.
   */
  function endTagPanel({uid, itemText, modes = ["remove", "retire"], onDone, onCancel}) {
    const note = h("div", {class: "notice", role: "alert"});
    const reason = h("input", {maxlength: "200", placeholder: "Reason (optional), e.g. wrong item or tag damaged", "aria-label": "Reason"});
    let choice = modes[0];
    const go = h("button", {class: "btn danger small", type: "button"}, END[choice].label);
    const labels = [];
    const options = modes.map((m, i) => labels[i] = h("label", {class: "radio" + (i === 0 ? " on" : "")},
      h("input", {type: "radio", name: "end-" + uid, value: m, checked: i === 0 ? "" : false, onchange: () => {
        choice = m; go.textContent = END[m].label;
        labels.forEach((l, j) => l.classList.toggle("on", j === modes.indexOf(m)));
      }}),
      h("span", {}, h("b", {}, END[m].label.replace(" tag", "")), h("span", {class: "sub"}, END[m].help))));
    go.onclick = async () => {
      go.disabled = true; note.className = "notice";
      try {
        const r = await api("/api/rfid/tags/" + encodeURIComponent(uid) + "/" + choice, {json: {reason: reason.value.trim()}});
        await onDone(r);
      } catch (e) { note.className = "notice show bad"; note.textContent = e.message; go.disabled = false; }
    };
    return h("div", {class: "confirm", role: "group"},
      h("p", {style: "margin:0 0 8px"}, modes.length > 1 ? "What should happen to tag " : "Retire tag ", h("b", {class: "mono"}, short(uid)),
        modes.length > 1 ? " on " : " (last on ", h("b", {}, itemText), modes.length > 1 ? "?" : ")?"),
      modes.length > 1 ? h("div", {class: "radios"}, options) : h("p", {class: "sub", style: "margin:0 0 8px"}, END.retire.help),
      h("p", {class: "sub", style: "margin:8px 0 10px"}, "If the tag is on the reader it is erased and its security bit switched off. Its history is kept."),
      reason, h("div", {class: "actions"}, go, h("button", {class: "btn ghost small", type: "button", onclick: onCancel}, "Cancel")), note);
  }

  /** list of problems (from validation) as a <ul> */
  function problemList(problems, kind = "bad") {
    return h("ul", {class: "problems " + kind}, problems.map(p => h("li", {}, p.message)));
  }

  return {$, h, fill, api, ApiError, say, clear, fmt, when, short, problemList, endTagPanel, stateBadge};
})();
