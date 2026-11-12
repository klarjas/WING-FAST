(() => {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    tg.ready();
    try {
      tg.setBackgroundColor("#F5F7FA");
      tg.setHeaderColor("#F5F7FA");
      if (typeof tg.setBottomBarColor === "function") tg.setBottomBarColor("#0D1718");
      // Keep the normal Telegram Mini App window; do not force fullscreen.
      if (typeof tg.expand === "function") tg.expand();
    } catch (error) {
      console.warn("Telegram fullscreen mode is unavailable:", error);
    }
  }
  const $ = (id) => document.getElementById(id);
  const state = { user:null,driver:null,pickup:null,destination:null,map:null,driverMarkers:new Map(),pickupMarker:null,destinationMarker:null,routeLine:null,selectedRouteLine:null,mapMode:"pickup",pickerMode:"pickup",mapCandidate:null,distanceMode:"road",distanceKm:null,liveTimer:null,watchId:null,locationTimer:null,locationPermission:"unknown",locationAsked:false };
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
    document.querySelectorAll(".bottom-nav .tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    ["ride","activity","profile","driver","admin"].forEach((v) => { const panel = $(v + "View"); if (panel) panel.classList.toggle("hidden", v !== view); });
    if (view === "activity") loadActivity();
    if (view === "driver") loadDriverView();
    if (view === "admin") loadAdmin();
  }

  function mapIcon(type){const h=type==="driver"?'<div class="driver-marker">🚕</div>':type==="pickup"?'<div class="pickup-marker"></div>':'<div class="destination-marker"></div>';return L.divIcon({html:h,className:"",iconSize:type==="driver"?[34,34]:[18,18],iconAnchor:type==="driver"?[17,17]:[9,9]});}
  function initMap(){if(state.map||!window.L)return;state.map=L.map("liveMap",{zoomControl:true}).setView([11.5564,104.9282],13);L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",{maxZoom:20,attribution:'&copy; OpenStreetMap &copy; CARTO'}).addTo(state.map);state.map.on("moveend",()=>{const c=state.map.getCenter();state.mapCandidate={lat:c.lat,lon:c.lng};$("selectedCoordinates").textContent=c.lat.toFixed(5)+", "+c.lng.toFixed(5);});if(state.pickup)setPickup(state.pickup,$("pickupText").value);if(state.destination)setDestination(state.destination,$("destination").value);setTimeout(()=>state.map&&state.map.invalidateSize(),250);}
  function setPickup(p,label){state.pickup={lat:+p.lat,lon:+p.lon};$("pickupText").value=label||state.pickup.lat.toFixed(5)+", "+state.pickup.lon.toFixed(5);if(state.map){if(state.pickupMarker)state.pickupMarker.setLatLng([p.lat,p.lon]);else state.pickupMarker=L.marker([p.lat,p.lon],{icon:mapIcon("pickup")}).addTo(state.map);}}
  function setDestination(p,label){state.destination={lat:+p.lat,lon:+p.lon};$("destination").value=label||state.destination.lat.toFixed(5)+", "+state.destination.lon.toFixed(5);if(state.map){if(state.destinationMarker)state.destinationMarker.setLatLng([p.lat,p.lon]);else state.destinationMarker=L.marker([p.lat,p.lon],{icon:mapIcon("destination")}).addTo(state.map);}}
  function mapMode(mode){state.mapMode=mode;document.querySelectorAll("[data-map-mode]").forEach(x=>x.classList.toggle("active",x.dataset.mapMode===mode));$("mapStatus").textContent=mode==="pickup"?"Tap the map to set passenger pickup":"Tap the map to set destination";}
  function coordsFromMapLink(s){const patterns=[/[?&](?:q|query|destination|center)=(-?\d+(?:\.\d+)?)[,%2C ]+(-?\d+(?:\.\d+)?)/i,/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/,/!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)/,/^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/];for(const re of patterns){const m=String(s).match(re);if(m){const lat=+m[1],lon=+m[2];if(lat>=-90&&lat<=90&&lon>=-180&&lon<=180)return{lat,lon};}}return null;}
  function useMapLink(){const p=coordsFromMapLink($("googleMapsLink").value.trim());if(!p){message("bookingMessage","Could not read coordinates. Paste a Google Maps URL containing coordinates, or latitude,longitude.","error");return;}state.pickerMode="destination";state.mapMode="destination";state.mapCandidate=p;state.map.setView([p.lat,p.lon],17);$("selectedCoordinates").textContent=p.lat.toFixed(5)+", "+p.lon.toFixed(5)+" · Google Maps";$("locationModalTitle").textContent="Choose destination";}
  async function refreshDrivers(){if(!state.map)return;try{const data=await api("/api/drivers/live"),ids=new Set();data.items.forEach(d=>{ids.add(d.driver_id);const p=[+d.latitude,+d.longitude];let m=state.driverMarkers.get(d.driver_id);if(!m){m=L.marker(p,{icon:mapIcon("driver")}).addTo(state.map);state.driverMarkers.set(d.driver_id,m);}else m.setLatLng(p);m.bindPopup("<b>"+escapeHtml(d.vehicle_model||"WING FAST Driver")+"</b><br>"+escapeHtml(d.plate_number||""));});for(const [id,m] of state.driverMarkers)if(!ids.has(id)){state.map.removeLayer(m);state.driverMarkers.delete(id);}$("driverCount").textContent=data.items.length+" drivers online · refresh 5 sec";if(!$("mapStatus").textContent.startsWith("Tap the map"))$("mapStatus").textContent=data.items.length?"Live drivers on map":"No drivers currently online";await refreshDriverRoute();}catch(_){$("driverCount").textContent="Waiting for live driver locations…";}}
  async function refreshDriverRoute(){try{const data=await api("/api/bookings/mine"),b=data.items.find(x=>["accepted","arrived","in_progress"].includes(x.status)&&x.driver_lat!=null);if(!b||!state.pickup){if(state.routeLine){state.map.removeLayer(state.routeLine);state.routeLine=null;}return;}const a=[+b.driver_lat,+b.driver_lon],z=[+b.pickup_lat,+b.pickup_lon],u="https://router.project-osrm.org/route/v1/driving/"+a[1]+","+a[0]+";"+z[1]+","+z[0]+"?overview=full&geometries=geojson",r=await(await fetch(u)).json();if(!r.routes||!r.routes[0])return;const pts=r.routes[0].geometry.coordinates.map(c=>[c[1],c[0]]);if(state.routeLine)state.routeLine.setLatLngs(pts);else state.routeLine=L.polyline(pts,{color:"#FFC928",weight:5,opacity:.9}).addTo(state.map);$("mapStatus").textContent="Your driver is on the way";}catch(_){}}
  function startLiveMap(){initMap();if(!state.map)return;setTimeout(()=>state.map.invalidateSize(),150);refreshDrivers();if(state.liveTimer)clearInterval(state.liveTimer);state.liveTimer=setInterval(refreshDrivers,5000);}
  function chooseVehicle(type){$("vehicleType").value=type;document.querySelectorAll("[data-vehicle-choice]").forEach(btn=>{const active=btn.dataset.vehicleChoice===type;btn.classList.toggle("active",active);btn.setAttribute("aria-pressed",String(active));});}
  async function openLocationPicker(mode){state.pickerMode=mode;state.mapMode=mode;$("locationModalTitle").textContent=mode==="pickup"?"Choose pickup point":"Choose destination";$("locationModalHint").textContent=mode==="pickup"?"The center pin marks your real pickup location. Move the map to adjust.":"Move the map until the center pin marks your destination."; $("locationPicker").classList.remove("hidden");initMap();if(!state.map)return;startLiveMap();const existing=mode==="pickup"?state.pickup:state.destination;if(existing){state.map.setView([existing.lat,existing.lon],16);}else if(mode==="pickup"){await requestCurrentLocation(false);}else if(state.pickup){state.map.setView([state.pickup.lat,state.pickup.lon],14);}setTimeout(()=>{state.map.invalidateSize();const c=state.map.getCenter();state.mapCandidate={lat:c.lat,lon:c.lng};$("selectedCoordinates").textContent=c.lat.toFixed(5)+", "+c.lng.toFixed(5);updateDistance();},180);}
  function closeLocationPicker(){ $("locationPicker").classList.add("hidden"); }
  function confirmLocationPicker(){const p=state.mapCandidate;if(!p)return;if(state.pickerMode==="pickup")setPickup(p,"GPS / Map: "+p.lat.toFixed(5)+", "+p.lon.toFixed(5));else setDestination(p,"Map: "+p.lat.toFixed(5)+", "+p.lon.toFixed(5));closeLocationPicker();updateDistance();}
  function haversine(a,b){const rad=x=>x*Math.PI/180,R=6371,dLat=rad(b.lat-a.lat),dLon=rad(b.lon-a.lon),q=Math.sin(dLat/2)**2+Math.cos(rad(a.lat))*Math.cos(rad(b.lat))*Math.sin(dLon/2)**2;return 2*R*Math.asin(Math.sqrt(q));}
  async function updateDistance(){if(!state.pickup||!state.destination){$("distanceInfo").textContent="Choose pickup and destination to calculate distance.";state.distanceKm=null;return;}if(state.distanceMode==="map"){state.distanceKm=haversine(state.pickup,state.destination);$("distanceInfo").textContent="Map distance: "+state.distanceKm.toFixed(2)+" km (straight line).";return;}try{const a=state.pickup,b=state.destination,url="https://router.project-osrm.org/route/v1/driving/"+a.lon+","+a.lat+";"+b.lon+","+b.lat+"?overview=full&geometries=geojson";$("distanceInfo").textContent="Calculating actual road distance…";const data=await(await fetch(url)).json();if(!data.routes||!data.routes[0])throw new Error("No driving route");state.distanceKm=data.routes[0].distance/1000;const pts=data.routes[0].geometry.coordinates.map(c=>[c[1],c[0]]);if(state.selectedRouteLine)state.map.removeLayer(state.selectedRouteLine);state.selectedRouteLine=L.polyline(pts,{color:"#FFC928",weight:4,opacity:.9,dashArray:"7 6"}).addTo(state.map);$("distanceInfo").textContent="Actual driving distance: "+state.distanceKm.toFixed(2)+" km."; }catch(_){state.distanceKm=null;$("distanceInfo").textContent="Could not calculate road route. Try map distance.";}}
  async function requestCurrentLocation(showMessage=true){if(!navigator.geolocation){if(showMessage)message("bookingMessage","GPS is not supported by this device.","error");return;}if(state.locationPermission==="denied"){if(showMessage)message("bookingMessage","Location permission is blocked in browser settings. Enable it once to continue.","error");return;}if(state.locationAsked&&state.locationPermission==="prompt"){if(showMessage)message("bookingMessage","Please allow location access in your browser settings.","error");return;}state.locationAsked=true;$("useCurrentLocation").textContent="RECENTERING…";navigator.geolocation.getCurrentPosition(pos=>{state.locationPermission="granted";const p={lat:pos.coords.latitude,lon:pos.coords.longitude};state.mapCandidate=p;if(state.map)state.map.setView([p.lat,p.lon],17);$("selectedCoordinates").textContent=p.lat.toFixed(5)+", "+p.lon.toFixed(5)+" · GPS";$("useCurrentLocation").textContent="RECENTER GPS";},err=>{if(err.code===1)state.locationPermission="denied";else if(err.code===2)state.locationPermission="unavailable";$("useCurrentLocation").textContent="USE MY GPS";if(showMessage)message("bookingMessage",err.code===1?"Location permission denied. Enable it in browser settings.":err.message||"Unable to get GPS location.","error");},{enableHighAccuracy:true,timeout:15000,maximumAge:60000});}
  async function checkLocationPermission(){if(!navigator.permissions||!navigator.permissions.query)return;try{const p=await navigator.permissions.query({name:"geolocation"});state.locationPermission=p.state;p.onchange=()=>{state.locationPermission=p.state;};}catch(_){} }

  function animateAppReveal() {
    const view = document.querySelector("#rideView:not(.hidden)");
    if (!view) return;
    const items = Array.from(view.querySelectorAll(":scope > :not(.hidden), .vehicle-option"))
      .filter(el => !el.classList.contains("hidden"))
      .sort((a,b) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1);
    items.forEach((el, index) => {
      el.classList.remove("anime-reveal");
      el.style.animationDelay = (index * 105) + "ms";
      void el.offsetWidth;
      el.classList.add("anime-reveal");
    });
  }

  async function boot() {
    $("loading").classList.remove("hidden");
    $("errorPanel").classList.add("hidden");
    $("appContent").classList.add("hidden");
    try {
      if (!tg && location.hostname !== "localhost" && location.hostname !== "127.0.0.1") throw new Error("Please open WING FAST from its Telegram bot.");
      const result = await api("/api/me");
      state.user = result.user; state.driver = result.driver;
      const displayName = [state.user.first_name, state.user.last_name].filter(Boolean).join(" ") || state.user.full_name || "WING FAST User";
      $("profileName").textContent = displayName;
      $("profileAvatar").textContent = (displayName.trim()[0] || "W").toUpperCase();
      $("profileUsername").textContent = state.user.username ? "@" + state.user.username.replace(/^@/,"") : "Telegram account";
      $("profileRole").textContent = ({admin:"Admin",driver:"Driver",driver_pending:"Driver pending",passenger:"Passenger"})[state.user.role] || "WING FAST member";
      $("profilePhone").textContent = state.user.phone || "Not added";
      $("profileTelegramId").textContent = state.user.telegram_user_id || state.user.id || "—";
      $("profileStatus").textContent = state.user.account_status || "Active";
      if (["driver","driver_pending"].includes(state.user.role) || state.driver) $("profileDriverAction").classList.remove("hidden");
      if (state.user.role === "admin") $("profileAdminAction").classList.remove("hidden");
      $("loading").classList.add("hidden"); $("errorPanel").classList.add("hidden"); $("appContent").classList.remove("hidden");
      animateAppReveal();
      checkLocationPermission();
      loadPromotion();
    } catch (e) { showError(e); }
  }
  function locate(){openLocationPicker("pickup");}
  async function bookRide() {
    const destination = $("destination").value.trim();
    if (!state.pickup) { message("bookingMessage", "Please share your pickup location first.", "error"); return; }
    if (destination.length < 2) { message("bookingMessage", "Please enter your destination.", "error"); return; }
    const btn = $("bookBtn"); btn.disabled = true; btn.textContent = "SENDING REQUEST…";
    try {
      const result = await api("/api/bookings", { method:"POST", body: JSON.stringify({
        pickup_lat: state.pickup.lat, pickup_lon: state.pickup.lon, pickup_text: $("pickupText").value,
        destination_text: destination, destination_lat:state.destination?state.destination.lat:null, destination_lon:state.destination?state.destination.lon:null, vehicle_type:$("vehicleType").value, payment_method:$("payment").value, distance_mode:state.distanceMode, estimated_distance_km:state.distanceKm
      })});
      message("bookingMessage", "Ride request created: " + result.booking_id, "success");
      $("destination").value="";state.destination=null;state.distanceKm=null;if(state.destinationMarker){state.map.removeLayer(state.destinationMarker);state.destinationMarker=null;}
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
  let promotionObjectUrl = null;
  async function loadPromotionImage(mediaId, targetId) {
    const target = $(targetId);
    if (!target) return;
    try {
      const response = await fetch("/api/media/" + encodeURIComponent(mediaId), {headers: authHeaders()});
      if (!response.ok) throw new Error("Image unavailable");
      const url = URL.createObjectURL(await response.blob());
      if (targetId === "promotionImage") {
        if (promotionObjectUrl) URL.revokeObjectURL(promotionObjectUrl);
        promotionObjectUrl = url;
        target.src = url;
        $("promotionBanner").classList.remove("hidden");
      } else {
        target.innerHTML = '<img src="' + url + '" alt="Current promotion">';
      }
    } catch (_) {
      if (targetId === "promotionImage") $("promotionBanner").classList.add("hidden");
      else target.textContent = "Image preview unavailable";
    }
  }
  async function loadPromotion() {
    try {
      const data = await api("/api/promotions/active");
      const p = data.promotion;
      if (!p) {
        $("promotionBanner").classList.add("hidden");
        $("adminPromotionPreview").innerHTML = '<span>No promotion image</span>';
        $("removePromotionBtn").classList.add("hidden");
        return;
      }
      await loadPromotionImage(p.media_id, "promotionImage");
      await loadPromotionImage(p.media_id, "adminPromotionPreview");
      $("removePromotionBtn").classList.remove("hidden");
    } catch (e) {
      $("promotionBanner").classList.add("hidden");
      if ($("promotionMessage")) message("promotionMessage", e.message, "error");
    }
  }
  async function savePromotion() {
    const file = $("promotionUpload").files[0];
    if (!file) { message("promotionMessage", "Choose an image first.", "error"); return; }
    const btn = $("savePromotionBtn");
    btn.disabled = true;
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("category", "other");
      const response = await fetch("/api/media", {method:"POST", headers:{"Authorization":authHeaders().Authorization}, body:form});
      const uploaded = await response.json().catch(()=>({}));
      if (!response.ok) throw new Error(uploaded.detail || "Image upload failed");
      await api("/api/admin/promotions", {method:"POST", body:JSON.stringify({media_id:uploaded.media.id})});
      $("promotionUpload").value = "";
      message("promotionMessage", "Promotion image saved.", "success");
      await loadPromotion();
    } catch (e) { message("promotionMessage", e.message, "error"); }
    finally { btn.disabled = false; }
  }
  async function removePromotion() {
    try {
      const active = await api("/api/promotions/active");
      if (!active.promotion || !confirm("Remove the current promotion image?")) return;
      await api("/api/admin/promotions/" + encodeURIComponent(active.promotion.id), {method:"DELETE"});
      $("promotionBanner").classList.add("hidden");
      $("adminPromotionPreview").innerHTML = '<span>No promotion image</span>';
      $("removePromotionBtn").classList.add("hidden");
      message("promotionMessage", "Promotion removed.", "success");
    } catch (e) { message("promotionMessage", e.message, "error"); }
  }

  async function loadAdmin() {
    try {
      const [stats, drivers, bookings] = await Promise.all([
        api("/api/admin/overview"), api("/api/admin/drivers"), api("/api/admin/bookings"), loadPromotion()
      ]);
      $("adminStats").innerHTML = [["USERS",stats.users],["PENDING DRIVERS",stats.pending_drivers],["ACTIVE DRIVERS",stats.active_drivers],["ACTIVE RIDES",stats.active_bookings]]
        .map(([label,value]) => '<div class="stat"><b>' + value + '</b><span>' + label + '</span></div>').join("");
      $("adminDrivers").innerHTML = drivers.items.length ? drivers.items.map((d) =>
        '<article class="admin-card"><strong>' + escapeHtml(d.first_name || d.driver_id) + ' · ' + escapeHtml(d.plate_number) + '</strong><small>' + escapeHtml(d.vehicle_model) + ' / ' + escapeHtml(d.phone) + ' · ' + escapeHtml(d.status) + '</small>' +
        '<div class="card-actions">' + (d.status === "pending" ? '<button class="mini-btn good" data-approve="' + escapeHtml(d.driver_id) + '">APPROVE</button><button class="mini-btn" data-reject="' + escapeHtml(d.driver_id) + '">REJECT</button>' : '') +
        (d.status !== "blocked" ? '<button class="mini-btn" data-block="' + escapeHtml(d.driver_id) + '">BLOCK DRIVER</button>' : '<span class="muted">BLOCKED</span>') + '</div></article>'
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
    const tab = event.target.closest(".bottom-nav .tab");
    if (tab) setView(tab.dataset.view);
    const openView = event.target.closest("[data-open-view]");
    if (openView) setView(openView.dataset.openView);
    const cancel = event.target.closest("[data-cancel]"); if (cancel) cancelRide(cancel.dataset.cancel);
    const accept = event.target.closest("[data-accept]"); if (accept) acceptRide(accept.dataset.accept);
    const approve = event.target.closest("[data-approve]"); if (approve) adminAction("/api/admin/drivers/" + encodeURIComponent(approve.dataset.approve) + "/approve");
    const reject = event.target.closest("[data-reject]"); if (reject) adminAction("/api/admin/drivers/" + encodeURIComponent(reject.dataset.reject) + "/reject");
    const block = event.target.closest("[data-block]"); if (block && confirm("Block this driver and account?")) adminAction("/api/admin/drivers/" + encodeURIComponent(block.dataset.block) + "/block");
  });
  $("locateBtn").addEventListener("click",()=>openLocationPicker("pickup"));
  $("pickupText").addEventListener("click",()=>openLocationPicker("pickup"));
  $("destination").addEventListener("click",()=>openLocationPicker("destination"));
  $("chooseDestinationBtn").addEventListener("click",()=>openLocationPicker("destination"));
  $("closeLocationPicker").addEventListener("click",closeLocationPicker);
  $("cancelLocationPicker").addEventListener("click",closeLocationPicker);
  $("confirmLocationPicker").addEventListener("click",confirmLocationPicker);
  $("useCurrentLocation").addEventListener("click",()=>requestCurrentLocation(true));
  $("distanceMode").addEventListener("change",e=>{state.distanceMode=e.target.value;updateDistance();});
  document.querySelectorAll("[data-vehicle-choice]").forEach(x=>x.addEventListener("click",()=>chooseVehicle(x.dataset.vehicleChoice)));
  $("useMapLinkBtn").addEventListener("click",useMapLink);
  $("destination").addEventListener("change",()=>{const p=coordsFromMapLink($("destination").value);if(p){state.mapCandidate=p;setDestination(p,$("destination").value);}});
  $("bookBtn").addEventListener("click", bookRide);
  $("applyDriverBtn").addEventListener("click", applyDriver);
  $("shareLocationBtn").addEventListener("click", startLocation);
  $("loadActivity").addEventListener("click", loadActivity);
  $("loadRequests").addEventListener("click", loadRequests);
  $("loadAdmin").addEventListener("click", loadAdmin);
  $("savePromotionBtn").addEventListener("click", savePromotion);
  $("removePromotionBtn").addEventListener("click", removePromotion);
  $("retryBtn").addEventListener("click", boot);
  boot();
})();

/* WING FAST multilingual interface: Khmer, Chinese and English. */
(() => {
  const dictionaries = {
    km: {
      "SET PICKUP":"កំណត់ទីតាំងទទួល",
      "SET DESTINATION":"កំណត់គោលដៅ",
      "USE LINK":"ប្រើ Link",
      "RECENTER GPS":"កំណត់ទីតាំង GPS ឡើងវិញ",
      "RECENTERING…":"កំពុងកំណត់ទីតាំងឡើងវិញ…",
      "Paste Google Maps link (optional)":"បិទភ្ជាប់ Link Google Maps (ជាជម្រើស)",
      "Finding nearby drivers…":"កំពុងស្វែងរកអ្នកបើកបរនៅជិត…",
      "Tap the map to set passenger pickup":"ចុចលើផែនទីដើម្បីកំណត់ទីតាំងទទួល",
      "Tap the map to set destination":"ចុចលើផែនទីដើម្បីកំណត់គោលដៅ",
      "No drivers currently online":"មិនមានអ្នកបើកបរអនឡាញទេ",
      "Your driver is on the way":"អ្នកបើកបរកំពុងធ្វើដំណើរមករកអ្នក",
      "CHOOSE YOUR RIDE":"ជ្រើសរើសការធ្វើដំណើរ","Choose your vehicle":"ជ្រើសរើសយានជំនិះ","Sedan":"សេដង់","Tuk-Tuk":"តុកតុក","TukTuk":"តុកតុក","Motorcycle":"ម៉ូតូ",
      "Connecting to WING FAST…":"កំពុងភ្ជាប់ទៅ WING FAST…","PLEASE WAIT":"សូមរង់ចាំ",
      "Connection problem":"មានបញ្ហាក្នុងការតភ្ជាប់",
      "Try again":"ព្យាយាមម្ដងទៀត",
      "Administrator":"អ្នកគ្រប់គ្រង",
      "Approved driver":"អ្នកបើកបរដែលបានអនុម័ត",
      "Driver application pending":"កំពុងរង់ចាំការអនុម័តអ្នកបើកបរ",
      "Passenger":"អ្នកដំណើរ",
      "WING FAST member":"សមាជិក WING FAST",
      "ONLINE":"កំពុងអនឡាញ",
      "Ride":"កក់ដំណើរ",
      "Activity":"ប្រវត្តិដំណើរ",
      "Profile":"ប្រវត្តិរូប",
      "YOUR ACCOUNT":"គណនីរបស់អ្នក",
      "Phone number":"លេខទូរស័ព្ទ",
      "Telegram ID":"លេខសម្គាល់ Telegram",
      "Account status":"ស្ថានភាពគណនី",
      "Not added":"មិនទាន់បញ្ចូល",
      "Active":"សកម្ម",
      "Driver Center":"ផ្ទាំងអ្នកបើកបរ",
      "Manage your driver account and ride requests.":"គ្រប់គ្រងគណនីអ្នកបើកបរ និងសំណើដំណើរ",
      "Admin Panel":"ផ្ទាំងអ្នកគ្រប់គ្រង",
      "Manage drivers and bookings.":"គ្រប់គ្រងអ្នកបើកបរ និងការកក់ដំណើរ",
      "OPEN →":"បើក →",
      "Driver":"អ្នកបើកបរ",
      "Admin":"អ្នកគ្រប់គ្រង",
      "CAMBODIA SERVICE AREA":"តំបន់សេវាកម្មកម្ពុជា",
      "Set your pickup point":"កំណត់ទីតាំងទទួល",
      "Use your current GPS location":"ប្រើទីតាំង GPS បច្ចុប្បន្ន",
      "BOOK A RIDE":"កក់ការធ្វើដំណើរ",
      "Where to?":"តើអ្នកចង់ទៅទីណា?",
      "Pickup location":"ទីតាំងទទួល",
      "Current location":"ទីតាំងបច្ចុប្បន្ន",
      "LOCATE":"កំណត់ទីតាំង",
      "WAIT…":"សូមរង់ចាំ…",
      "UPDATED":"បានធ្វើបច្ចុប្បន្នភាព",
      "Destination":"គោលដៅ",
      "Enter destination":"បញ្ចូលគោលដៅ",
      "Vehicle":"ប្រភេទយានយន្ត",
      "All vehicles":"យានយន្តទាំងអស់",
      "Tuk Tuk":"តុកតុក",
      "Car":"រថយន្ត",
      "SUV":"រថយន្ត SUV",
      "Motorbike":"ម៉ូតូ",
      "Payment":"ការទូទាត់",
      "Cash":"សាច់ប្រាក់",
      "Digital":"ឌីជីថល",
      "FIND A DRIVER":"ស្វែងរកអ្នកបើកបរ",
      "YOUR JOURNEY":"ដំណើររបស់អ្នក",
      "Ride activity":"ប្រវត្តិការធ្វើដំណើរ",
      "Refresh":"ផ្ទុកឡើងវិញ",
      "Your rides will appear here.":"ដំណើររបស់អ្នកនឹងបង្ហាញនៅទីនេះ។",
      "Loading rides…":"កំពុងផ្ទុកប្រវត្តិដំណើរ…",
      "No rides yet.":"មិនទាន់មានដំណើរទេ។",
      "CANCEL RIDE":"បោះបង់ការធ្វើដំណើរ",
      "DRIVER CENTER":"មជ្ឈមណ្ឌលអ្នកបើកបរ",
      "Driver dashboard":"ផ្ទាំងគ្រប់គ្រងអ្នកបើកបរ",
      "NOT REGISTERED":"មិនទាន់ចុះឈ្មោះ",
      "Driver registration":"ការចុះឈ្មោះអ្នកបើកបរ",
      "Submit your vehicle details. Admin approval is required before receiving trips.":"សូមបញ្ជូនព័ត៌មានយានយន្ត។ ត្រូវការការអនុម័តពីអ្នកគ្រប់គ្រង មុនពេលទទួលដំណើរ។",
      "Phone number":"លេខទូរស័ព្ទ",
      "Vehicle type":"ប្រភេទយានយន្ត",
      "Vehicle model":"ម៉ូដែលយានយន្ត",
      "License plate":"ស្លាកលេខយានយន្ត",
      "SUBMIT FOR APPROVAL":"បញ្ជូនសម្រាប់ការអនុម័ត",
      "DRIVER STATUS":"ស្ថានភាពអ្នកបើកបរ",
      "Ready for requests":"រួចរាល់សម្រាប់ទទួលការកក់",
      "Turn on location to receive nearby ride requests.":"បើកទីតាំង ដើម្បីទទួលការកក់នៅក្បែរនេះ។",
      "GO ONLINE":"បើកអនឡាញ",
      "GO OFFLINE":"បិទអនឡាញ",
      "CONNECTING…":"កំពុងភ្ជាប់…",
      "Available requests":"សំណើធ្វើដំណើរដែលមាន",
      "No ride requests right now.":"មិនទាន់មានសំណើធ្វើដំណើរទេ។",
      "ACCEPT RIDE":"ទទួលយកដំណើរ",
      "PROMOTION":"ប្រម៉ូសិន",
      "Promotion image":"រូបភាពប្រម៉ូសិន",
      "BANNER":"បដារូបភាព",
      "No promotion image":"មិនទាន់មានរូបភាពប្រម៉ូសិន",
      "Upload or replace image":"បញ្ចូល ឬប្ដូររូបភាព",
      "SAVE PROMOTION":"រក្សាទុកប្រម៉ូសិន",
      "REMOVE PROMOTION":"លុបប្រម៉ូសិន",
      "Choose an image first.":"សូមជ្រើសរើសរូបភាពជាមុនសិន។",
      "Image upload failed":"ការបញ្ចូលរូបភាពមិនបានជោគជ័យទេ",
      "Promotion image saved.":"បានរក្សាទុករូបភាពប្រម៉ូសិន។",
      "Promotion removed.":"បានលុបប្រម៉ូសិន។",
      "Image preview unavailable":"មិនអាចបង្ហាញរូបភាពមើលជាមុនបានទេ",
      "CONTROL CENTER":"មជ្ឈមណ្ឌលគ្រប់គ្រង",
      "Admin panel":"ផ្ទាំងអ្នកគ្រប់គ្រង",
      "Driver applications":"ពាក្យស្នើសុំអ្នកបើកបរ",
      "Recent bookings":"ការកក់ថ្មីៗ",
      "USERS":"អ្នកប្រើប្រាស់",
      "PENDING DRIVERS":"អ្នកបើកបររង់ចាំ",
      "ACTIVE DRIVERS":"អ្នកបើកបរសកម្ម",
      "ACTIVE RIDES":"ដំណើរកំពុងដំណើរការ",
      "APPROVE":"អនុម័ត",
      "REJECT":"បដិសេធ",
      "BLOCK DRIVER":"បិទអ្នកបើកបរ",
      "BLOCKED":"ត្រូវបានបិទ",
      "No driver applications yet.":"មិនទាន់មានពាក្យស្នើសុំអ្នកបើកបរទេ។",
      "No bookings yet.":"មិនទាន់មានការកក់ទេ។",
      "SAFE · SIMPLE · FAST":"សុវត្ថិភាព · ងាយស្រួល · រហ័ស",
      "Pickup:":"ទីតាំងទទួល៖",
      "Status:":"ស្ថានភាព៖",
      "Passenger:":"អ្នកដំណើរ៖",
      "GPS is not supported by this device.":"ឧបករណ៍នេះមិនគាំទ្រ GPS ទេ។",
      "Pickup location updated.":"បានធ្វើបច្ចុប្បន្នភាពទីតាំងទទួល។",
      "Please share your pickup location first.":"សូមចែករំលែកទីតាំងទទួលជាមុនសិន។",
      "Please enter your destination.":"សូមបញ្ចូលគោលដៅរបស់អ្នក។",
      "SENDING REQUEST…":"កំពុងផ្ញើសំណើ…",
      "Application submitted. Please wait for admin approval.":"បានបញ្ជូនពាក្យស្នើសុំ។ សូមរង់ចាំការអនុម័តពីអ្នកគ្រប់គ្រង។",
      "Your application is waiting for admin approval.":"ពាក្យស្នើសុំរបស់អ្នកកំពុងរង់ចាំការអនុម័ត។",
      "GPS is not available.":"មិនអាចប្រើ GPS បានទេ។",
      "Please open WING FAST from its Telegram bot.":"សូមបើក WING FAST តាមរយៈ Telegram Bot។",
      "Block this driver and account?":"តើអ្នកចង់បិទអ្នកបើកបរ និងគណនីនេះមែនទេ?"
    },
    zh: {
      "SET PICKUP":"设置上车地点",
      "SET DESTINATION":"设置目的地",
      "USE LINK":"使用链接",
      "RECENTER GPS":"重新定位 GPS",
      "RECENTERING…":"正在重新定位…",
      "Paste Google Maps link (optional)":"粘贴 Google Maps 链接（可选）",
      "Finding nearby drivers…":"正在寻找附近司机…",
      "Tap the map to set passenger pickup":"点击地图设置上车地点",
      "Tap the map to set destination":"点击地图设置目的地",
      "No drivers currently online":"目前没有司机在线",
      "Your driver is on the way":"司机正在前往您的位置",
      "CHOOSE YOUR RIDE":"选择出行方式","Choose your vehicle":"选择交通工具","Sedan":"轿车","Tuk-Tuk":"嘟嘟车","TukTuk":"嘟嘟车","Motorcycle":"摩托车",
      "Connecting to WING FAST…":"正在连接 WING FAST…",
      "Connection problem":"连接出现问题",
      "Try again":"重试",
      "Administrator":"管理员",
      "Approved driver":"已认证司机",
      "Driver application pending":"司机申请审核中",
      "Passenger":"乘客",
      "WING FAST member":"WING FAST 用户",
      "ONLINE":"在线",
      "Ride":"叫车",
      "Profile":"个人资料",
      "Activity":"行程记录",
      "Driver":"司机",
      "Admin":"管理",
      "CAMBODIA SERVICE AREA":"柬埔寨服务区域",
      "Set your pickup point":"设置上车地点",
      "Use your current GPS location":"使用当前 GPS 位置",
      "BOOK A RIDE":"预约行程",
      "Where to?":"您要去哪里？",
      "Pickup location":"上车地点",
      "Current location":"当前位置",
      "LOCATE":"定位",
      "WAIT…":"请稍候…",
      "UPDATED":"已更新",
      "Destination":"目的地",
      "Enter destination":"输入目的地",
      "Vehicle":"车型",
      "All vehicles":"所有车型",
      "Tuk Tuk":"嘟嘟车",
      "Car":"轿车",
      "SUV":"SUV",
      "Motorbike":"摩托车",
      "Payment":"付款方式",
      "Cash":"现金",
      "Digital":"数字支付",
      "FIND A DRIVER":"寻找司机",
      "YOUR JOURNEY":"您的行程",
      "Ride activity":"行程记录",
      "Refresh":"刷新",
      "Your rides will appear here.":"您的行程将显示在这里。",
      "Loading rides…":"正在加载行程…",
      "No rides yet.":"暂无行程。",
      "CANCEL RIDE":"取消行程",
      "DRIVER CENTER":"司机中心",
      "Driver dashboard":"司机面板",
      "NOT REGISTERED":"尚未注册",
      "Driver registration":"司机注册",
      "Submit your vehicle details. Admin approval is required before receiving trips.":"请提交车辆信息。获得管理员批准后才能接单。",
      "Phone number":"电话号码",
      "Vehicle type":"车辆类型",
      "Vehicle model":"车辆型号",
      "License plate":"车牌号码",
      "SUBMIT FOR APPROVAL":"提交审核",
      "DRIVER STATUS":"司机状态",
      "Ready for requests":"已准备好接单",
      "Turn on location to receive nearby ride requests.":"开启定位以接收附近的订单。",
      "GO ONLINE":"上线接单",
      "GO OFFLINE":"下线",
      "CONNECTING…":"正在连接…",
      "Available requests":"可接订单",
      "No ride requests right now.":"目前没有订单。",
      "ACCEPT RIDE":"接受订单",
      "PROMOTION":"促销",
      "Promotion image":"促销图片",
      "BANNER":"横幅",
      "No promotion image":"暂无促销图片",
      "Upload or replace image":"上传或替换图片",
      "SAVE PROMOTION":"保存促销图片",
      "REMOVE PROMOTION":"删除促销图片",
      "Choose an image first.":"请先选择图片。",
      "Image upload failed":"图片上传失败",
      "Promotion image saved.":"促销图片已保存。",
      "Promotion removed.":"促销图片已删除。",
      "Image preview unavailable":"无法预览图片",
      "CONTROL CENTER":"控制中心",
      "Admin panel":"管理面板",
      "Driver applications":"司机申请",
      "Recent bookings":"最近订单",
      "USERS":"用户",
      "PENDING DRIVERS":"待审核司机",
      "ACTIVE DRIVERS":"在线司机",
      "ACTIVE RIDES":"进行中的行程",
      "APPROVE":"批准",
      "REJECT":"拒绝",
      "BLOCK DRIVER":"封禁司机",
      "BLOCKED":"已封禁",
      "No driver applications yet.":"暂无司机申请。",
      "No bookings yet.":"暂无订单。",
      "SAFE · SIMPLE · FAST":"安全 · 简单 · 快捷",
      "Pickup:":"上车地点：",
      "Status:":"状态：",
      "Passenger:":"乘客：",
      "GPS is not supported by this device.":"此设备不支持 GPS。",
      "Pickup location updated.":"上车地点已更新。",
      "Please share your pickup location first.":"请先分享您的上车位置。",
      "Please enter your destination.":"请输入目的地。",
      "SENDING REQUEST…":"正在发送请求…",
      "Application submitted. Please wait for admin approval.":"申请已提交，请等待管理员审核。",
      "Your application is waiting for admin approval.":"您的申请正在等待管理员审核。",
      "GPS is not available.":"GPS 不可用。",
      "Please open WING FAST from its Telegram bot.":"请通过 Telegram 机器人打开 WING FAST。",
      "Block this driver and account?":"确定封禁此司机及其账户吗？"
    }
  };
  let currentLanguage = "km";
  const replacements = Object.entries(dictionaries.km).concat(Object.entries(dictionaries.zh));
  const englishKeys = [...new Set(replacements.map(([key]) => key))].sort((a,b) => b.length-a.length);
  const textMap = (value) => {
    if (currentLanguage === "en") return value;
    const dict = dictionaries[currentLanguage] || {};
    let output = value;
    for (const key of englishKeys) {
      if (dict[key]) output = output.split(key).join(dict[key]);
    }
    return output;
  };
  const originalText = new WeakMap();
  const originalAttributes = new WeakMap();
  function translateElement(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (!originalText.has(node)) originalText.set(node, node.nodeValue);
      node.nodeValue = textMap(originalText.get(node));
    }
    const elements = [];
    if (root.nodeType === Node.ELEMENT_NODE && root.matches?.("[placeholder],[aria-label],[title]")) elements.push(root);
    root.querySelectorAll?.("[placeholder],[aria-label],[title]").forEach((el) => elements.push(el));
    for (const el of elements) {
      let originals = originalAttributes.get(el);
      if (!originals) { originals = {}; originalAttributes.set(el, originals); }
      for (const attr of ["placeholder","aria-label","title"]) {
        if (el.hasAttribute(attr)) {
          if (!(attr in originals)) originals[attr] = el.getAttribute(attr);
          el.setAttribute(attr, textMap(originals[attr]));
        }
      }
    }
  }
  function setLanguage(language) {
    currentLanguage = ["km","zh","en"].includes(language) ? language : "km";
    document.documentElement.lang = currentLanguage;
    try { localStorage.setItem("wing_fast_language", currentLanguage); } catch (_) {}
    translateElement(document.body);
  }
  const selector = document.getElementById("languageSelect");
  if (selector) {
    let saved = "km";
    try { saved = localStorage.getItem("wing_fast_language") || "km"; } catch (_) {}
    selector.value = ["km","zh","en"].includes(saved) ? saved : "km";
    selector.addEventListener("change", () => setLanguage(selector.value));
    setLanguage(selector.value);
  }
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "childList") record.addedNodes.forEach((node) => {
        if (node.nodeType === Node.ELEMENT_NODE) translateElement(node);
        else if (node.nodeType === Node.TEXT_NODE) node.nodeValue = textMap(node.nodeValue);
      });
    }
  });
  observer.observe(document.body, { childList:true, subtree:true });
  window.WING_FAST_setLanguage = setLanguage;
})();
