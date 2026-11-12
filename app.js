(() => {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) { tg.ready(); tg.expand(); }
  const $ = (id) => document.getElementById(id);
  const state = { user: null, driver: null, pickup: null, watchId: null, locationTimer: null };
  const authHeaders = () => ({ "Content-Type": "application/json", "Authorization": "tma " + (tg ? tg.initData : "") });
  async function api(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "Request failed (" + response.status + ")");
    return data;
  }
  function message(id, text, kind = "") {
    const el = $(id); if (!el) return;
    el.textContent = text; el.className = "message" + (kind ? " " + kind : "");
  }
  function showError(error) {
    $("loading").classList.add("hidden"); $("appContent").classList.add("hidden");
    $("errorPanel").classList.remove("hidden"); $("errorText").textContent = error.message || String(error);
  }
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
  }
  function setView(view) {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    ["ride","activity","driver","admin"].forEach((v) => $(v + "View").classList.toggle("hidden", v !== view));
    if (view === "activity") loadActivity();
    if (view === "driver") loadDriverView();
    if (view === "admin") loadAdmin();
  }
  async function boot() {
    try {
      if (!tg && location.hostname !== "localhost" && location.hostname !== "127.0.0.1") throw new Error("Please open WING FAST from its Telegram bot.");
      const result = await api("/api/me");
      state.user = result.user; state.driver = result.driver;
      $("greeting").textContent = "Hello, " + (state.user.first_name || "there");
      $("roleLabel").textContent = ({ admin:"Administrator", driver:"Approved driver", driver_pending:"Driver application pending", passenger:"Passenger" })[state.user.role] || "WING FAST member";
      if (["driver","driver_pending"].includes(state.user.role) || state.driver) document.querySelector('[data-view="driver"]').classList.remove("hidden");
      if (state.user.role === "admin") document.querySelector('[data-view="admin"]').classList.remove("hidden");
      $("loading").classList.add("hidden"); $("errorPanel").classList.add("hidden"); $("appContent").classList.remove("hidden");
      if (!state.pickup) locate(false);
    } catch (e) { showError(e); }
  }
  function locate(showAlert = true) {
    if (!navigator.geolocation) { if (showAlert) message("bookingMessage", "GPS is not supported by this device.", "error"); return; }
    $("locateBtn").textContent = "WAIT…";
    navigator.geolocation.getCurrentPosition((pos) => {
      state.pickup = { lat: pos.coords.latitude, lon: pos.coords.longitude };
      $("pickupText").value = pos.coords.latitude.toFixed(5) + ", " + pos.coords.longitude.toFixed(5);
      $("locateBtn").textContent = "UPDATED";
      if (showAlert) message("bookingMessage", "Pickup location updated.", "success");
    }, (err) => {
      $("locateBtn").textContent = "LOCATE";
      if (showAlert) message("bookingMessage", err.message || "Location permission was not granted.", "error");
      if (tg && typeof tg.requestLocation === "function") {
        try { tg.requestLocation((location) => {
          if (location && Number.isFinite(location.latitude) && Number.isFinite(location.longitude)) {
            state.pickup = { lat: location.latitude, lon: location.longitude };
            $("pickupText").value = location.latitude.toFixed(5) + ", " + location.longitude.toFixed(5);
          }
        }); } catch (_) {}
      }
    }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 15000 });
  }
  async function bookRide() {
    const destination = $("destination").value.trim();
    if (!state.pickup) { message("bookingMessage", "Please share your pickup location first.", "error"); return; }
    if (destination.length < 2) { message("bookingMessage", "Please enter your destination.", "error"); return; }
    const btn = $("bookBtn"); btn.disabled = true; btn.textContent = "SENDING REQUEST…";
    try {
      const result = await api("/api/bookings", { method:"POST", body: JSON.stringify({
        pickup_lat: state.pickup.lat, pickup_lon: state.pickup.lon, pickup_text: $("pickupText").value,
        destination_text: destination, vehicle_type: $("vehicleType").value, payment_method: $("payment").value
      })});
      message("bookingMessage", "Ride request created: " + result.booking_id, "success");
      $("destination").value = "";
    } catch (e) { message("bookingMessage", e.message, "error"); }
    finally { btn.disabled = false; btn.innerHTML = 'FIND A DRIVER <span>→</span>'; }
  }
  async function loadActivity() {
    const box = $("activityList"); box.innerHTML = '<div class="empty-state">Loading rides…</div>';
    try {
      const data = await api("/api/bookings/mine");
      box.innerHTML = data.items.length ? data.items.map((b) =>
        '<article class="ride-card"><strong>' + escapeHtml(b.destination_text) + '</strong><small>Pickup: ' + escapeHtml(b.pickup_text || (b.pickup_lat + ", " + b.pickup_lon)) + '</small><small>Status: ' + escapeHtml(b.status.toUpperCase()) + ' · ' + new Date(b.created_at * 1000).toLocaleString() + '</small>' +
        (["searching","accepted","arrived","in_progress"].includes(b.status) ? '<div class="card-actions"><button class="mini-btn" data-cancel="' + escapeHtml(b.id) + '">CANCEL RIDE</button></div>' : '') + '</article>'
      ).join("") : '<div class="empty-state">No rides yet.</div>';
    } catch (e) { box.innerHTML = '<div class="empty-state">' + escapeHtml(e.message) + '</div>'; }
  }
  async function cancelRide(id) {
    try { await api("/api/bookings/" + encodeURIComponent(id) + "/cancel", { method:"POST" }); loadActivity(); }
    catch (e) { alert(e.message); }
  }
  async function loadDriverView() {
    const status = state.driver ? state.driver.status : "not_registered";
    $("driverStatus").textContent = status.replace("_"," ").toUpperCase();
    $("driverApplyPanel").classList.toggle("hidden", status === "approved");
    $("driverActivePanel").classList.toggle("hidden", status !== "approved");
    if (status === "approved") loadRequests();
    else if (status === "pending") message("driverMessage", "Your application is waiting for admin approval.");
  }
  async function applyDriver() {
    const payload = { phone:$("driverPhone").value.trim(), vehicle_type:$("driverVehicle").value,
      vehicle_model:$("driverModel").value.trim(), plate_number:$("driverPlate").value.trim() };
    try {
      await api("/api/driver/apply", { method:"POST", body:JSON.stringify(payload) });
      message("driverMessage", "Application submitted. Please wait for admin approval.", "success");
      boot();
    } catch (e) { message("driverMessage", e.message, "error"); }
  }
  async function sendLocation(pos) {
    try { await api("/api/driver/location", { method:"POST", body:JSON.stringify({
      lat:pos.coords.latitude, lon:pos.coords.longitude, accuracy:pos.coords.accuracy
    })}); } catch (e) { message("driverMessage", e.message, "error"); stopLocation(); }
  }
  function stopLocation() {
    if (state.watchId !== null) navigator.geolocation.clearWatch(state.watchId);
    state.watchId = null; $("shareLocationBtn").textContent = "GO ONLINE";
  }
  function startLocation() {
    if (!navigator.geolocation) { alert("GPS is not available."); return; }
    if (state.watchId !== null) { stopLocation(); return; }
    $("shareLocationBtn").textContent = "CONNECTING…";
    state.watchId = navigator.geolocation.watchPosition(sendLocation, (err) => {
      stopLocation(); alert("Location error: " + err.message);
    }, { enableHighAccuracy:true, maximumAge:5000, timeout:15000 });
    $("shareLocationBtn").textContent = "GO OFFLINE";
  }
  async function loadRequests() {
    const box = $("requestList"); box.innerHTML = '<div class="empty-state">Loading requests…</div>';
    try {
      const data = await api("/api/driver/requests");
      box.innerHTML = data.items.length ? data.items.map((b) =>
        '<article class="ride-card"><strong>' + escapeHtml(b.destination_text) + '</strong><small>Passenger: ' + escapeHtml(b.passenger_name) + '</small><small>Pickup: ' + Number(b.pickup_lat).toFixed(4) + ', ' + Number(b.pickup_lon).toFixed(4) + '</small><div class="card-actions"><button class="mini-btn good" data-accept="' + escapeHtml(b.id) + '">ACCEPT RIDE</button></div></article>'
      ).join("") : '<div class="empty-state">No ride requests right now.</div>';
    } catch (e) { box.innerHTML = '<div class="empty-state">' + escapeHtml(e.message) + '</div>'; }
  }
  async function acceptRide(id) {
    try { await api("/api/driver/bookings/" + encodeURIComponent(id) + "/accept", {method:"POST"}); loadRequests(); }
    catch (e) { alert(e.message); }
  }
  async function loadAdmin() {
    try {
      const [stats, drivers, bookings] = await Promise.all([
        api("/api/admin/overview"), api("/api/admin/drivers"), api("/api/admin/bookings")
      ]);
      $("adminStats").innerHTML = [["USERS",stats.users],["PENDING DRIVERS",stats.pending_drivers],["ACTIVE DRIVERS",stats.active_drivers],["ACTIVE RIDES",stats.active_bookings]]
        .map(([label,value]) => '<div class="stat"><b>' + value + '</b><span>' + label + '</span></div>').join("");
      $("adminDrivers").innerHTML = drivers.items.length ? drivers.items.map((d) =>
        '<article class="admin-card"><strong>' + escapeHtml(d.first_name || d.telegram_id) + ' · ' + escapeHtml(d.plate_number) + '</strong><small>' + escapeHtml(d.vehicle_model) + ' / ' + escapeHtml(d.phone) + ' · ' + escapeHtml(d.status) + '</small>' +
        '<div class="card-actions">' + (d.status === "pending" ? '<button class="mini-btn good" data-approve="' + escapeHtml(d.telegram_id) + '">APPROVE</button><button class="mini-btn" data-reject="' + escapeHtml(d.telegram_id) + '">REJECT</button>' : '') +
        (d.status !== "blocked" ? '<button class="mini-btn" data-block="' + escapeHtml(d.telegram_id) + '">BLOCK DRIVER</button>' : '<span class="muted">BLOCKED</span>') + '</div></article>'
      ).join("") : '<div class="empty-state">No driver applications yet.</div>';
      $("adminBookings").innerHTML = bookings.items.slice(0,25).map((b) =>
        '<article class="ride-card"><strong>' + escapeHtml(b.destination_text) + '</strong><small>' + escapeHtml(b.status) + ' · Passenger ' + escapeHtml(b.passenger_id) + '</small></article>'
      ).join("") || '<div class="empty-state">No bookings yet.</div>';
    } catch (e) { $("adminStats").innerHTML = '<div class="empty-state">' + escapeHtml(e.message) + '</div>'; }
  }
  async function adminAction(path) {
    try { await api(path, {method:"POST"}); loadAdmin(); }
    catch (e) { alert(e.message); }
  }
  document.addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) setView(tab.dataset.view);
    const cancel = event.target.closest("[data-cancel]"); if (cancel) cancelRide(cancel.dataset.cancel);
    const accept = event.target.closest("[data-accept]"); if (accept) acceptRide(accept.dataset.accept);
    const approve = event.target.closest("[data-approve]"); if (approve) adminAction("/api/admin/drivers/" + encodeURIComponent(approve.dataset.approve) + "/approve");
    const reject = event.target.closest("[data-reject]"); if (reject) adminAction("/api/admin/drivers/" + encodeURIComponent(reject.dataset.reject) + "/reject");
    const block = event.target.closest("[data-block]"); if (block && confirm("Block this driver and account?")) adminAction("/api/admin/drivers/" + encodeURIComponent(block.dataset.block) + "/block");
  });
  $("locateBtn").addEventListener("click", () => locate(true));
  $("bookBtn").addEventListener("click", bookRide);
  $("applyDriverBtn").addEventListener("click", applyDriver);
  $("shareLocationBtn").addEventListener("click", startLocation);
  $("loadActivity").addEventListener("click", loadActivity);
  $("loadRequests").addEventListener("click", loadRequests);
  $("loadAdmin").addEventListener("click", loadAdmin);
  $("refreshBtn").addEventListener("click", boot);
  $("retryBtn").addEventListener("click", boot);
  boot();
})();