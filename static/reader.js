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

  var scale = 1;

  function openLightbox(src) {
    lightboxImg.src = src;
    setScale(1);
    lightbox.hidden = false;
    document.body.style.overflow = "hidden"; // stop the article scrolling behind
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lightboxImg.src = "";
    document.body.style.overflow = "";
  }

  function setScale(next) {
    scale = Math.min(6, Math.max(1, next));
    lightboxImg.style.transform = "scale(" + scale + ")";
    // When zoomed, let the wrapper scroll to pan the enlarged image.
    lightboxImg.style.transformOrigin = scale > 1 ? "top left" : "center center";
  }

  content.addEventListener("click", function (e) {
    if (e.target.tagName === "IMG") openLightbox(e.target.src);
  });

  closeBtn.addEventListener("click", closeLightbox);
  lightbox.addEventListener("click", function (e) {
    if (e.target === lightbox || e.target.id === "lightbox-pan") closeLightbox();
  });

  // Pinch zoom via raw touch events (works in iOS standalone PWAs where
  // native page zoom is disabled).
  var pinchStartDist = 0;
  var pinchStartScale = 1;

  function touchDistance(touches) {
    var dx = touches[0].clientX - touches[1].clientX;
    var dy = touches[0].clientY - touches[1].clientY;
    return Math.hypot(dx, dy);
  }

  lightboxImg.addEventListener("touchstart", function (e) {
    if (e.touches.length === 2) {
      pinchStartDist = touchDistance(e.touches);
      pinchStartScale = scale;
    }
  }, { passive: true });

  lightboxImg.addEventListener("touchmove", function (e) {
    if (e.touches.length === 2 && pinchStartDist > 0) {
      e.preventDefault(); // keep iOS from doing its own gesture handling
      setScale(pinchStartScale * (touchDistance(e.touches) / pinchStartDist));
    }
  }, { passive: false });

  // Double-tap toggles between fit and 2.5x for one-handed use.
  var lastTap = 0;
  lightboxImg.addEventListener("touchend", function (e) {
    if (e.touches.length > 0) return;
    var now = Date.now();
    if (now - lastTap < 300) setScale(scale > 1 ? 1 : 2.5);
    lastTap = now;
  });
})();
