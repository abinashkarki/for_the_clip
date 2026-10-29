/* For the Clip: scroll progress fallback.
   The head script adds .motion (motion allowed) plus .native (CSS scroll timelines drive --p)
   or .fallback (this file drives --p). Nothing here runs a loop when the page is idle. */
(function () {
  var d = document.documentElement;
  var mq = window.matchMedia ? matchMedia('(prefers-reduced-motion: reduce)') : null;

  function onMotionPref() {
    // Reduced motion switched on mid-visit: drop to the static layout. Switched off: reload into motion.
    if (mq.matches) {
      d.classList.remove('motion', 'native', 'fallback');
      clear();
    } else if (!d.classList.contains('motion')) {
      location.reload();
    }
  }
  if (mq) {
    if (mq.addEventListener) mq.addEventListener('change', onMotionPref);
    else if (mq.addListener) mq.addListener(onMotionPref);
  }

  var els = [].slice.call(document.querySelectorAll('[data-drive]'));
  function clear() { els.forEach(function (el) { el.style.removeProperty('--p'); }); }

  if (!d.classList.contains('fallback')) return;

  var specs = els.map(function (el) {
    var parts = (el.getAttribute('data-drive') || 'contain').split(':');
    return { el: el, mode: parts[0], a: parseFloat(parts[1] || 0), b: parseFloat(parts[2] || 1), last: -1 };
  });
  var queued = false;

  function clamp(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

  function frame() {
    queued = false;
    if (!d.classList.contains('fallback')) return;
    var vh = window.innerHeight;
    for (var i = 0; i < specs.length; i++) {
      var s = specs[i];
      var r = s.el.getBoundingClientRect();
      if (r.bottom < -vh || r.top > vh * 2) continue; // far offscreen: leave as is
      var p;
      if (s.mode === 'contain') {
        var span = r.height - vh;
        p = span > 0 ? clamp(-r.top / span) : 1;
      } else {
        var c = (vh - r.top) / (vh + r.height); // cover progress 0..1
        p = clamp((c - s.a) / (s.b - s.a));
      }
      p = Math.round(p * 2000) / 2000;
      if (p !== s.last) { s.last = p; s.el.style.setProperty('--p', p); }
    }
  }
  function queue() { if (!queued) { queued = true; requestAnimationFrame(frame); } }

  window.addEventListener('scroll', queue, { passive: true });
  window.addEventListener('resize', queue, { passive: true });
  window.addEventListener('orientationchange', queue, { passive: true });
  frame();
})();
