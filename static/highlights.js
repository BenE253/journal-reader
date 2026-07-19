/* PaperShelf highlight engine (Phase 2).

   How highlights work:

   CREATE  — When you select text, a floating toolbar appears with four
             color dots and a note button. Choosing one saves the selected
             text plus ~50 characters of context before and after it
             (the "prefix"/"suffix") to the server, then wraps the
             selection in <mark> elements.

   ANCHOR  — On page load we fetch the paper's highlights and re-find each
             one in the article text: first by searching for
             prefix + text + suffix (robust against duplicate phrases),
             falling back to the text alone if it's unique. Matches are
             wrapped in <mark data-hl-id="..."> elements.

   EDIT    — Tapping an existing highlight reopens the toolbar in edit
             mode: change color, add/edit a note, or delete.

   Everything operates on character offsets into the article's combined
   text content, so highlights can span paragraphs, bold spans, links,
   etc. — wrapRange() walks the text nodes and wraps each covered piece
   in its own <mark>. */

(function () {
  "use strict";

  var root = document.getElementById("reader-content");
  if (!root || !root.dataset.paperId) return;
  // No article content (still converting / failed) → nothing to highlight.
  if (root.querySelector(".reader-placeholder")) return;

  var paperId = root.dataset.paperId;
  var COLORS = ["yellow", "green", "blue", "pink"];
  var CONTEXT_CHARS = 50;

  /* ================= text-offset utilities ================= */

  function fullText() {
    // The article's text as one string. Built from a Range so offsets
    // line up exactly with Range/selection calculations elsewhere.
    var range = document.createRange();
    range.selectNodeContents(root);
    return range.toString();
  }

  function textNodes() {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    var nodes = [];
    var node;
    while ((node = walker.nextNode())) nodes.push(node);
    return nodes;
  }

  /* Wrap the characters [start, end) of the article text in <mark>
     elements. A highlight crossing element boundaries (e.g. spanning a
     <b> or a paragraph break) gets one <mark> per covered text piece,
     all sharing the same data-hl-id. */
  function wrapRange(start, end, id, color, hasNote) {
    var nodes = textNodes();
    var pos = 0;
    for (var i = 0; i < nodes.length && pos < end; i++) {
      var node = nodes[i];
      var len = node.nodeValue.length;
      var nodeStart = pos;
      pos += len;
      if (pos <= start) continue; // node entirely before the highlight

      var from = Math.max(start - nodeStart, 0);
      var to = Math.min(end - nodeStart, len);
      if (from >= to) continue;

      // Isolate exactly the covered slice of this text node...
      var target = node;
      if (from > 0) target = target.splitText(from);
      if (to - from < target.nodeValue.length) target.splitText(to - from);

      // ...and wrap it.
      var mark = document.createElement("mark");
      mark.className = "hl hl-" + color + (hasNote ? " hl-noted" : "");
      mark.dataset.hlId = id;
      target.parentNode.insertBefore(mark, target);
      mark.appendChild(target);
    }
  }

  function marksFor(id) {
    return root.querySelectorAll('mark.hl[data-hl-id="' + id + '"]');
  }

  function unwrap(id) {
    marksFor(id).forEach(function (mark) {
      var parent = mark.parentNode;
      while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
      parent.removeChild(mark);
      parent.normalize(); // merge the split text nodes back together
    });
  }

  /* Re-find a stored highlight in the article text. Returns true if it
     was anchored, false if the text couldn't be located (e.g. the paper
     was re-converted and the wording changed). */
  function anchor(hl) {
    var full = fullText();
    var index = -1;

    if (hl.prefix || hl.suffix) {
      var withContext = hl.prefix + hl.text + hl.suffix;
      var at = full.indexOf(withContext);
      if (at !== -1) index = at + hl.prefix.length;
    }
    if (index === -1) {
      // Fall back to the bare text — but only if it appears exactly once,
      // otherwise we might highlight the wrong occurrence.
      var first = full.indexOf(hl.text);
      if (first !== -1 && full.indexOf(hl.text, first + 1) === -1) index = first;
    }
    if (index === -1) return false;

    wrapRange(index, index + hl.text.length, hl.id, hl.color, !!hl.note);
    return true;
  }

  /* ================= floating toolbar ================= */

  var toolbar = document.createElement("div");
  toolbar.className = "hl-toolbar";
  toolbar.hidden = true;
  document.body.appendChild(toolbar);

  // Mode: {kind: "create", range} or {kind: "edit", id}
  var mode = null;

  function buildToolbar() {
    toolbar.innerHTML = "";
    COLORS.forEach(function (color) {
      var dot = document.createElement("button");
      dot.className = "hl-dot hl-dot-" + color;
      dot.setAttribute("aria-label", color + " highlight");
      dot.addEventListener("click", function () { pickColor(color); });
      toolbar.appendChild(dot);
    });

    var noteBtn = document.createElement("button");
    noteBtn.className = "hl-tool";
    noteBtn.textContent = "✎";
    noteBtn.setAttribute("aria-label", "Add note");
    noteBtn.addEventListener("click", openNoteForCurrent);
    toolbar.appendChild(noteBtn);

    if (mode && mode.kind === "edit") {
      var delBtn = document.createElement("button");
      delBtn.className = "hl-tool hl-tool-danger";
      delBtn.textContent = "🗑";
      delBtn.setAttribute("aria-label", "Delete highlight");
      delBtn.addEventListener("click", deleteCurrent);
      toolbar.appendChild(delBtn);
    }
  }

  function showToolbar(rect) {
    buildToolbar();
    toolbar.hidden = false;
    // Position above the selection, or below it when too near the top.
    var width = toolbar.offsetWidth;
    var left = rect.left + rect.width / 2 - width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    var top = rect.top - toolbar.offsetHeight - 10;
    if (top < 60) top = rect.bottom + 10;
    toolbar.style.left = left + "px";
    toolbar.style.top = top + "px";
  }

  function hideToolbar() {
    toolbar.hidden = true;
    mode = null;
  }

  // Tapping toolbar buttons must not clear the text selection first —
  // preventing default on pointerdown keeps the selection alive until
  // the click handler runs.
  toolbar.addEventListener("pointerdown", function (e) { e.preventDefault(); });

  /* ================= selection handling (create) ================= */

  var selectionTimer = null;
  document.addEventListener("selectionchange", function () {
    clearTimeout(selectionTimer);
    selectionTimer = setTimeout(onSelectionSettled, 250);
  });

  function onSelectionSettled() {
    // Don't fight with an open edit toolbar or note sheet.
    if (mode && mode.kind === "edit") return;
    if (!noteSheet.hidden) return;

    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      if (mode && mode.kind === "create") hideToolbar();
      return;
    }
    var range = sel.getRangeAt(0);
    if (!root.contains(range.commonAncestorContainer)) return;
    if (!range.toString().trim()) return;

    mode = { kind: "create", range: range.cloneRange() };
    showToolbar(range.getBoundingClientRect());
  }

  /* Character offset of a range boundary within the whole article. */
  function offsetOf(container, offsetInNode) {
    var probe = document.createRange();
    probe.selectNodeContents(root);
    probe.setEnd(container, offsetInNode);
    return probe.toString().length;
  }

  function saveSelection(color, thenOpenNote) {
    var range = mode.range;
    var text = range.toString();
    var start = offsetOf(range.startContainer, range.startOffset);
    var end = start + text.length;
    var full = fullText();

    var payload = {
      text: text,
      prefix: full.slice(Math.max(0, start - CONTEXT_CHARS), start),
      suffix: full.slice(end, end + CONTEXT_CHARS),
      color: color
    };

    fetch("/api/paper/" + paperId + "/highlights", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (res) { return res.json(); })
      .then(function (hl) {
        wrapRange(start, end, hl.id, hl.color, false);
        window.getSelection().removeAllRanges();
        hideToolbar();
        if (thenOpenNote) openNoteSheet(hl.id, "");
      })
      .catch(function () { alert("Couldn't save highlight — is the server reachable?"); });
  }

  /* ================= edit mode (tap an existing mark) ================= */

  root.addEventListener("click", function (e) {
    var mark = e.target.closest && e.target.closest("mark.hl");
    if (!mark) return;
    e.preventDefault();
    e.stopPropagation();
    mode = { kind: "edit", id: mark.dataset.hlId };
    showToolbar(mark.getBoundingClientRect());
  });

  // Tapping outside the toolbar closes edit mode.
  document.addEventListener("click", function (e) {
    if (mode && mode.kind === "edit" && !toolbar.contains(e.target)
        && !(e.target.closest && e.target.closest("mark.hl"))) {
      hideToolbar();
    }
  });

  function pickColor(color) {
    if (!mode) return;
    if (mode.kind === "create") {
      saveSelection(color, false);
      return;
    }
    // Edit: recolor all this highlight's <mark> pieces, then persist.
    var id = mode.id;
    marksFor(id).forEach(function (mark) {
      COLORS.forEach(function (c) { mark.classList.remove("hl-" + c); });
      mark.classList.add("hl-" + color);
    });
    patchHighlight(id, { color: color });
    hideToolbar();
  }

  function deleteCurrent() {
    if (!mode || mode.kind !== "edit") return;
    var id = mode.id;
    fetch("/api/highlight/" + id, { method: "DELETE" })
      .then(function () { unwrap(id); })
      .catch(function () { alert("Couldn't delete — is the server reachable?"); });
    hideToolbar();
  }

  function patchHighlight(id, fields) {
    return fetch("/api/highlight/" + id, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields)
    });
  }

  /* ================= note sheet ================= */

  var noteSheet = document.createElement("div");
  noteSheet.className = "note-sheet";
  noteSheet.hidden = true;
  noteSheet.innerHTML =
    '<textarea rows="4" placeholder="Add a note…"></textarea>' +
    '<div class="note-sheet-actions">' +
    '  <button type="button" class="btn" data-note-cancel>Cancel</button>' +
    '  <button type="button" class="btn btn-primary" data-note-save>Save note</button>' +
    "</div>";
  document.body.appendChild(noteSheet);

  var noteTextarea = noteSheet.querySelector("textarea");
  var noteForId = null;

  function openNoteSheet(id, existingText) {
    noteForId = id;
    noteTextarea.value = existingText || "";
    noteSheet.hidden = false;
    noteTextarea.focus();
  }

  function openNoteForCurrent() {
    if (!mode) return;
    if (mode.kind === "create") {
      // Note button on a fresh selection: save as yellow, then open notes.
      saveSelection("yellow", true);
      return;
    }
    var id = mode.id;
    hideToolbar();
    // Fetch the current note text so editing starts from what's saved.
    fetch("/api/paper/" + paperId + "/highlights")
      .then(function (res) { return res.json(); })
      .then(function (highlights) {
        var hl = highlights.find(function (h) { return String(h.id) === String(id); });
        openNoteSheet(id, hl ? hl.note : "");
      })
      .catch(function () { openNoteSheet(id, ""); });
  }

  noteSheet.querySelector("[data-note-cancel]").addEventListener("click", function () {
    noteSheet.hidden = true;
    noteForId = null;
  });

  noteSheet.querySelector("[data-note-save]").addEventListener("click", function () {
    var id = noteForId;
    var note = noteTextarea.value;
    noteSheet.hidden = true;
    noteForId = null;
    patchHighlight(id, { note: note }).then(function () {
      // Dotted underline signals "this highlight has a note".
      marksFor(id).forEach(function (mark) {
        mark.classList.toggle("hl-noted", note.trim().length > 0);
      });
    });
  });

  /* ================= initial load ================= */

  fetch("/api/paper/" + paperId + "/highlights")
    .then(function (res) { return res.json(); })
    .then(function (highlights) {
      var lost = 0;
      highlights.forEach(function (hl) {
        if (!anchor(hl)) lost += 1;
      });
      if (lost > 0) {
        // Not fatal — the text may have changed after a re-conversion.
        console.warn(lost + " highlight(s) could not be re-anchored in this paper.");
      }
    })
    .catch(function () { /* offline: reader still works, minus highlights */ });
})();
