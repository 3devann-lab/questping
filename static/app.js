(function () {
  var calm = window.matchMedia("(prefers-reduced-motion: reduce)");

  document.addEventListener("pointerdown", function (event) {
    var btn = event.target.closest(".btn");
    if (!btn || calm.matches) return;
    var box = btn.getBoundingClientRect();
    var x = event.clientX - box.left;
    var y = event.clientY - box.top;
    var reach = Math.max(
      Math.hypot(x, y),
      Math.hypot(box.width - x, y),
      Math.hypot(x, box.height - y),
      Math.hypot(box.width - x, box.height - y)
    );
    var wave = document.createElement("span");
    wave.className = "ripple";
    wave.style.width = wave.style.height = reach * 2 + "px";
    wave.style.left = x - reach + "px";
    wave.style.top = y - reach + "px";
    btn.appendChild(wave);
    wave.addEventListener("animationend", function () {
      wave.remove();
    });
  });

  document.addEventListener("click", function (event) {
    var el = event.target.closest(".btn");
    if (!el || calm.matches || event.defaultPrevented) return;
    if (el.dataset.going) {
      event.preventDefault();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    var form = el.tagName === "BUTTON" ? el.form : null;
    var href = el.tagName === "A" ? el.href : null;
    if (!form && !href) return;
    if (form && !form.checkValidity()) return;
    event.preventDefault();
    el.dataset.going = "1";
    setTimeout(function () {
      if (form) {
        form.requestSubmit(el);
      } else {
        window.location.href = href;
      }
    }, 260);
  });
})();
