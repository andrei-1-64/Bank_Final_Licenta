/**
 * Backend API client. The backend uses cookie-based sessions, so every
 * call needs credentials: "include" - this is a separate origin (backend
 * on :8000, this static site on :8080), so the cookie only round-trips if
 * the backend's CORS_ORIGINS lists this origin with credentials allowed
 * (already configured, see backend/.env.example).
 */

const API_BASE_URL = "http://localhost:8000/api/v1";

async function apiFetch(path, options = {}) {
  const res = await fetch(API_BASE_URL + path, {
    credentials: "include",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    let code = null;
    let details = null;
    try {
      const body = await res.json();
      message = body?.error?.message || message;
      code = body?.error?.code || null;
      details = body?.error?.details || null;
    } catch {
      /* non-JSON error body, keep the generic message */
    }
    const error = new Error(message);
    error.status = res.status;
    error.code = code;
    error.details = details;
    throw error;
  }
  return res.status === 204 ? null : res.json();
}

/** Toggles the "flashlight" effect shared by every face-camera UI in the
 * app (step-up confirmation, Face Login enrollment, both login-page face
 * flows): while the camera is running, most of the screen flashes solid
 * white at the device's max display brightness, acting as a passive light
 * source for whoever's face the camera needs to see - the same trick real
 * ID-scanning apps use. Lives here (not app.js) because login.html doesn't
 * load app.js but does load this file.
 *
 * Pass the specific .modal-overlay element when the camera lives inside a
 * modal (e.g. the step-up face-confirm modal) - its own backdrop goes
 * white instead of the shared full-page overlay, so the modal card (its
 * own DOM child) naturally keeps painting on top with no z-index changes
 * needed. Omit it for a camera embedded directly in the page (both
 * login-page flows, the Face Login settings view) - the shared
 * #face-flashlight-overlay element handles those instead. */
function setFaceFlashlight(active, modalOverlayEl) {
  if (modalOverlayEl) {
    modalOverlayEl.classList.toggle("face-flashlight", active);
    return;
  }
  const overlay = document.getElementById("face-flashlight-overlay");
  if (overlay) overlay.classList.toggle("is-active", active);
}

/** Captures a short burst of JPEG frames from `video` (reusing `canvas` for
 * each grab) instead of a single photo - the backend's face liveness check
 * (vision/app/face.py::extract_embedding_with_liveness) requires a genuine
 * blink across the burst before it will return an embedding at all, since a
 * single still photo - including one held up to the camera - can never
 * blink. Every face capture (login, enrollment, step-up confirm, on both
 * login.html and index.html) uses this same helper so the three flows
 * can't drift into three different liveness behaviors.
 *
 * `onFrame(count, total)`, if given, fires after each frame - callers use
 * it to drive a "blink now" progress hint. Resolves to a Blob[]. */
async function captureFaceBurst(video, canvas, { frameCount = 10, intervalMs = 150, onFrame } = {}) {
  const frames = [];
  for (let i = 0; i < frameCount; i++) {
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.85));
    if (blob) frames.push(blob);
    if (onFrame) onFrame(i + 1, frameCount);
    if (i < frameCount - 1) {
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
  }
  return frames;
}

function formatMoney(amountMinor, currency) {
  const major = amountMinor / 100;
  const language = document.documentElement.lang || "ro";
  const amount = major.toLocaleString(language, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const symbols = { EUR: "€", USD: "$" };
  const symbol = symbols[currency];
  return symbol ? `${symbol} ${amount}` : `${amount} ${currency}`;
}

/** Redirects to login if there's no valid session; returns the user otherwise. */
async function requireSession() {
  try {
    return await apiFetch("/users/me");
  } catch (err) {
    if (err.status === 401) {
      window.location.href = "login.html";
      return null;
    }
    throw err;
  }
}

/**
 * Show/hide toggle for every password field on the page. Looks for
 * `.password-toggle-btn[data-target="<input id>"]` (see register.html,
 * login.html, forgot-password.html, index.html's change-password form for
 * the markup) - wired here, once, so any page just needs the markup and
 * gets the behavior automatically, instead of every page re-wiring it.
 */
function wirePasswordToggles() {
  document.querySelectorAll(".password-toggle-btn").forEach((btn) => {
    const input = document.getElementById(btn.dataset.target);
    if (!input) return;
    btn.addEventListener("click", () => {
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      btn.innerHTML = `<i data-lucide="${showing ? "eye" : "eye-off"}"></i>`;
      if (window.lucide) lucide.createIcons();
    });
  });
}

document.addEventListener("DOMContentLoaded", wirePasswordToggles);
