document.addEventListener("DOMContentLoaded", function () {
  document.body.classList.add("is-loaded");

  var toggle = document.getElementById("sidebarToggle");
  var sidebar = document.getElementById("sidebar");
  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      sidebar.classList.toggle("show");
    });
  }

  // Collapsible panels (− Pay / + Add)
  document.querySelectorAll("[data-fold]").forEach(function (panel) {
    var trigger = panel.querySelector(".fold-trigger");
    var body = panel.querySelector(".fold-body");
    var hint = panel.querySelector(".fold-hint");
    if (!trigger || !body) return;
    trigger.addEventListener("click", function () {
      var open = panel.classList.toggle("is-open");
      trigger.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        body.hidden = false;
        if (hint) hint.textContent = "Tap to close";
      } else {
        body.hidden = true;
        if (hint) hint.textContent = "Tap to open";
      }
    });
  });

  // Custom Yes / No confirm (download & delete)
  function askYesNo(message, onYes) {
    var existing = document.getElementById("confirmOverlay");
    if (existing) existing.remove();

    var overlay = document.createElement("div");
    overlay.id = "confirmOverlay";
    overlay.className = "confirm-overlay";
    var logoSrc = (document.querySelector(".brand-logo") || {}).src || "/static/logo/image.png";
    overlay.innerHTML =
      '<div class="confirm-card" role="dialog" aria-modal="true">' +
      '<div class="confirm-logo-wrap"><img class="confirm-logo" src="" alt="TIA LOAN COMPANY"></div>' +
      '<p class="confirm-msg"></p>' +
      '<div class="confirm-actions">' +
      '<button type="button" class="btn btn-outline-secondary confirm-no">No</button>' +
      '<button type="button" class="btn btn-primary confirm-yes">Yes</button>' +
      "</div></div>";
    overlay.querySelector(".confirm-logo").src = logoSrc;
    overlay.querySelector(".confirm-msg").textContent = message;
    document.body.appendChild(overlay);

    function close() {
      overlay.remove();
    }
    overlay.querySelector(".confirm-no").addEventListener("click", close);
    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) close();
    });
    overlay.querySelector(".confirm-yes").addEventListener("click", function () {
      close();
      onYes();
    });
  }

  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      // Second click after Yes — allow real action (keeps formaction working)
      if (el.dataset.confirmed === "1") {
        delete el.dataset.confirmed;
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      var msg = el.getAttribute("data-confirm") || "Are you sure?";
      askYesNo(msg, function () {
        el.dataset.confirmed = "1";
        el.click();
      });
    });
  });

  // When borrower changes on payment form, filter loans
  var borrowerSelect = document.getElementById("borrower_id");
  var loanSelect = document.getElementById("loan_id");
  if (borrowerSelect && loanSelect) {
    var allOptions = Array.prototype.slice.call(loanSelect.querySelectorAll("option"));
    function filterLoans() {
      var bid = borrowerSelect.value;
      var current = loanSelect.value;
      loanSelect.innerHTML = "";
      allOptions.forEach(function (opt) {
        if (!opt.value || !bid || opt.getAttribute("data-borrower") === bid) {
          loanSelect.appendChild(opt.cloneNode(true));
        }
      });
      // Restore selection if still valid
      var still = Array.prototype.some.call(loanSelect.options, function (o) {
        return o.value === current;
      });
      if (still) loanSelect.value = current;
    }
    borrowerSelect.addEventListener("change", filterLoans);
    filterLoans();
  }
});
