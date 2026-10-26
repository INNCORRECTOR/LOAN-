document.addEventListener("DOMContentLoaded", function () {
  var toggle = document.getElementById("sidebarToggle");
  var sidebar = document.getElementById("sidebar");
  if (toggle && sidebar) {
    toggle.addEventListener("click", function () {
      sidebar.classList.toggle("show");
    });
  }

  // Confirm destructive actions
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      var msg = el.getAttribute("data-confirm") || "Are you sure?";
      if (!window.confirm(msg)) {
        e.preventDefault();
      }
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
