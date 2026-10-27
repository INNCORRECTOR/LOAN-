(function () {
  var deck = document.getElementById("loanDeck");
  if (!deck) return;

  var stack = document.getElementById("deckStack");
  var stage = document.getElementById("deckStage");
  var counter = document.getElementById("deckCounter");
  var prevBtn = document.getElementById("deckPrev");
  var nextBtn = document.getElementById("deckNext");
  var openBtn = document.getElementById("deckOpen");
  var deleteForm = document.getElementById("deckDeleteForm");
  if (!stack || !stage) return;

  var cards = Array.prototype.slice.call(stack.querySelectorAll(".paper-card"));
  var total = cards.length;
  if (!total) return;

  var index = 0;
  var startX = 0;
  var startY = 0;
  var deltaX = 0;
  var tracking = false;
  var moved = false;
  var animating = false;

  function clamp(n, min, max) {
    return Math.max(min, Math.min(max, n));
  }

  function applyCard(card, rel, offset) {
    offset = offset || 0;
    card.style.transition = tracking ? "none" : "transform 0.28s ease, opacity 0.28s ease";

    if (rel === 0) {
      card.style.transform =
        "translate3d(" + offset + "px, 0, 0) rotate(" + offset / 32 + "deg)";
      card.style.opacity = "1";
      card.style.zIndex = "40";
      card.style.pointerEvents = tracking ? "none" : "auto";
      card.setAttribute("aria-hidden", "false");
    } else if (rel === 1) {
      card.style.transform = "translate3d(0, 12px, 0) scale(0.96)";
      card.style.opacity = "0.95";
      card.style.zIndex = "30";
      card.style.pointerEvents = "none";
      card.setAttribute("aria-hidden", "true");
    } else if (rel === 2) {
      card.style.transform = "translate3d(0, 22px, 0) scale(0.92)";
      card.style.opacity = "0.75";
      card.style.zIndex = "20";
      card.style.pointerEvents = "none";
      card.setAttribute("aria-hidden", "true");
    } else if (rel > 2) {
      card.style.transform = "translate3d(0, 30px, 0) scale(0.88)";
      card.style.opacity = "0";
      card.style.zIndex = "10";
      card.style.pointerEvents = "none";
      card.setAttribute("aria-hidden", "true");
    } else {
      // already passed
      card.style.transform = "translate3d(" + (rel * 60) + "px, 0, 0) scale(0.96)";
      card.style.opacity = "0";
      card.style.zIndex = "5";
      card.style.pointerEvents = "none";
      card.setAttribute("aria-hidden", "true");
    }
  }

  function render(offset) {
    cards.forEach(function (card, i) {
      applyCard(card, i - index, offset || 0);
    });
    if (counter) counter.textContent = index + 1 + " / " + total;
    if (prevBtn) prevBtn.disabled = index <= 0 || animating;
    if (nextBtn) nextBtn.disabled = index >= total - 1 || animating;
    var active = cards[index];
    if (active && openBtn) openBtn.href = active.getAttribute("data-open") || "#";
    if (active && deleteForm) {
      deleteForm.setAttribute("action", active.getAttribute("data-delete") || "#");
    }
  }

  function go(to) {
    var nextIndex = clamp(to, 0, total - 1);
    if (nextIndex === index && !tracking) {
      render(0);
      return;
    }
    index = nextIndex;
    deltaX = 0;
    render(0);
  }

  function next() {
    if (animating || index >= total - 1) return;
    go(index + 1);
  }

  function prev() {
    if (animating || index <= 0) return;
    go(index - 1);
  }

  function pointerPos(e) {
    if (e.touches && e.touches[0]) {
      return { x: e.touches[0].clientX, y: e.touches[0].clientY };
    }
    if (e.changedTouches && e.changedTouches[0]) {
      return { x: e.changedTouches[0].clientX, y: e.changedTouches[0].clientY };
    }
    return { x: e.clientX, y: e.clientY };
  }

  function onStart(e) {
    if (e.target.closest("a, button, input, select, textarea, label, form")) return;
    if (animating) return;
    var p = pointerPos(e);
    tracking = true;
    moved = false;
    startX = p.x;
    startY = p.y;
    deltaX = 0;
    stage.classList.add("is-dragging");
  }

  function onMove(e) {
    if (!tracking) return;
    var p = pointerPos(e);
    var dx = p.x - startX;
    var dy = p.y - startY;
    if (!moved && Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 10) {
      // vertical scroll — cancel drag
      tracking = false;
      stage.classList.remove("is-dragging");
      render(0);
      return;
    }
    if (Math.abs(dx) > 8) moved = true;
    if (moved && e.cancelable) e.preventDefault();
    deltaX = dx;
    // resistance at ends
    if ((index === 0 && deltaX > 0) || (index === total - 1 && deltaX < 0)) {
      deltaX = deltaX * 0.35;
    }
    render(deltaX);
  }

  function onEnd() {
    if (!tracking) return;
    tracking = false;
    stage.classList.remove("is-dragging");
    var threshold = Math.min(90, stage.clientWidth * 0.22);
    if (moved && deltaX <= -threshold && index < total - 1) {
      next();
    } else if (moved && deltaX >= threshold && index > 0) {
      prev();
    } else {
      render(0);
    }
    deltaX = 0;
    moved = false;
  }

  stage.addEventListener("mousedown", onStart);
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onEnd);

  stage.addEventListener("touchstart", onStart, { passive: true });
  stage.addEventListener("touchmove", onMove, { passive: false });
  stage.addEventListener("touchend", onEnd);
  stage.addEventListener("touchcancel", onEnd);

  if (prevBtn) {
    prevBtn.addEventListener("click", function (e) {
      e.preventDefault();
      prev();
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener("click", function (e) {
      e.preventDefault();
      next();
    });
  }

  document.addEventListener("keydown", function (e) {
    if (!deck.offsetParent) return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      prev();
    }
    if (e.key === "ArrowRight") {
      e.preventDefault();
      next();
    }
  });

  // initial paint
  cards.forEach(function (card) {
    card.style.position = "absolute";
    card.style.inset = "0";
  });
  render(0);
})();
