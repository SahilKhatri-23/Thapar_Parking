/* Shared helpers for the Thapar Smart Parking web portal (session auth + CSRF). */
const TSP = (() => {
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  async function api(path, opts = {}) {
    const o = { method: opts.method || "GET", headers: { "X-CSRFToken": csrf() }, credentials: "same-origin" };
    if (opts.form) o.body = opts.form;
    else if (opts.body !== undefined) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(opts.body); }
    const r = await fetch("/api/" + path.replace(/^\//, ""), o);
    let data = null;
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) { const msg = (data && (data.detail || JSON.stringify(data))) || r.statusText; throw new Error(msg); }
    return data;
  }
  const post = (p, body) => api(p, { method: "POST", body });
  const patch = (p, body) => api(p, { method: "PATCH", body });

  function toast(msg, kind = "") {
    let w = document.querySelector(".toast-wrap");
    if (!w) { w = document.createElement("div"); w.className = "toast-wrap"; document.body.appendChild(w); }
    const t = document.createElement("div"); t.className = "toast " + kind; t.textContent = msg; w.appendChild(t);
    setTimeout(() => t.remove(), 4200);
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function prettyPlate(p) {
    p = (p || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    let m = p.match(/^([A-Z]{2})(\d{1,2})([A-Z]{0,3})(\d{1,4})$/);
    if (m) return m.slice(1).filter(Boolean).join(" ");
    m = p.match(/^(\d{2})(BH)(\d{4})([A-Z]{1,2})$/);
    return m ? m.slice(1).join(" ") : p;
  }
  const plate = (p, xl) => p ? `<span class="plate${xl ? " xl" : ""}"><i>IND</i><span>${esc(prettyPlate(p))}</span></span>`
                              : `<span class="plate unk${xl ? " xl" : ""}"><i>IND</i><span>? ? ?</span></span>`;
  const LABEL = { authorized: "Authorized", unauthorized: "Unauthorized", expired: "Expired", needs_review: "Needs review" };
  const badge = (s, label) => `<span class="badge b-${esc(s)}"><span class="dot"></span>${esc(label || LABEL[s] || (s || "").replace(/_/g, " "))}</span>`;
  const fmtTime = (d) => d ? new Date(d).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
  const fmtDT = (d) => d ? new Date(d).toLocaleString([], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
  const ago = (d) => {
    if (!d) return "never"; const s = (Date.now() - new Date(d)) / 1000;
    if (s < 60) return Math.round(s) + "s ago"; if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago"; return Math.round(s / 86400) + "d ago";
  };
  function modal(html) {
    let bg = document.getElementById("modal-bg");
    if (!bg) { bg = document.createElement("div"); bg.id = "modal-bg"; bg.className = "modal-bg"; document.body.appendChild(bg);
      bg.addEventListener("click", (e) => { if (e.target === bg) closeModal(); }); }
    bg.innerHTML = `<div class="modal">${html}</div>`; bg.classList.add("show"); return bg;
  }
  const closeModal = () => document.getElementById("modal-bg")?.classList.remove("show");
  function beep(freq = 880, ms = 180) {
    try { const c = new (window.AudioContext || window.webkitAudioContext)(); const o = c.createOscillator(); const g = c.createGain();
      o.frequency.value = freq; o.connect(g); g.connect(c.destination); g.gain.value = .08; o.start(); setTimeout(() => { o.stop(); c.close(); }, ms);
    } catch (e) {}
  }
  /* Webcam helper: start(videoEl, deviceId) / snap() -> Blob */
  function Cam(video) {
    let stream = null;
    return {
      async devices() { const d = await navigator.mediaDevices.enumerateDevices(); return d.filter((x) => x.kind === "videoinput"); },
      async start(deviceId) {
        this.stop();
        stream = await navigator.mediaDevices.getUserMedia({ video: deviceId ? { deviceId: { exact: deviceId }, width: { ideal: 1920 } }
                                                                           : { width: { ideal: 1920 }, facingMode: "environment" }, audio: false });
        video.srcObject = stream; await video.play(); return true;
      },
      stop() { if (stream) stream.getTracks().forEach((t) => t.stop()); stream = null; video.srcObject = null; },
      get on() { return !!stream; },
      snap(q = 0.88) {
        const c = document.createElement("canvas"); c.width = video.videoWidth; c.height = video.videoHeight;
        c.getContext("2d").drawImage(video, 0, 0); return new Promise((res) => c.toBlob(res, "image/jpeg", q));
      },
    };
  }
  async function upload(path, blob, extra = {}) {
    const f = new FormData(); f.append("image", blob, "frame.jpg");
    Object.entries(extra).forEach(([k, v]) => f.append(k, v));
    return api(path, { method: "POST", form: f });
  }
  return { api, post, patch, toast, esc, plate, prettyPlate, badge, fmtTime, fmtDT, ago, modal, closeModal, beep, Cam, upload, LABEL };
})();
