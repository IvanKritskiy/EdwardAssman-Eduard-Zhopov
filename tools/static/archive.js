// Случайный документ и раскрытие спойлеров.
(function () {
  var btn = document.querySelector("[data-random]");
  if (btn) {
    var links = [];
    try { links = JSON.parse(btn.getAttribute("data-random")); } catch (e) { links = []; }
    btn.addEventListener("click", function () {
      if (links.length) location.href = links[Math.floor(Math.random() * links.length)];
    });
  }
  var cur = document.querySelector(".site-nav [aria-current]");
  var nav = cur && cur.parentElement;
  if (nav && nav.scrollWidth > nav.clientWidth) {
    nav.scrollLeft = cur.offsetLeft - (nav.clientWidth - cur.offsetWidth) / 2;
  }
  document.addEventListener("click", function (e) {
    var s = e.target.closest && e.target.closest(".spoiler");
    if (s) s.style.color = "inherit";
  });
})();
