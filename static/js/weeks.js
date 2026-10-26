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
      var rows = body.querySelectorAll("tr.week-new");
      rows.forEach(function (row, i) {
        var num = row.querySelector(".week-num");
        if (num) num.textContent = startIndex + i + 1;
      });
      if (hint) {
        var any =
          body.querySelectorAll("tr.week-existing").length +
            body.querySelectorAll("tr.week-new").length >
          0;
        hint.style.display = any ? "none" : "block";
      }
    }

    function recalcRow(row) {
      var interest = parseFloat(row.querySelector(".week-interest").value) || 0;
      var extra = parseFloat(row.querySelector(".week-extra").value) || 0;
      var totalEl = row.querySelector(".week-total");
      if (totalEl) totalEl.textContent = (interest + extra).toFixed(2);
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
      var hadWeeks = body.querySelectorAll("tr.week-existing, tr.week-new").length > 0;

      var tr = document.createElement("tr");
      tr.className = "week-new";
      var statusCell = withStatus
        ? '<td><select name="week_status" class="form-select form-select-sm week-status">' +
          '<option value="not_paid" selected>Not paid</option>' +
          '<option value="paid">Paid</option>' +
          '<option value="maaf">Maaf</option>' +
          "</select></td>"
        : "";
      tr.innerHTML =
        '<td class="week-num"></td>' +
        '<td><input type="date" name="week_date" class="form-control form-control-sm week-date" required></td>' +
        '<td><input type="number" step="0.01" min="0" name="week_interest" class="form-control form-control-sm week-interest" value="0"></td>' +
        '<td><select name="week_extra_type" class="form-select form-select-sm week-extra-type">' +
        '<option value="none">None</option>' +
        '<option value="interest">Extra interest</option>' +
        '<option value="fine">Fine</option>' +
        "</select></td>" +
        '<td><input type="number" step="0.01" min="0" name="week_extra_amount" class="form-control form-control-sm week-extra" value="0"></td>' +
        '<td class="week-total fw-semibold">0.00</td>' +
        statusCell +
        '<td><button type="button" class="btn btn-sm btn-outline-danger remove-week">Remove</button></td>';

      body.appendChild(tr);

      tr.querySelector(".week-date").value =
        dateVal ||
        (hadWeeks && priorDate ? addDaysISO(priorDate, 7) : priorDate || todayISO());

      var interestDefault =
        interestVal != null
          ? interestVal
          : interestInput
            ? parseFloat(interestInput.value) || 0
            : 0;
      tr.querySelector(".week-interest").value = Number(interestDefault).toFixed(2);

      tr.querySelector(".week-interest").addEventListener("input", function () {
        recalcRow(tr);
      });
      tr.querySelector(".week-extra").addEventListener("input", function () {
        recalcRow(tr);
      });
      tr.querySelector(".week-extra-type").addEventListener("change", function () {
        if (this.value === "none") tr.querySelector(".week-extra").value = "0";
        recalcRow(tr);
      });
      tr.querySelector(".remove-week").addEventListener("click", function () {
        tr.remove();
        renumber();
      });

      recalcRow(tr);
      renumber();
    }

    addBtn.addEventListener("click", function () {
      addWeek();
    });

    renumber();
    return { addWeek: addWeek, renumber: renumber };
  }

  return { init: init, todayISO: todayISO, addDaysISO: addDaysISO };
})();
