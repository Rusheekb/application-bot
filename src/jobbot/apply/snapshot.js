// Reads the application form on the current page (one frame at a time) for the apply agent.
// Every interactive element gets a stable `data-jobbot-ref` attribute so later actions can target it.
// Called as (options) to read a frame, or through a locator as (element, options) to check one
// button again right before it's clicked: the page may have changed since it was last read.
(target, options) => {
  const element = target instanceof Element ? target : null;
  const args = (element ? options : target) || {};
  const startRef = args.startRef || 1;
  const maxOptions = args.maxOptions || 60;
  const maxText = args.maxText || 1500;
  let nextRef = startRef;

  const clean = (s) => (s || "").replace(/[\s ]+/g, " ").trim();
  const textOf = (el) => (el ? clean(el.innerText || el.textContent || "") : "");
  const CONTROL_SEL =
    "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image])," +
    "select, textarea, [role=combobox], [role=checkbox], [role=radio], [contenteditable=true], [contenteditable='']";
  const BUTTON_SEL = "button, input[type=submit], input[type=button], [role=button], a[href]";

  // Collect elements, descending into open shadow roots.
  const all = [];
  const walk = (root) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node = walker.nextNode();
    while (node) {
      all.push(node);
      if (node.shadowRoot) walk(node.shadowRoot);
      node = walker.nextNode();
    }
  };
  walk(document.body || document.documentElement);

  // Elements copied with cloneNode (an "Add another" row, say) carry the original's ref. Keep it
  // on the first element only, so every ref names exactly one element.
  const refOwners = new Map();
  for (const el of all) {
    const ref = el.getAttribute && el.getAttribute("data-jobbot-ref");
    if (!ref) continue;
    if (refOwners.has(ref)) el.removeAttribute("data-jobbot-ref");
    else refOwners.set(ref, el);
  }
  const refOf = (el) => {
    let ref = el.getAttribute("data-jobbot-ref");
    if (!ref || (refOwners.has(ref) && refOwners.get(ref) !== el)) {
      ref = String(nextRef++);
      el.setAttribute("data-jobbot-ref", ref);
    }
    refOwners.set(ref, el);
    return ref;
  };

  const shown = (el) => {
    for (let e = el; e && e.nodeType === 1; ) {
      const st = getComputedStyle(e);
      if (st.display === "none" || st.visibility === "hidden") return false;
      const parent = e.parentElement || (e.getRootNode && e.getRootNode().host) || null;
      e = parent;
    }
    return true;
  };
  const visible = (el) => {
    if (!el.isConnected || !shown(el)) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };

  const byId = (el, id) => {
    const root = el.getRootNode();
    return (root && root.getElementById ? root.getElementById(id) : null) || document.getElementById(id);
  };

  // -- Could clicking a button submit the application? --------------------------------------
  const BUTTONISH = "button, input[type=submit], input[type=button], input[type=image], [role=button]";
  const buttonLabel = (b) =>
    clean(textOf(b) || b.value || b.getAttribute("aria-label") || b.getAttribute("title") || "").slice(0, 100);
  const STEP_WORDS = /\b(next|continue)\b/;
  const COMMIT_WORDS = /\b(apply|finish|done|confirm|complete|send|submit|save|proceed|register|post)\b/;
  const SAFE_WORDS = new RegExp(
    "\\b(back|previous|prev|review|add|another|upload|attach|browse|choose|select|remove|delete|edit|change|" +
      "replace|cancel|close|dismiss|clear|show|hide|more|less|expand|collapse|search|skip|yes|no|accept|agree|" +
      "decline|reject|allow|got it|ok|okay|manual|manually|start|begin|view|preview|open|toggle|sign in|log in)\\b"
  );
  // The application's submit button: says submit/send, or is the submit button of a form that
  // looks like an application (and isn't a step, save or upload button).
  const submitLike = (b, label) => {
    const t = label.toLowerCase();
    if (/\b(submit|send)\b/.test(t) || /(complete|finish|send) (my |your )?application/.test(t)) return true;
    const tag = b.tagName;
    const isSubmitType =
      (tag === "BUTTON" && (b.getAttribute("type") || "submit").toLowerCase() === "submit") ||
      (tag === "INPUT" && (b.type || "").toLowerCase() === "submit");
    if (isSubmitType && b.form) {
      const form = b.form;
      const n = form.querySelectorAll("input:not([type=hidden]), textarea, select").length;
      if (form.querySelector("input[type=file]") || n >= 3) {
        return !/\b(next|continue|review|save|back|previous|upload|add|attach|remove|cancel|search)\b/.test(t);
      }
    }
    return false;
  };
  // Whether this frame holds an application form: three text fields, a file upload next to a
  // text field, or two fields already filled in (including steps that are now hidden).
  const holdsApplication = () => {
    const TEXTY = "textarea, input:not([type]), input[type=text], input[type=email], input[type=tel], " +
      "input[type=url], input[type=number], input[type=date], [contenteditable=true], [contenteditable='']";
    let fields = 0, filled = 0, files = 0;
    for (const el of all) {
      if (!el.matches) continue;
      if (el.matches("input[type=file]")) {
        files++;
        if (el.files && el.files.length) filled += 2;
      } else if (el.matches(TEXTY)) {
        if ((el.isContentEditable ? textOf(el) : el.value || "").trim()) filled++;
        if (visible(el)) fields++;
      }
    }
    return fields >= 3 || (files > 0 && fields >= 1) || filled >= 2;
  };
  // Toggles, tabs and dropdown openers answer or reveal things; they don't submit.
  const toggleLike = (el) =>
    ["aria-pressed", "aria-checked", "aria-expanded", "aria-haspopup", "aria-selected"].some((a) => el.hasAttribute(a)) ||
    /^(radio|checkbox|switch|tab|option|menuitem|menuitemradio|menuitemcheckbox|combobox)$/.test((el.getAttribute("role") || "").toLowerCase());
  // Answers shown as a row of short buttons: Yes / No, Remote / Hybrid / On-site.
  const answerGroup = (el, label) => {
    if (!label || label.length > 40) return false;
    for (let parent = el.parentElement, depth = 0; parent && depth < 2; parent = parent.parentElement, depth++) {
      const others = Array.from(parent.querySelectorAll(BUTTONISH)).filter((b) => b !== el && visible(b));
      if (!others.length) continue;
      return others.length <= 12 && others.every((b) => {
        const t = buttonLabel(b).toLowerCase();
        return t && t.length <= 40 && !STEP_WORDS.test(t) && !COMMIT_WORDS.test(t) && !/\b(back|previous|prev|cancel)\b/.test(t);
      });
    }
    return false;
  };
  // final: the submit button itself. commit: on an application form, with a label like Apply,
  // Done or Confirm. risky: commit, or any button there that isn't recognizably safe (Next, Back,
  // Add, Upload, an answer...), because it might submit the form with a script.
  const assess = (el, label, application) => {
    const verdict = { final: false, commit: false, risky: false };
    if (el.matches(CONTROL_SEL)) return verdict;
    const href = (el.tagName === "A" && el.getAttribute("href")) || "";
    const navigates = href.trim() && !/^\s*(#|javascript:)/i.test(href);
    if (submitLike(el, label)) return { final: true, commit: true, risky: true };
    const t = label.toLowerCase();
    if (!application || navigates || !(el.matches(BUTTONISH) || el.tagName === "A") || toggleLike(el) || STEP_WORDS.test(t)) {
      return verdict;
    }
    verdict.commit = COMMIT_WORDS.test(t);
    verdict.risky = verdict.commit || !(SAFE_WORDS.test(t) || answerGroup(el, label));
    return verdict;
  };
  if (element) {
    const label = buttonLabel(element);
    return { label, ...assess(element, label, holdsApplication()) };
  }

  // Visible text inside `node`, skipping controls, buttons, links and option lists (their
  // captions are not part of a question), and any text exactly matching `exclude`.
  const SKIP_TEXT_IN = "button, [role=button], a, select, option, textarea, script, style, noscript, [role=listbox], [role=option]";
  const ownText = (node, exclude) => {
    const parts = [];
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let t = walker.nextNode();
    while (t) {
      const parent = t.parentElement;
      const skip = parent && parent.closest(SKIP_TEXT_IN);
      const value = clean(t.nodeValue);
      if (value && parent && !(skip && node.contains(skip)) && !(exclude && exclude.has(value)) &&
          (!parent.checkVisibility || parent.checkVisibility())) {
        parts.push(value);
      }
      t = walker.nextNode();
    }
    return clean(parts.join(" "));
  };
  const directLabel = (el) => {
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const t = clean(labelledBy.split(/\s+/).map((id) => { const n = byId(el, id); return n ? ownText(n) : ""; }).join(" "));
      if (t) return t;
    }
    if (el.labels && el.labels.length) {
      const t = clean(Array.from(el.labels).map((l) => ownText(l)).join(" "));
      if (t) return t;
    }
    const aria = clean(el.getAttribute("aria-label"));
    if (aria) return aria;
    return "";
  };
  const groupKey = (el) =>
    (el.type === "radio" || el.type === "checkbox") && el.name ? `${el.type}:${el.name}` : null;
  const stripMarks = (s) => clean((s || "").replace(/^[*✱\s]+|[*✱\s]+$/g, " "));

  // The question a control belongs to. Climb to the largest container that holds no other
  // question; prefer a label-like element inside it, else its text minus the option labels.
  const LABELISH = "label, legend, [class*=label i], [id*=label i], [class*=question i], [class*=title i]";
  const questionFor = (el, exclude) => {
    const key = groupKey(el);
    const levels = [];
    let node = el.parentElement;
    for (let depth = 0; node && depth < 7 && node !== document.body; depth++, node = node.parentElement) {
      let other = false;
      for (const c of node.querySelectorAll(CONTROL_SEL)) {
        if (c === el || (key && groupKey(c) === key)) continue;
        if (c.closest("[role=listbox]")) continue;
        if (!shown(c) && c.type !== "file") continue;
        other = true;
        break;
      }
      if (other) break;
      levels.push(node);
    }
    for (const level of levels) {
      const legend = level.tagName === "FIELDSET" ? level.querySelector("legend") : null;
      if (legend && ownText(legend)) return ownText(legend).slice(0, 300);
      for (const cand of level.querySelectorAll(LABELISH)) {
        if (cand.contains(el) || cand.querySelector(CONTROL_SEL)) continue;
        const text = ownText(cand, exclude);
        if (stripMarks(text).length > 1) return text.slice(0, 300);
      }
    }
    for (const level of levels) {
      const text = ownText(level, exclude);
      if (stripMarks(text).length > 1) return text.slice(0, 300);
    }
    return "";
  };

  // Text of a heading or label-like element right before the control (e.g. <h4>Additional
  // information</h4><textarea>), for controls whose container holds other questions too.
  const precedingText = (el) => {
    let node = el;
    for (let depth = 0; node && depth < 3; depth++, node = node.parentElement) {
      let prev = node.previousElementSibling;
      for (let i = 0; prev && i < 3; i++, prev = prev.previousElementSibling) {
        if (prev.matches(CONTROL_SEL) || prev.querySelector(CONTROL_SEL)) return "";
        const text = ownText(prev);
        if (text) return text.slice(0, 200);
      }
      if (node.parentElement && node.parentElement.firstElementChild !== node) break;
    }
    return "";
  };

  const isRequired = (el, label) =>
    !!el.required ||
    el.getAttribute("aria-required") === "true" ||
    /(^|\s)\*|\*\s*$|✱|\(required\)/i.test(label || "");

  const out = {
    url: location.href,
    title: document.title,
    headings: [],
    fields: [],
    buttons: [],
    alerts: [],
    captcha: [],
    login: false,
    text: "",
    nextRef: startRef,
  };

  // Headings give the agent context about where it is.
  for (const h of all) {
    if (/^H[1-3]$/.test(h.tagName) && visible(h)) {
      const t = textOf(h);
      if (t && out.headings.length < 8) out.headings.push(t.slice(0, 120));
    }
  }

  const groups = new Map();
  for (const el of all) {
    if (!el.matches || !el.matches(CONTROL_SEL)) continue;
    if (el.closest("[aria-hidden=true]") && el.type !== "file") continue;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();

    if (type === "password") {
      if (visible(el)) out.login = true;
      continue;
    }
    if (type === "file") {
      const names = new Set(Array.from(el.files || []).map((f) => f.name));
      const label = directLabel(el) || questionFor(el, names) || precedingText(el) || "File upload";
      out.fields.push({
        ref: refOf(el),
        kind: "file",
        label,
        required: isRequired(el, label),
        value: Array.from(el.files || []).map((f) => f.name).join(", "),
        accept: el.getAttribute("accept") || "",
      });
      continue;
    }
    if (type === "radio" || type === "checkbox" || role === "radio" || role === "checkbox") {
      const optionLabel = directLabel(el) || clean(el.value !== "on" ? el.value : "") || textOf(el);
      const labelEl = el.labels && el.labels[0];
      if (!visible(el) && !(labelEl && visible(labelEl))) continue;
      const checked = role ? el.getAttribute("aria-checked") === "true" : !!el.checked;
      const key = groupKey(el) || (role === "radio" && el.closest("[role=radiogroup]") ? "rg:" + refOf(el.closest("[role=radiogroup]")) : null);
      if (!key) {
        const label = optionLabel || questionFor(el, new Set());
        out.fields.push({ ref: refOf(el), kind: "checkbox", label, required: isRequired(el, label), value: checked });
        continue;
      }
      let group = groups.get(key);
      if (!group) {
        group = { first: el, kind: type === "checkbox" || role === "checkbox" ? "checkboxes" : "radio", options: [], required: false };
        groups.set(key, group);
        out.fields.push(group);
      }
      group.options.push({ label: optionLabel, ref: refOf(el), checked });
      group.required = group.required || isRequired(el, "");
      continue;
    }
    if (!visible(el)) continue;
    let kind;
    if (tag === "select") kind = el.multiple ? "multiselect" : "select";
    else if (tag === "textarea") kind = "textarea";
    else if (role === "combobox" || (el.getAttribute("aria-autocomplete") && el.getAttribute("aria-haspopup"))) kind = "combobox";
    else if (el.isContentEditable && tag !== "input") kind = "richtext";
    else if (["email", "tel", "url", "number", "date", "month"].includes(type)) kind = type;
    else kind = "text";

    const field = { ref: refOf(el), kind, label: "", required: false, value: "" };
    let exclude = new Set();
    if (tag === "select") {
      const options = Array.from(el.options).map((o) => ({ label: clean(o.label || o.text), value: o.value }));
      exclude = new Set(options.map((o) => o.label));
      field.options = options.filter(
        (o) => o.label && !/^(-+|(please )?(select|choose)( one| an option| an answer)?\s*(\.|…)*\s*)$/i.test(o.label)
      );
      field.option_count = field.options.length;
      field.options = field.options.slice(0, maxOptions);
      field.value = Array.from(el.selectedOptions || [])
        .filter((o) => o.value !== "")
        .map((o) => clean(o.label || o.text))
        .join(", ");
    } else if (kind === "combobox") {
      field.value = el.value || "";
      const container = el.closest("[class*=container], [class*=control], div");
      const shownValue = container && container.parentElement
        ? container.parentElement.querySelector("[class*=single-value], [class*=singleValue], [class*=multi-value__label], [class*=multiValue]")
        : null;
      if (shownValue) field.value = textOf(shownValue);
      exclude = new Set([field.value]);
    } else if (kind === "richtext") {
      field.value = textOf(el);
    } else {
      field.value = el.value || "";
      if (el.placeholder) field.placeholder = clean(el.placeholder).slice(0, 80);
      if (el.maxLength > 0 && el.maxLength < 100000) field.max_length = el.maxLength;
    }
    field.label =
      directLabel(el) || questionFor(el, exclude) || precedingText(el) || clean(el.placeholder) || clean(el.name);
    field.required = isRequired(el, field.label);
    if (el.getAttribute("aria-invalid") === "true") field.invalid = true;
    out.fields.push(field);
  }

  // Finish radio/checkbox groups: their question is the container text minus option labels.
  for (const [key, group] of groups) {
    const labels = new Set(group.options.map((o) => o.label));
    const el = group.first;
    let question = "";
    const radiogroup = el.closest("[role=radiogroup]");
    if (radiogroup) question = directLabel(radiogroup);
    if (!question) {
      const fieldset = el.closest("fieldset");
      const legend = fieldset && fieldset.querySelector("legend");
      if (legend) question = textOf(legend);
    }
    if (!question) question = questionFor(el, labels) || precedingText(el.closest("ul, ol, div") || el);
    group.ref = group.options[0].ref + "g";
    group.label = question;
    group.required = group.required || isRequired(el, question);
    group.value = group.options.filter((o) => o.checked).map((o) => o.label).join(", ");
    delete group.first;
    if (group.options.length === 1 && group.kind === "checkboxes") {
      // A lone checkbox that merely shares a name attribute.
      group.kind = "checkbox";
      group.label = question ? `${question} — ${group.options[0].label}`.slice(0, 300) : group.options[0].label;
      group.ref = group.options[0].ref;
      group.value = group.options[0].checked;
      delete group.options;
    }
  }

  // Buttons and button-like links.
  const application = holdsApplication();
  let links = 0;
  for (const b of all) {
    if (!b.matches || !b.matches(BUTTON_SEL)) continue;
    if (!visible(b)) continue;
    if (b.closest("[role=listbox]")) continue;
    const label = buttonLabel(b);
    if (!label) continue;
    const isLink = b.tagName === "A" && !b.matches("[role=button]");
    if (isLink) {
      const buttonish = /btn|button/i.test(b.className || "") ||
        /\b(apply|next|continue|start|begin|submit|interested|application)\b/i.test(label);
      if (!buttonish || links >= 15) continue;
      links++;
    }
    const entry = { ref: refOf(b), kind: isLink ? "link" : "button", label };
    const verdict = assess(b, label, application);
    if (verdict.final) entry.final_submit = true;
    else if (args.review ? verdict.risky : verdict.commit) entry.may_submit = true;
    if (b.getAttribute("aria-pressed") === "true" || b.getAttribute("aria-checked") === "true" ||
        /(^|[\s_-])(active|selected|is-selected|checked)([\s_-]|$)/i.test(b.className || "")) entry.pressed = true;
    if (b.disabled || b.getAttribute("aria-disabled") === "true") entry.disabled = true;
    const context = questionFor(b, new Set([label]));
    if (context && context !== label && context.length < 300) entry.context = context;
    out.buttons.push(entry);
    if (out.buttons.length >= 60) break;
  }

  // Validation messages and banners.
  for (const el of all) {
    if (!el.matches) continue;
    const isAlert = el.matches("[role=alert], [aria-live=assertive]") ||
      (/(^|[\s_-])(error|invalid|alert|warning)([\s_-]|$)/i.test(el.className || "") && el.children.length < 4);
    if (!isAlert || !visible(el)) continue;
    const t = textOf(el);
    if (t && t.length < 300 && !out.alerts.includes(t)) out.alerts.push(t);
    if (out.alerts.length >= 12) break;
  }

  // CAPTCHAs: report them so the agent can stop instead of trying to solve them.
  for (const f of all) {
    if (f.tagName !== "IFRAME") continue;
    const src = (f.getAttribute("src") || "").toLowerCase();
    const title = (f.getAttribute("title") || "").toLowerCase();
    let kind = null;
    if (src.includes("recaptcha")) kind = "reCAPTCHA";
    else if (src.includes("hcaptcha")) kind = "hCaptcha";
    else if (src.includes("challenges.cloudflare.com") || src.includes("turnstile")) kind = "Cloudflare Turnstile";
    else if (src.includes("arkoselabs") || src.includes("funcaptcha")) kind = "Arkose";
    else if (title.includes("captcha")) kind = "CAPTCHA";
    if (!kind) continue;
    const r = f.getBoundingClientRect();
    const interactive = visible(f) && r.width > 60 && r.height > 60 && !src.includes("size=invisible");
    out.captcha.push(`${kind}${interactive ? " (visible challenge)" : " (invisible/background)"}`);
  }
  for (const el of all) {
    if (el.id === "px-captcha" && visible(el)) out.captcha.push("PerimeterX (visible challenge)");
  }

  const body = document.body ? clean(document.body.innerText || "") : "";
  out.text = body.slice(0, maxText);
  out.nextRef = nextRef;
  return out;
}
