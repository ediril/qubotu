// Live updates: the server pushes "refresh" events naming topics, and every element
// whose data-refresh lists one of those topics reloads itself through htmx.

(function () {
  function refresh(topics) {
    document.querySelectorAll("[data-refresh]").forEach(function (el) {
      var mine = el.dataset.refresh.split(/\s+/);
      if (topics.some(function (t) { return mine.indexOf(t) !== -1; })) {
        htmx.trigger(el, "refresh");
      }
    });
    // Streamed bubbles whose message has completed are replaced by the stored message.
    topics.forEach(function (t) {
      if (t.indexOf("messages:") === 0) {
        var stream = document.querySelector('[data-stream="' + t.slice(9) + '"]');
        if (stream) stream.querySelectorAll(".done").forEach(function (b) { b.remove(); });
      }
    });
  }

  function onDelta(ev) {
    var stream = document.querySelector('[data-stream="' + ev.channel + '"]');
    if (!stream) return;
    var id = "stream-" + ev.item;
    var bubble = document.getElementById(id);
    if (!bubble) {
      bubble = document.createElement("div");
      bubble.id = id;
      bubble.className = "msg msg-agent streaming";
      var author = document.createElement("div");
      author.className = "msg-author";
      author.textContent = ev.agent;
      var body = document.createElement("div");
      body.className = "bubble";
      bubble.appendChild(author);
      bubble.appendChild(body);
      stream.appendChild(bubble);
    }
    bubble.querySelector(".bubble").textContent += ev.delta;
    stickToBottom(true);
  }

  function connect() {
    var es = new EventSource("/events");
    es.onmessage = function (e) {
      var ev = JSON.parse(e.data);
      if (ev.type === "refresh") refresh(ev.topics);
      else if (ev.type === "delta") onDelta(ev);
      else if (ev.type === "item_done") {
        var b = document.getElementById("stream-" + ev.item);
        if (b) b.classList.add("done");
      }
    };
    es.onerror = function () {
      // The browser reconnects on its own; catch up on anything missed once it does.
      es.onopen = function () { refresh(["agents", "status", "outputs", "requests", "memory"].concat(
        Array.from(document.querySelectorAll("[data-refresh]")).flatMap(function (el) { return el.dataset.refresh.split(/\s+/); })
      )); };
    };
  }

  // Chat scrolling: follow new messages only if the user is already near the bottom.
  var pinned = true;
  function stickToBottom(onlyIfPinned) {
    var s = document.getElementById("chat-scroll");
    if (!s) return;
    if (!onlyIfPinned || pinned) s.scrollTop = s.scrollHeight;
  }
  document.addEventListener("scroll", function (e) {
    if (e.target.id === "chat-scroll") {
      var s = e.target;
      pinned = s.scrollHeight - s.scrollTop - s.clientHeight < 80;
    }
  }, true);
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target.closest && e.target.closest("#chat-scroll")) stickToBottom(true);
    highlightNav();
  });

  // Forms with data-confirm ask first. The text lives in an attribute, never in inline script.
  document.addEventListener("submit", function (e) {
    var msg = e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);

  // Composer: Enter sends, Shift+Enter adds a line. Clear after sending.
  document.addEventListener("keydown", function (e) {
    var ta = e.target;
    if (ta.tagName === "TEXTAREA" && ta.closest("[data-composer]") && e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (ta.value.trim()) htmx.trigger(ta.form, "submit");
    }
  });
  // Multi-line forms: Enter adds a line, Cmd/Ctrl+Enter submits.
  document.addEventListener("keydown", function (e) {
    var ta = e.target;
    if (ta.tagName === "TEXTAREA" && ta.hasAttribute("data-mod-enter") && e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      if (ta.value.trim()) ta.form.requestSubmit();
    }
  });
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var form = e.detail.elt;
    if (form.matches && form.matches("[data-composer]") && e.detail.successful) {
      form.reset();
      pinned = true;
      form.querySelector("textarea").focus();
    }
  });

  // Agent form: switch model suggestions with the provider.
  function syncProvider() {
    var sel = document.querySelector("[data-provider]");
    var input = document.querySelector("[data-model]");
    var hint = document.querySelector("[data-model-hint]");
    if (!sel || !input) return;
    var fw = sel.value === "fireworks";
    input.setAttribute("list", fw ? "models-fireworks" : "models-openai");
    input.placeholder = fw ? "accounts/fireworks/models/…" : "Default";
    if (hint) hint.textContent = fw ? "A Fireworks model ID. Suggestions are examples; check Fireworks for current models." : "Leave empty to use your Codex default.";
  }
  document.addEventListener("change", function (e) { if (e.target.matches("[data-provider]")) syncProvider(); });

  function highlightNav() {
    var holder = document.querySelector("[data-nav]");
    if (!holder) return;
    var nav = holder.dataset.nav;
    holder.querySelectorAll("[data-agent-nav]").forEach(function (a) {
      a.classList.toggle("active", a.dataset.agentNav === nav);
    });
  }

  syncProvider();
  highlightNav();
  stickToBottom(false);
  connect();
})();
