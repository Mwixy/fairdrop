/* FairDrop client */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = {
    peers: $("peers"), noPeers: $("noPeers"), shareUrl: $("shareUrl"),
    displayName: $("displayName"), live: $("live"), picker: $("filePicker"),
    toasts: $("toasts"), themeBtn: $("themeBtn"), themeIcon: $("themeIcon"),
    infoBtn: $("infoBtn"), aboutDialog: $("aboutDialog"), aboutClose: $("aboutClose"),
    aboutUrl: $("aboutUrl"), requestDialog: $("requestDialog"),
    requestLead: $("requestLead"), requestFiles: $("requestFiles"),
    requestSize: $("requestSize"), acceptBtn: $("acceptBtn"), declineBtn: $("declineBtn"),
  };

  const state = {
    me: null,
    peers: [],
    pendingOffers: [],
    currentOffer: null,
    target: null,
    outgoing: new Map(),   // transfer id -> { peerId, total }
    lastFocus: null,
  };

  /* ------------------------------------------------------------- helpers */

  function formatBytes(bytes) {
    if (!bytes) return "0 KB";
    const units = ["bytes", "KB", "MB", "GB", "TB"];
    const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    const value = bytes / Math.pow(1024, i);
    return `${value.toFixed(i === 0 ? 0 : value < 10 ? 1 : 0)} ${units[i]}`;
  }

  function countLabel(n) { return n === 1 ? "1 file" : `${n} files`; }

  function esc(text) {
    const d = document.createElement("div");
    d.textContent = text;
    return d.innerHTML;
  }

  function announce(message) { el.live.textContent = message; }

  function toast(message, action) {
    const node = document.createElement("div");
    node.className = "toast";
    node.innerHTML = `<span class="toast__text">${esc(message)}</span>`;
    if (action) {
      const btn = document.createElement("button");
      btn.className = "toast__btn";
      btn.type = "button";
      btn.textContent = action.label;
      btn.addEventListener("click", action.onClick);
      node.appendChild(btn);
    }
    el.toasts.appendChild(node);
    announce(message);
    setTimeout(() => {
      node.classList.add("leaving");
      setTimeout(() => node.remove(), 260);
    }, action ? 8000 : 4200);
  }

  /* -------------------------------------------------------------- radar */
  /* Concentric circles anchored on the footer logo, the way PairDrop draws
     them: eight rings drifting outward on a two second loop.               */

  const canvas = document.querySelector("canvas.circles");
  const ctx = canvas.getContext("2d");
  const radar = { w: 0, h: 0, x0: 0, y0: 0, dw: 0, frame: 0, speed: 0.5, fps: 60 };
  const baseColor = "168 168 168";
  const baseOpacity = 0.3;

  function initRadar() {
    const footer = document.querySelector("footer");
    const dpr = window.devicePixelRatio || 1;
    radar.w = document.documentElement.clientWidth;
    radar.h = document.documentElement.clientHeight;
    canvas.width = radar.w * dpr;
    canvas.height = radar.h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    radar.x0 = radar.w / 2;
    radar.y0 = radar.h - (footer.offsetHeight - 28);
    radar.dw = Math.round(Math.max(0.6 * radar.w, radar.h) / 10);
    document.documentElement.style.setProperty("--footer-h", `${footer.offsetHeight}px`);
    drawRadar(radar.frame);
  }

  function drawRadar(frame) {
    const { w, h, x0, y0, dw } = radar;
    ctx.clearRect(0, 0, w, h);
    ctx.lineWidth = 2;
    for (let i = 7; i >= 0; i--) {
      const r = dw * i + radar.speed * dw * frame / radar.fps + 33;
      let opacity = Math.max(0, baseOpacity * (1 - 1.2 * r / Math.max(w, h)));
      if (r > dw * 7) opacity *= (8 * dw - r) / dw;
      if (opacity <= 0) continue;
      ctx.strokeStyle = `rgb(${baseColor} / ${opacity})`;
      ctx.beginPath();
      ctx.arc(x0, y0, r, 0, 2 * Math.PI);
      ctx.stroke();
    }
  }

  function animateRadar() {
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (still) { drawRadar(0); return; }
    const start = Date.now();
    const tick = () => {
      const next = Math.trunc(radar.fps * ((Date.now() - start) % (1000 / radar.speed)) / 1000);
      if (next !== radar.frame) { radar.frame = next; drawRadar(next); }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  window.addEventListener("resize", initRadar);

  /* --------------------------------------------------------------- peers */

  function iconFor(kind) {
    return kind === "phone" ? "#phone" : kind === "tablet" ? "#tablet" : "#desktop";
  }

  function renderPeers() {
    const others = state.peers.filter((p) => p.id !== (state.me && state.me.id));
    const existing = new Map([...el.peers.children].map((n) => [n.dataset.id, n]));

    others.forEach((peer) => {
      let node = existing.get(peer.id);
      if (!node) {
        node = document.createElement("button");
        node.type = "button";
        node.className = "peer";
        node.dataset.id = peer.id;
        node.innerHTML =
          `<span class="peer__icon">` +
            `<span class="progress"><span class="circle left"></span>` +
            `<span class="circle right"></span></span>` +
            `<svg class="icon" aria-hidden="true"><use href="${iconFor(peer.kind)}"/></svg>` +
          `</span>` +
          `<span class="highlight-wrapper"><span class="highlight"></span></span>` +
          `<span class="peer__name"></span>` +
          `<span class="peer__sub"></span>`;
        node.addEventListener("click", () => {
          if (node.dataset.cancel) cancelOutgoing(peer.id);
          else pickFilesFor(peer.id);
        });
        wireDropTarget(node, peer.id);
        el.peers.appendChild(node);
      }
      node.querySelector("use").setAttribute("href", iconFor(peer.kind));
      node.querySelector(".peer__name").textContent = peer.name;
      if (!node.dataset.status) node.querySelector(".peer__sub").textContent = peer.device;
      node.setAttribute("aria-label", `Send files to ${peer.name}, ${peer.device}`);
      existing.delete(peer.id);
    });

    existing.forEach((node) => node.remove());
    el.noPeers.hidden = false;   // CSS hides it whenever .peers has children
    initRadar();
  }

  function peerNode(peerId) {
    return el.peers.querySelector(`.peer[data-id="${CSS.escape(peerId)}"]`);
  }

  function setPeerStatus(peerId, status, cancellable) {
    const node = peerNode(peerId);
    if (!node) return;
    const peer = state.peers.find((p) => p.id === peerId);
    const name = peer ? peer.name : "";
    if (status) {
      node.dataset.status = "1";
      node.querySelector(".peer__sub").textContent = status;
      if (cancellable) {
        node.dataset.cancel = "1";
        node.setAttribute("aria-label", `Cancel sending to ${name}`);
        node.title = "Click to cancel";
      } else {
        delete node.dataset.cancel;
      }
    } else {
      delete node.dataset.status;
      delete node.dataset.cancel;
      node.removeAttribute("title");
      node.querySelector(".peer__sub").textContent = peer ? peer.device : "";
      node.setAttribute("aria-label", `Send files to ${name}, ${peer ? peer.device : ""}`);
      setPeerProgress(peerId, 0);
    }
  }

  function cancelOutgoing(peerId) {
    const entry = [...state.outgoing.entries()].find(([, r]) => r.peerId === peerId);
    if (!entry) { setPeerStatus(peerId, null); return; }
    const [tid] = entry;
    state.outgoing.delete(tid);
    setPeerStatus(peerId, null);
    fetch(`/api/transfers/${tid}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: state.me.id }),
    }).catch(() => {});
    toast("Transfer cancelled.");
  }

  function setPeerProgress(peerId, ratio) {
    const node = peerNode(peerId);
    if (!node) return;
    const progress = node.querySelector(".progress");
    const clamped = Math.max(0, Math.min(1, ratio));
    progress.classList.toggle("over50", clamped > 0.5);
    progress.style.setProperty("--progress", `rotate(${clamped * 360}deg)`);
  }

  /* ------------------------------------------------------- drag and drop */

  function wireDropTarget(node, peerId) {
    node.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
      node.dataset.drop = "1";
    });
    node.addEventListener("dragleave", () => delete node.dataset.drop);
    node.addEventListener("drop", (e) => {
      e.preventDefault();
      e.stopPropagation();   // the document handler must not also claim this drop
      delete node.dataset.drop;
      document.body.classList.remove("is-dragging");
      const files = [...(e.dataTransfer.files || [])];
      if (files.length) sendFiles(peerId, files);
    });
  }

  let dragDepth = 0;
  document.addEventListener("dragenter", (e) => {
    if (![...e.dataTransfer.types].includes("Files")) return;
    dragDepth++;
    document.body.classList.add("is-dragging");
  });
  document.addEventListener("dragleave", () => {
    if (--dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("is-dragging"); }
  });
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    document.body.classList.remove("is-dragging");
    if (e.dataTransfer.files.length) toast("Drop the files onto a person to send them.");
  });

  /* ------------------------------------------------------------- sending */

  function pickFilesFor(peerId) {
    state.target = peerId;
    el.picker.value = "";
    el.picker.click();
  }

  el.picker.addEventListener("change", () => {
    const files = [...el.picker.files];
    if (state.target && files.length) sendFiles(state.target, files);
    state.target = null;
    el.picker.value = "";
  });

  async function sendFiles(peerId, files) {
    const peer = state.peers.find((p) => p.id === peerId);
    if (!peer || !state.me) return;
    if (peerNode(peerId) && peerNode(peerId).dataset.status) {
      toast(`Already sending to ${peer.name}.`);
      return;
    }

    setPeerStatus(peerId, "Waiting…", true);
    let transfer;
    try {
      const res = await fetch("/api/transfers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          from: state.me.id,
          to: peerId,
          files: files.map((f) => ({ name: f.name, size: f.size, mime: f.type })),
        }),
      });
      transfer = await res.json();
      if (!res.ok) throw new Error(transfer.error || "Could not start the transfer");
    } catch (err) {
      setPeerStatus(peerId, null);
      toast(err.message || "Could not start the transfer");
      return;
    }

    const total = files.reduce((sum, f) => sum + f.size, 0) || 1;
    state.outgoing.set(transfer.id, { peerId, total });
    announce(`Asking ${peer.name} to accept ${countLabel(files.length)}`);

    let uploaded = 0;
    for (let i = 0; i < files.length; i++) {
      try {
        await uploadFile(transfer.id, i, files[i], (sent) => {
          setPeerProgress(peerId, (uploaded + sent) / total);
        });
      } catch {
        state.outgoing.delete(transfer.id);
        setPeerStatus(peerId, null);
        toast(`Could not send to ${peer.name}.`);
        return;
      }
      uploaded += files[i].size;
      setPeerProgress(peerId, uploaded / total);
    }

    const record = state.outgoing.get(transfer.id);
    if (record) {
      record.uploaded = true;
      if (record.accepted) finishSend(transfer.id, peer.name);
      else setPeerStatus(peerId, "Waiting for accept…", true);
    }
  }

  function uploadFile(tid, index, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", `/api/transfers/${tid}/files/${index}`);
      xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
      xhr.upload.onprogress = (e) => onProgress(e.loaded);
      xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve() : reject(new Error("upload")));
      xhr.onerror = () => reject(new Error("network"));
      xhr.send(file);
    });
  }

  function finishSend(tid, name) {
    const record = state.outgoing.get(tid);
    if (!record) return;
    state.outgoing.delete(tid);
    setPeerProgress(record.peerId, 1);
    setPeerStatus(record.peerId, "Sent");
    setTimeout(() => setPeerStatus(record.peerId, null), 2000);
    toast(`Sent to ${name}.`);
  }

  /* ----------------------------------------------------------- receiving */

  function queueOffer(offer) {
    state.pendingOffers.push(offer);
    if (!state.currentOffer) showNextOffer();
  }

  function showNextOffer() {
    const offer = state.pendingOffers.shift();
    if (!offer) { closeRequest(); return; }
    state.currentOffer = offer;
    el.aboutDialog.hidden = true;   // an incoming request outranks the About sheet
    state.lastFocus = document.activeElement;

    el.requestLead.innerHTML =
      `<span class="name-badge">${esc(offer.from.name)}</span> would like to share ` +
      `${countLabel(offer.files.length)} with you`;
    el.requestFiles.innerHTML = offer.files.slice(0, 12).map((f) =>
      `<li><svg class="icon" aria-hidden="true"><use href="#file"/></svg>` +
      `<span class="nm">${esc(f.name)}</span>` +
      `<span class="sz">${formatBytes(f.size)}</span></li>`).join("") +
      (offer.files.length > 12
        ? `<li><span class="nm">and ${offer.files.length - 12} more…</span></li>` : "");
    el.requestSize.textContent = formatBytes(offer.totalSize);

    el.requestDialog.hidden = false;
    el.acceptBtn.focus();
    announce(`${offer.from.name} wants to send you ${countLabel(offer.files.length)}`);
  }

  function closeRequest() {
    state.currentOffer = null;
    el.requestDialog.hidden = true;
    if (state.lastFocus && document.contains(state.lastFocus)) state.lastFocus.focus();
  }

  async function respondToOffer(action) {
    const offer = state.currentOffer;
    if (!offer) return;
    closeRequest();
    try {
      await fetch(`/api/transfers/${offer.id}/respond`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: state.me.id, action }),
      });
    } catch { /* the server tells the sender either way */ }

    if (action === "accept") receive(offer);
    else announce(`Declined ${offer.from.name}`);
    showNextOffer();
  }

  function receive(offer) {
    const many = offer.files.length > 1;
    const link = many
      ? `/api/transfers/${offer.id}/zip?id=${encodeURIComponent(state.me.id)}`
      : `/api/transfers/${offer.id}/files/0/download?id=${encodeURIComponent(state.me.id)}`;
    download(link);
    toast(`Saving ${many ? countLabel(offer.files.length) : offer.files[0].name} from ${offer.from.name}`,
          { label: "Save again", onClick: () => download(link) });
  }

  function download(href) {
    const a = document.createElement("a");
    a.href = href;
    a.rel = "noopener";
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 1000);
  }

  el.acceptBtn.addEventListener("click", () => respondToOffer("accept"));
  el.declineBtn.addEventListener("click", () => respondToOffer("decline"));

  /* ------------------------------------------------------- name + about */

  function commitName() {
    const name = el.displayName.textContent.trim().replace(/\s+/g, " ").slice(0, 32);
    if (!name || !state.me || name === state.me.name) {
      el.displayName.textContent = state.me ? state.me.name : "";
      return;
    }
    fetch("/api/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: state.me.id, name }),
    }).then((res) => {
      if (!res.ok) throw new Error();
      state.me.name = name;
      localStorage.setItem("fairdrop.name", name);
      el.displayName.textContent = name;
      announce(`You are now known as ${name}`);
    }).catch(() => {
      el.displayName.textContent = state.me.name;
      toast("Could not change your name.");
    });
  }

  el.displayName.addEventListener("blur", commitName);
  el.displayName.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); el.displayName.blur(); }
    if (e.key === "Escape") {
      el.displayName.textContent = state.me ? state.me.name : "";
      el.displayName.blur();
    }
  });
  el.displayName.addEventListener("paste", (e) => {
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData("text");
    document.execCommand("insertText", false, text.replace(/\s+/g, " ").slice(0, 32));
  });

  el.infoBtn.addEventListener("click", () => {
    state.lastFocus = el.infoBtn;
    el.aboutDialog.hidden = false;
    el.aboutClose.focus();
  });
  const closeAbout = () => {
    el.aboutDialog.hidden = true;
    el.infoBtn.focus();
  };
  el.aboutClose.addEventListener("click", closeAbout);
  el.aboutDialog.querySelector(".dialog__bg").addEventListener("click", closeAbout);

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!el.aboutDialog.hidden) closeAbout();
      else if (!el.requestDialog.hidden) respondToOffer("decline");
    } else if (e.key === "Enter" && !el.requestDialog.hidden) {
      respondToOffer("accept");
    }
  });

  /* --------------------------------------------------------------- theme */

  const THEMES = ["auto", "light", "dark"];
  function applyTheme(theme) {
    if (theme === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    el.themeIcon.querySelector("use").setAttribute("href", `#theme-${theme}`);
    el.themeBtn.title = `Theme: ${theme}`;
    el.themeBtn.setAttribute("aria-label", `Theme: ${theme}. Click to change.`);
    localStorage.setItem("fairdrop.theme", theme);
  }
  applyTheme(localStorage.getItem("fairdrop.theme") || "auto");
  el.themeBtn.addEventListener("click", () => {
    const current = localStorage.getItem("fairdrop.theme") || "auto";
    applyTheme(THEMES[(THEMES.indexOf(current) + 1) % THEMES.length]);
  });

  /* -------------------------------------------------------------- stream */

  function connect() {
    let id = sessionStorage.getItem("fairdrop.id");
    if (!id) {
      id = Math.random().toString(36).slice(2, 11) + Date.now().toString(36).slice(-3);
      sessionStorage.setItem("fairdrop.id", id);
    }
    const saved = localStorage.getItem("fairdrop.name") || "";
    const source = new EventSource(
      `/api/events?id=${encodeURIComponent(id)}` +
      (saved ? `&name=${encodeURIComponent(saved)}` : ""));

    source.addEventListener("hello", (e) => {
      state.me = JSON.parse(e.data).you;
      el.displayName.textContent = state.me.name;
    });

    source.addEventListener("peers", (e) => {
      state.peers = JSON.parse(e.data).peers;
      const mine = state.peers.find((p) => p.id === (state.me && state.me.id));
      if (mine) {
        state.me = mine;
        if (document.activeElement !== el.displayName) el.displayName.textContent = mine.name;
      }
      renderPeers();
    });

    source.addEventListener("offer", (e) => queueOffer(JSON.parse(e.data)));

    source.addEventListener("response", (e) => {
      const data = JSON.parse(e.data);
      const record = state.outgoing.get(data.id);
      if (!record) return;
      if (data.status === "accepted") {
        record.accepted = true;
        setPeerStatus(record.peerId, "Transferring…", true);
        if (record.uploaded) finishSend(data.id, data.by);
      } else {
        state.outgoing.delete(data.id);
        setPeerStatus(record.peerId, "Declined");
        setTimeout(() => setPeerStatus(record.peerId, null), 2500);
        toast(`${data.by} declined the files.`);
      }
    });

    source.addEventListener("cancelled", (e) => {
      const { id: tid } = JSON.parse(e.data);
      state.pendingOffers = state.pendingOffers.filter((o) => o.id !== tid);
      if (state.currentOffer && state.currentOffer.id === tid) {
        closeRequest();
        toast("The sender cancelled that transfer.");
        showNextOffer();
      }
    });

    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) setTimeout(connect, 2000);
    };
  }

  el.shareUrl.textContent = window.location.origin;
  el.aboutUrl.textContent = window.location.origin;
  initRadar();
  animateRadar();
  connect();
})();
