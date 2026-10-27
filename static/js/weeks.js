/** Shared "Add week" row builder used by Create Loan and Loan details. */
window.LoanWeeks = (function () {
  function todayISO() {
    return new Date().toISOString().slice(0, 10);
  }

  function addDaysISO(iso, days) {
    var d = new Date((iso || todayISO()) + "T12:00:00");
    d.setDate(d.getDate() + days);
    return d.toISOString().slice(0, 10);
  }

  function init(options) {
    var body = document.getElementById(options.bodyId || "weeksBody");
    var hint = document.getElementById(options.hintId || "weeksEmptyHint");
    var addBtn = document.getElementById(options.addBtnId || "addWeekBtn");
    var interestInput = document.getElementById(options.interestId || "interest_amount");
    var startIndex = options.startIndex || 0;
    var defaultDate = options.defaultDate || null;
    var withStatus = !!options.withStatus;

    if (!body || !addBtn) return;

    function renumber() {
      var rows = body.querySelectorAll(".week-card.week-new");
      rows.forEach(function (row, i) {
        var num = row.querySelector(".week-num");
        if (num) num.textContent = startIndex + i + 1;
      });
      // also renumber existing for display consistency
      var all = body.querySelectorAll(".week-card");
      all.forEach(function (row, i) {
        var num = row.querySelector(".week-num");
        if (num) num.textContent = i + 1;
      });
      if (hint) {
        hint.style.display = all.length > 0 ? "none" : "block";
      }
    }

    function recalcRow(row) {
      var interest = parseFloat((row.querySelector(".week-interest") || {}).value) || 0;
      var extra = parseFloat((row.querySelector(".week-extra") || {}).value) || 0;
      var totalEl = row.querySelector(".week-total");
      if (totalEl) totalEl.textContent = (interest + extra).toFixed(2);
    }

    function wireRow(row) {
      row.querySelectorAll(".week-interest, .week-extra").forEach(function (el) {
        el.addEventListener("input", function () {
          recalcRow(row);
        });
      });
      var extraType = row.querySelector(".week-extra-type");
      if (extraType) {
        extraType.addEventListener("change", function () {
          if (this.value === "none") {
            var extra = row.querySelector(".week-extra");
            if (extra) extra.value = "0";
          }
          recalcRow(row);
        });
      }
      var removeBtn = row.querySelector(".remove-week");
      if (removeBtn) {
        removeBtn.addEventListener("click", function () {
          row.remove();
          renumber();
        });
      }
      recalcRow(row);
    }

    function lastFilledDate() {
      var dates = body.querySelectorAll(".week-date");
      for (var i = dates.length - 1; i >= 0; i--) {
        if (dates[i].value) return dates[i].value;
      }
      var locked = body.querySelectorAll("[data-due-date]");
      if (locked.length) return locked[locked.length - 1].getAttribute("data-due-date");
      return defaultDate || null;
    }

    function addWeek(dateVal, interestVal) {
      var priorDate = lastFilledDate();
      var hadWeeks = body.querySelectorAll(".week-card").length > 0;

      var card = document.createElement("div");
      card.className = "week-card week-new";
      var statusBlock = withStatus
        ? '<div class="week-field">' +
          '<label class="form-label">Status</label>' +
          '<select name="week_status" class="form-select form-select-sm week-status">' +
          '<option value="not_paid" selected>Not paid</option>' +
          '<option value="paid">Paid</option>' +
          '<option value="maaf">Maaf</option>' +
          "</select></div>"
        : "";

      card.innerHTML =
        '<div class="week-card-head">' +
        '<span class="week-badge">Week <span class="week-num"></span></span>' +
        '<div class="week-total-wrap">Total <strong class="week-total">0.00</strong></div>' +
        '<button type="button" class="btn btn-sm btn-outline-danger remove-week">Remove</button>' +
        "</div>" +
        '<div class="week-fields">' +
        '<div class="week-field week-field-date">' +
        '<label class="form-label">Pay date</label>' +
        '<input type="date" name="week_date" class="form-control form-control-sm week-date" required>' +
        "</div>" +
        '<div class="week-field">' +
        '<label class="form-label">Interest</label>' +
        '<input type="number" step="0.01" min="0" name="week_interest" class="form-control form-control-sm week-interest" value="0">' +
        "</div>" +
        '<div class="week-field">' +
        '<label class="form-label">Extra</label>' +
        '<select name="week_extra_type" class="form-select form-select-sm week-extra-type">' +
        '<option value="none">None</option>' +
        '<option value="interest">Extra interest</option>' +
        '<option value="fine">Fine</option>' +
        "</select></div>" +
        '<div class="week-field">' +
        '<label class="form-label">Extra amt</label>' +
        '<input type="number" step="0.01" min="0" name="week_extra_amount" class="form-control form-control-sm week-extra" value="0">' +
        "</div>" +
        statusBlock +
        "</div>";

      body.appendChild(card);

      card.querySelector(".week-date").value =
        dateVal ||
        (hadWeeks && priorDate ? addDaysISO(priorDate, 7) : priorDate || todayISO());

      var interestDefault =
        interestVal != null
          ? interestVal
          : interestInput
            ? parseFloat(interestInput.value) || 0
            : 0;
      card.querySelector(".week-interest").value = Number(interestDefault).toFixed(2);

      wireRow(card);
      renumber();
      card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    // wire existing cards (edit page)
    body.querySelectorAll(".week-card").forEach(wireRow);

    addBtn.addEventListener("click", function () {
      addWeek();
    });

    renumber();
    return { addWeek: addWeek, renumber: renumber };
  }

  return { init: init, todayISO: todayISO, addDaysISO: addDaysISO };
})();
