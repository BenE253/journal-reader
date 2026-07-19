/* PaperShelf highlight engine (Phase 2).

   Two ways to create a highlight, chosen by device:

   TOUCH (Kindle-style) — Native text selection is disabled inside the
       article, which also suppresses iOS's Copy/Look Up menu (that menu
       cannot be hidden while native selection is active, and it overlaps
       any toolbar we draw). Instead: long-press a word to start a
       highlight, drag to extend it in either direction (the pending
       range is drawn live via the CSS Custom Highlight API, and the page
       auto-scrolls near the screen edges), then lift your finger — the
       highlight is saved in your last-used color and a small palette
       appears for recolor / note / delete.

   MOUSE (desktop) — Normal text selection; a floating toolbar with the
       four color dots and a note button appears near the selection.
       Desktop has no system callout, so nothing overlaps.

   Fallback: a touch device too old for the Custom Highlight API keeps
   native selection, but the toolbar docks at the bottom of the screen
   where the iOS menu can't cover it.

   Storage/anchoring model (both modes): a highlight is the selected text
   plus ~50 characters of context on each side. On load, each highlight
   is re-found by searching for prefix+text+suffix (falling back to the
   bare text if unique) and wrapped in <mark data-hl-id> elements — one
   per covered text piece, so highlights can span paragraphs. Tapping a
   mark reopens the palette in edit mode. */

(function () {
  "use strict";

  var root = document.getElementById("reader-content");
  if (!root || !root.dataset.paperId) return;
  // No article content (still converting / failed) → nothing to highlight.
  if (root.querySelector(".reader-placeholder")) return;

  var paperId = root.dataset.paperId;
  var COLORS = ["yellow", "green", "blue", "pink"];
  var CONTEXT_CHARS = 50;
  var COLOR_KEY = "papershelf-hl-color";

  // "Coarse pointer" ≈ finger-first device (phone/tablet).
  var touchMode = window.matchMedia("(pointer: coarse)").matches;
  var supportsCustomHighlight =
    typeof window.Highlight === "function" && typeof CSS !== "undefined" && !!CSS.highlights;
  var kindleMode = touchMode && supportsCustomHighlight;

  function lastColor() {
    var c = localStorage.getItem(COLOR_KEY);
    return COLORS.indexOf(c) !== -1 ? c : "yellow";
  }

  function rememberColor(c) { localStorage.setItem(COLOR_KEY, c); }

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

  /* Character offset of a (node, offsetInNode) position within the
     whole article text. */
  function offsetOf(container, offsetInNode) {
    var probe = document.createRange();
    probe.selectNodeContents(root);
    probe.setEnd(container, offsetInNode);
    return probe.toString().length;
  }

  /* Build a live Range covering article characters [start, end). */
  function rangeFromOffsets(start, end) {
    var nodes = textNodes();
    var range = document.createRange();
    var pos = 0;
    var startSet = false;
    for (var i = 0; i < nodes.length; i++) {
      var len = nodes[i].nodeValue.length;
      if (!startSet && pos + len > start) {
        range.setStart(nodes[i], start - pos);
        startSet = true;
      }
      if (startSet && pos + len >= end) {
        range.setEnd(nodes[i], Math.min(end - pos, len));
        return range;
      }
      pos += len;
    }
    return startSet ? range : null;
  }

  /* Wrap the characters [start, end) in <mark> elements. A highlight
     crossing element boundaries (spanning a <b>, a paragraph break, …)
     gets one <mark> per covered text piece, all sharing data-hl-id. */
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

  /* Re-find a stored highlight in the article text. Returns false if the
     text can't be located (e.g. the paper was re-converted and the
     wording changed slightly). */
  function anchor(hl) {
    var full = fullText();
    var index = -1;

    if (hl.prefix || hl.suffix) {
      var at = full.indexOf(hl.prefix + hl.text + hl.suffix);
      if (at !== -1) index = at + hl.prefix.length;
    }
    if (index === -1) {
      // Bare-text fallback — only if it appears exactly once, otherwise
      // we might highlight the wrong occurrence.
      var first = full.indexOf(hl.text);
      if (first !== -1 && full.indexOf(hl.text, first + 1) === -1) index = first;
    }
    if (index === -1) return false;

    wrapRange(index, index + hl.text.length, hl.id, hl.color, !!hl.note);
    return true;
  }

  /* ================= create on the server ================= */

  function createHighlight(start, end, color, options) {
    options = options || {};
    var full = fullText();
    var payload = {
      text: full.slice(start, end),
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
        if (options.openNote) {
          openNoteSheet(hl.id, "");
        } else if (options.showEditToolbar) {
          // Kindle flow: highlight is already applied; offer the palette
          // so a different color / note / undo is one tap away.
          mode = { kind: "edit", id: String(hl.id) };
          var rect = rangeFromOffsets(start, end).getBoundingClientRect();
          showToolbar(rect);
        }
      })
      .catch(function () { alert("Couldn't save highlight — is the server reachable?"); });
  }

  /* ================= floating toolbar ================= */

  var toolbar = document.createElement("div");
  toolbar.className = "hl-toolbar";
  toolbar.hidden = true;
  document.body.appendChild(toolbar);

  // Mode: {kind: "create", range} (desktop selection) or {kind: "edit", id}
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
    toolbar.classList.remove("hl-toolbar-bottom");
    toolbar.hidden = false;
    // Position above the target, or below it when too near the top.
    var width = toolbar.offsetWidth;
    var left = rect.left + rect.width / 2 - width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    var top = rect.top - toolbar.offsetHeight - 10;
    if (top < 60) top = rect.bottom + 10;
    toolbar.style.left = left + "px";
    toolbar.style.top = top + "px";
  }

  function showToolbarBottom() {
    // Fallback placement: docked at the bottom, clear of the iOS callout.
    buildToolbar();
    toolbar.classList.add("hl-toolbar-bottom");
    toolbar.style.left = "";
    toolbar.style.top = "";
    toolbar.hidden = false;
  }

  function hideToolbar() {
    toolbar.hidden = true;
    mode = null;
  }

  // Tapping toolbar buttons must not clear a text selection first —
  // preventing default on pointerdown keeps it alive until click runs.
  toolbar.addEventListener("pointerdown", function (e) { e.preventDefault(); });

  function pickColor(color) {
    if (!mode) return;
    rememberColor(color);
    if (mode.kind === "create") {
      saveDesktopSelection(color, false);
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
      // Note button on a fresh desktop selection: save first, then note.
      saveDesktopSelection(lastColor(), true);
      return;
    }
    var id = mode.id;
    hideToolbar();
    // Fetch the saved note text so editing starts from what's stored.
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

  /* ================= desktop flow: native selection ================= */
  /* Also used as the fallback on touch devices without the Custom
     Highlight API — there the toolbar docks at the bottom instead of
     floating near the selection (where the iOS callout would cover it). */

  if (!kindleMode) {
    var selectionTimer = null;
    document.addEventListener("selectionchange", function () {
      clearTimeout(selectionTimer);
      selectionTimer = setTimeout(onSelectionSettled, 250);
    });
  }

  function onSelectionSettled() {
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
    if (touchMode) {
      showToolbarBottom();
    } else {
      showToolbar(range.getBoundingClientRect());
    }
  }

  function saveDesktopSelection(color, thenOpenNote) {
    var range = mode.range;
    var start = offsetOf(range.startContainer, range.startOffset);
    var end = start + range.toString().length;
    window.getSelection().removeAllRanges();
    hideToolbar();
    createHighlight(start, end, color, { openNote: thenOpenNote });
  }

  /* ================= Kindle flow: long-press + drag ================= */

  if (kindleMode) {
    // Kill native selection inside the article: no iOS callout, no
    // selection handles — our long-press gesture takes over.
    root.classList.add("no-native-select");

    var LONG_PRESS_MS = 400;
    var MOVE_TOLERANCE = 8;   // px of finger drift allowed before it's a scroll
    var EDGE_ZONE = 70;       // px from screen edge that triggers auto-scroll

    var pressTimer = null;
    var startX = 0, startY = 0, lastX = 0, lastY = 0;
    var selecting = false;
    // Anchor = the word first pressed; selection always contains it.
    var anchorStart = 0, anchorEnd = 0, selStart = 0, selEnd = 0;

    var caretFromPoint = function (x, y) {
      if (document.caretRangeFromPoint) return document.caretRangeFromPoint(x, y);
      if (document.caretPositionFromPoint) {           // Firefox
        var pos = document.caretPositionFromPoint(x, y);
        if (!pos) return null;
        var r = document.createRange();
        r.setStart(pos.offsetNode, pos.offset);
        return r;
      }
      return null;
    };

    /* The word under the finger, as [start, end) offsets in the article
       text — or null if the finger isn't over article text. */
    function wordAt(x, y) {
      var caret = caretFromPoint(x, y);
      if (!caret) return null;
      var node = caret.startContainer;
      if (node.nodeType !== Node.TEXT_NODE || !root.contains(node)) return null;

      var text = node.nodeValue;
      var i = Math.min(caret.startOffset, text.length);
      var s = i, e = i;
      while (s > 0 && !/\s/.test(text[s - 1])) s--;
      while (e < text.length && !/\s/.test(text[e])) e++;
      if (s === e) return null; // finger over whitespace

      var base = offsetOf(node, 0);
      return { start: base + s, end: base + e };
    }

    function renderPending(color) {
      var range = rangeFromOffsets(selStart, selEnd);
      COLORS.forEach(function (c) { CSS.highlights.delete("pshl-pending-" + c); });
      if (range) CSS.highlights.set("pshl-pending-" + color, new Highlight(range));
    }

    function clearPending() {
      COLORS.forEach(function (c) { CSS.highlights.delete("pshl-pending-" + c); });
    }

    function cancelPress() {
      clearTimeout(pressTimer);
      pressTimer = null;
    }

    document.addEventListener("touchstart", function (e) {
      if (e.touches.length !== 1) { cancelPress(); return; }
      if (toolbar.contains(e.target) || noteSheet.contains(e.target)) return;
      if (!root.contains(e.target)) return;
      // Long-pressing an existing highlight starts a new (overlapping)
      // one; a quick tap on it still opens the edit palette via click.

      var t = e.touches[0];
      startX = lastX = t.clientX;
      startY = lastY = t.clientY;
      cancelPress();
      pressTimer = setTimeout(function () {
        pressTimer = null;
        var word = wordAt(lastX, lastY);
        if (!word) return; // pressed margin/figure — let it be a scroll
        selecting = true;
        anchorStart = selStart = word.start;
        anchorEnd = selEnd = word.end;
        renderPending(lastColor());
      }, LONG_PRESS_MS);
    }, { passive: true });

    document.addEventListener("touchmove", function (e) {
      var t = e.touches[0];
      if (!t) return;
      lastX = t.clientX;
      lastY = t.clientY;

      if (selecting) {
        // Finger is extending the highlight: stop the page from scrolling
        // out from under it...
        e.preventDefault();
        // ...except our own deliberate auto-scroll near the edges, which
        // lets long highlights continue past the visible screen.
        if (lastY > window.innerHeight - EDGE_ZONE) window.scrollBy(0, 6);
        else if (lastY < EDGE_ZONE + 60) window.scrollBy(0, -6);

        var word = wordAt(lastX, lastY);
        if (word) {
          // Grow from the anchor word toward the finger, either direction.
          selStart = Math.min(anchorStart, word.start);
          selEnd = Math.max(anchorEnd, word.end);
          renderPending(lastColor());
        }
      } else if (pressTimer !== null) {
        // Still waiting for the long press: real movement means the user
        // is scrolling, not highlighting.
        var moved = Math.hypot(lastX - startX, lastY - startY);
        if (moved > MOVE_TOLERANCE) cancelPress();
      }
    }, { passive: false });

    function endTouch(e) {
      cancelPress();
      if (!selecting) return;
      // preventDefault stops the browser synthesizing a click, which
      // would instantly close the palette we're about to show.
      if (e.cancelable) e.preventDefault();
      selecting = false;
      clearPending();
      if (selEnd > selStart) {
        createHighlight(selStart, selEnd, lastColor(), { showEditToolbar: true });
      }
    }

    document.addEventListener("touchend", endTouch, { passive: false });
    document.addEventListener("touchcancel", function () {
      cancelPress();
      if (selecting) { selecting = false; clearPending(); }
    }, { passive: true });
  }

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
