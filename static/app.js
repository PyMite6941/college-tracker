// College Tracker client helpers. No inline scripts (the CSP forbids them).
(function () {
  // Confirm before destructive submits: <form data-confirm="Delete X?">
  document.addEventListener("submit", function (e) {
    var btn = e.submitter;
    var msg = (btn && btn.getAttribute("data-confirm")) || (e.target.getAttribute && e.target.getAttribute("data-confirm"));
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // <select data-autosubmit> submits its form on change.
  document.addEventListener("change", function (e) {
    if (e.target.hasAttribute && e.target.hasAttribute("data-autosubmit") && e.target.form) e.target.form.requestSubmit();
  });

  // Poll a running background job and reload when it finishes.
  var banner = document.getElementById("job-banner");
  if (banner && banner.dataset.running === "true") {
    var msg = document.getElementById("job-message");
    var timer = setInterval(function () {
      fetch("/jobs/status", { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (msg && s.message) msg.textContent = s.message;
          if (!s.running) { clearInterval(timer); window.location.reload(); }
        })
        .catch(function () {});
    }, 1500);
  }

  // Chrome profile detection on the Sites page.
  var detect = document.getElementById("detect-profiles");
  if (detect) {
    detect.addEventListener("click", function () {
      var status = document.getElementById("profile_status");
      var select = document.getElementById("profile_select");
      status.textContent = "Detecting…";
      fetch("/chrome-profiles").then(function (r) { return r.json(); }).then(function (data) {
        select.innerHTML = "";
        var first = document.createElement("option");
        first.value = ""; first.textContent = "-- select a profile --";
        select.appendChild(first);
        (data.profiles || []).forEach(function (p) {
          var opt = document.createElement("option");
          opt.value = p.dir_name;
          opt.textContent = p.name + (p.is_default ? " (default)" : "") + (p.user_name ? " — " + p.user_name : "");
          if (p.dir_name === data.current_profile) opt.selected = true;
          select.appendChild(opt);
        });
        if (data.user_data_dir) document.getElementById("user_data_dir").value = data.user_data_dir;
        var n = (data.profiles || []).length;
        status.textContent = n ? ("Found " + n + " profile" + (n === 1 ? "" : "s") + ".") :
          "No Chrome profiles found. Enter the User Data directory manually.";
        if (data.chrome_running) status.textContent += " Chrome is running — close it before checks use this profile.";
      }).catch(function (e) { status.textContent = "Detection failed: " + e.message; });
    });
  }

  // Show/hide credential fields depending on the sign-in mode.
  document.querySelectorAll("select[data-auth-mode]").forEach(function (sel) {
    var form = sel.form;
    function sync() {
      form.querySelectorAll("[data-auth-only]").forEach(function (el) {
        var modes = el.getAttribute("data-auth-only").split(" ");
        el.hidden = modes.indexOf(sel.value) === -1;
      });
    }
    sel.addEventListener("change", sync);
    sync();
  });
})();
