// Mail admin dashboard - small progressive enhancements, no dependencies.
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  var MARK = { ok: "✓", warn: "!", error: "✗" };

  // Confirm dangerous actions.
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Buttons that need confirming (a form with several buttons).
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-confirm-click]");
    if (b && !window.confirm(b.getAttribute("data-confirm-click"))) e.preventDefault();
  });

  // Selects that apply immediately.
  document.querySelectorAll("select[data-autosubmit]").forEach(function (sel) {
    sel.addEventListener("change", function () { sel.form.submit(); });
  });

  // Click to copy.
  document.addEventListener("click", function (e) {
    var el = e.target.closest(".copy");
    if (!el || !navigator.clipboard) return;
    navigator.clipboard.writeText(el.textContent.trim()).then(function () {
      el.classList.add("copied");
      setTimeout(function () { el.classList.remove("copied"); }, 900);
    });
  });

  // Mobile menu.
  document.querySelectorAll("[data-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      document.getElementById(b.getAttribute("data-toggle")).classList.toggle("open");
    });
  });

  // Close other "Manage" menus when one opens.
  document.addEventListener("toggle", function (e) {
    if (e.target.classList && e.target.classList.contains("menu-d") && e.target.open) {
      document.querySelectorAll(".menu-d[open]").forEach(function (d) { if (d !== e.target) d.open = false; });
    }
  }, true);

  // "Generate password" disables the password field.
  document.querySelectorAll("[data-gen]").forEach(function (cb) {
    cb.addEventListener("change", function () {
      var pw = cb.form.querySelector("[data-pw]");
      if (pw) { pw.disabled = cb.checked; pw.required = !cb.checked && pw.form.action.indexOf("/add") > -1; }
    });
  });

  // Table filter.
  document.querySelectorAll("[data-filter]").forEach(function (input) {
    var table = document.querySelector(input.getAttribute("data-filter"));
    input.addEventListener("input", function () {
      var q = input.value.toLowerCase();
      table.querySelectorAll("tbody tr").forEach(function (tr) {
        tr.hidden = q && tr.textContent.toLowerCase().indexOf(q) === -1;
      });
    });
  });

  // Live DNS status on the domain page.
  var dns = document.getElementById("dns");
  if (dns) {
    fetch(dns.getAttribute("data-src"), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (rows) {
        rows.forEach(function (r) {
          var tr = dns.querySelector('tr[data-key="' + (r.type + " " + r.name).replace(/"/g, "") + '"]');
          if (!tr) return;
          var label = r.note === "DNS lookup failed" ? "lookup failed" : { ok: "OK", warn: r.required ? "check" : "not set", error: "missing" }[r.status];
          tr.querySelector(".st").innerHTML = '<span class="badge ' + r.status + '">' + MARK[r.status] + " " + label + "</span>";
          if (r.note) tr.querySelector(".note").textContent = r.note;
          tr.classList.add(r.status);
        });
        dns.querySelectorAll(".st .badge:not(.ok):not(.warn):not(.error)").forEach(function (b) { b.textContent = "–"; });
      })
      .catch(function () { dns.querySelectorAll(".st").forEach(function (td) { td.textContent = "?"; }); });
  }

  // Health page.
  var health = document.getElementById("health");
  function loadHealth(fresh) {
    health.innerHTML = '<div class="card loading">Running checks… this takes up to 30 seconds.</div>';
    fetch(health.getAttribute("data-src") + (fresh ? "?fresh=1" : ""), { credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (rows) {
        var groups = {}, order = [], count = { ok: 0, warn: 0, error: 0 };
        rows.forEach(function (r) {
          if (!groups[r.group]) { groups[r.group] = []; order.push(r.group); }
          groups[r.group].push(r);
          count[r.status]++;
        });
        var html = '<div class="summary"><span class="badge ok">' + count.ok + ' OK</span>' +
          '<span class="badge warn">' + count.warn + ' warnings</span>' +
          '<span class="badge error">' + count.error + ' problems</span></div>';
        order.forEach(function (g) {
          var worst = groups[g].some(function (r) { return r.status === "error"; }) ? "error" :
            (groups[g].some(function (r) { return r.status === "warn"; }) ? "warn" : "ok");
          html += '<section class="card hgroup"><h2><span class="dot ' + worst + '"></span>' + esc(g) + '</h2><ul class="hlist">';
          groups[g].forEach(function (r) {
            html += '<li><span class="mark ' + r.status + '">' + MARK[r.status] + "</span><span>" + esc(r.name) +
              "</span><span>" + esc(r.detail) + "</span></li>";
          });
          html += "</ul></section>";
        });
        health.innerHTML = html;
      })
      .catch(function (e) { health.innerHTML = '<div class="flash error">Health check failed (' + esc(e.message) + ").</div>"; });
  }
  if (health) {
    loadHealth(false);
    document.querySelectorAll("[data-health-refresh]").forEach(function (b) {
      b.addEventListener("click", function () { loadHealth(true); });
    });
  }
})();
