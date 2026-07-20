/* PaperShelf reader page behavior:
   1. Restore + save reading progress (debounced POST to the server).
   2. Overflow menu toggle.
   3. Full-screen figure lightbox with pinch-zoom and double-tap zoom.
   Phase 2 will add the highlight engine here. */

(function () {
  "use strict";

  var content = document.getElementById("reader-content");
  if (!content) return;
  var paperId = content.dataset.paperId;

  /* ---------- Progress tracking ---------- */

  function docHeight() {
    return document.documentElement.scrollHeight - window.innerHeight;
  }

  function currentProgress() {
    var h = docHeight();
    if (h <= 0) return 0;
    return Math.min(1, Math.max(0, window.scrollY / h));
  }

  // Restore the saved position once images have had a chance to lay out
  // (image heights shift the scroll target).
  var savedProgress = parseFloat(content.dataset.progress || "0");
  if (savedProgress > 0.01) {
    window.addEventListener("load", function () {
      window.scrollTo(0, savedProgress * docHeight());
    });
  }

  var saveTimer = null;
  var lastSaved = savedProgress;

  function saveProgress() {
    var p = currentProgress();
    if (Math.abs(p - lastSaved) < 0.005) return; // nothing meaningful changed
    lastSaved = p;
    fetch("/paper/" + paperId + "/progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ progress: p })
    }).catch(function () { /* offline/moving between networks: ignore */ });
  }

  window.addEventListener("scroll", function () {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveProgress, 800); // debounce: save after scrolling settles
  }, { passive: true });

  // Best-effort save when the tab is backgrounded or closed. sendBeacon
  // survives page unload where fetch may not.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      var blob = new Blob(
        [JSON.stringify({ progress: currentProgress() })],
        { type: "application/json" }
      );
      navigator.sendBeacon("/paper/" + paperId + "/progress", blob);
    }
  });

  /* ---------- Overflow menu ---------- */

  var menuBtn = document.getElementById("menu-btn");
  var menuSheet = document.getElementById("menu-sheet");
  if (menuBtn && menuSheet) {
    menuBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      menuSheet.hidden = !menuSheet.hidden;
    });
    // Tapping anywhere else closes the menu.
    document.addEventListener("click", function (e) {
      if (!menuSheet.hidden && !menuSheet.contains(e.target)) {
        menuSheet.hidden = true;
      }
    });
  }

  /* ---------- Figure lightbox ---------- */

  var lightbox = document.getElementById("lightbox");
  var lightboxImg = document.getElementById("lightbox-img");
  var closeBtn = document.getElementById("lightbox-close");
  if (!lightbox || !lightboxImg) return;

  // Zoom/pan state. We apply translate + scale ourselves rather than
  // relying on the wrapper's scrollbars: a CSS transform doesn't grow the
  // element's layout box, so an "overflow: auto" wrapper has nothing extra
  // to scroll into — which is why zoom used to pin you to a corner.
  var scale = 1;
  var tx = 0, ty = 0; // current pan offset in screen px

  function applyTransform() {
    lightboxImg.style.transform =
      "translate(" + tx + "px," + ty + "px) scale(" + scale + ")";
  }

  /* Keep the image from being dragged completely off-screen: clamp the
     pan so its scaled edges never cross the viewport centre lines. */
  function clampPan() {
    var rect = lightboxImg.getBoundingClientRect();
    // How far the scaled image overhangs the viewport on each axis.
    var overX = Math.max(0, (rect.width - window.innerWidth) / 2);
    var overY = Math.max(0, (rect.height - window.innerHeight) / 2);
    tx = Math.max(-overX, Math.min(overX, tx));
    ty = Math.max(-overY, Math.min(overY, ty));
  }

  function setScale(next) {
    scale = Math.min(6, Math.max(1, next));
    if (scale === 1) { tx = 0; ty = 0; } // snap back to centred when fit
    clampPan();
    applyTransform();
  }

  function openLightbox(src) {
    lightboxImg.src = src;
    scale = 1; tx = 0; ty = 0;
    applyTransform();
    lightbox.hidden = false;
    document.body.style.overflow = "hidden"; // stop the article scrolling behind
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lightboxImg.src = "";
    document.body.style.overflow = "";
  }

  content.addEventListener("click", function (e) {
    if (e.target.tagName === "IMG") openLightbox(e.target.src);
  });

  closeBtn.addEventListener("click", closeLightbox);
  // A plain tap on the dark backdrop (not a drag) closes the lightbox.
  lightbox.addEventListener("click", function (e) {
    if (!dragged && (e.target === lightbox || e.target.id === "lightbox-pan")) {
      closeLightbox();
    }
  });

  // ---- touch gestures: pinch to zoom, one-finger drag to pan ----
  var pinchStartDist = 0;
  var pinchStartScale = 1;
  var panStartX = 0, panStartY = 0, panOrigTx = 0, panOrigTy = 0;
  var dragged = false; // distinguishes a pan/pinch from a tap
  var lastTap = 0;      // for double-tap detection

  function touchDistance(touches) {
    var dx = touches[0].clientX - touches[1].clientX;
    var dy = touches[0].clientY - touches[1].clientY;
    return Math.hypot(dx, dy);
  }

  var pan = document.getElementById("lightbox-pan") || lightbox;

  pan.addEventListener("touchstart", function (e) {
    dragged = false;
    if (e.touches.length === 2) {
      pinchStartDist = touchDistance(e.touches);
      pinchStartScale = scale;
    } else if (e.touches.length === 1 && scale > 1) {
      // Begin a pan from the current offset.
      panStartX = e.touches[0].clientX;
      panStartY = e.touches[0].clientY;
      panOrigTx = tx;
      panOrigTy = ty;
    }
  }, { passive: true });

  pan.addEventListener("touchmove", function (e) {
    if (e.touches.length === 2 && pinchStartDist > 0) {
      e.preventDefault(); // keep iOS from doing its own gesture handling
      dragged = true;
      setScale(pinchStartScale * (touchDistance(e.touches) / pinchStartDist));
    } else if (e.touches.length === 1 && scale > 1) {
      // One-finger drag pans the zoomed image.
      e.preventDefault();
      dragged = true;
      tx = panOrigTx + (e.touches[0].clientX - panStartX);
      ty = panOrigTy + (e.touches[0].clientY - panStartY);
      clampPan();
      applyTransform();
    }
  }, { passive: false });

  pan.addEventListener("touchend", function (e) {
    if (e.touches.length > 0) return;
    pinchStartDist = 0;
    // Double-tap toggles between fit and 2.5x (only when it wasn't a drag).
    if (!dragged) {
      var now = Date.now();
      if (now - lastTap < 300) setScale(scale > 1 ? 1 : 2.5);
      lastTap = now;
    }
  });
})();
