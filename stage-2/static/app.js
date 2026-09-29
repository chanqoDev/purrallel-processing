(() => {
  "use strict";
  const app = document.getElementById("app");
  const account = document.getElementById("account-area");
  const path = window.location.pathname;
  const WEEKDAY = ["sun","mon","tue","wed","thu","fri","sat"];

  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const session = () => {
    try { return JSON.parse(localStorage.getItem("tablekeeper-session") || "null"); }
    catch { return null; }
  };
  function setSession(value) {
    if (value) localStorage.setItem("tablekeeper-session", JSON.stringify(value));
    else localStorage.removeItem("tablekeeper-session");
    renderAccount();
  }
  async function request(url, options={}) {
    const current = session();
    const headers = new Headers(options.headers || {});
    if (current?.token) headers.set("Authorization", "Bearer " + current.token);
    if (options.body !== undefined && !(options.body instanceof FormData)) headers.set("Content-Type","application/json");
    let response;
    try {
      response = await fetch(url, {...options, headers,
        body: options.body === undefined || options.body instanceof FormData ? options.body : JSON.stringify(options.body)});
    } catch (error) { throw error; }
    const type = response.headers.get("content-type") || "";
    const value = type.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const err = new Error(value?.error?.message || "The request could not be completed.");
      err.status = response.status;
      err.code = value?.error?.code;
      throw err;
    }
    return value;
  }
  function renderAccount() {
    const user = session();
    account.innerHTML = user
      ? '<span class="current-user" data-testid="current-user">' + esc(user.display_name) + '</span><button class="link-button" data-testid="logout-button" type="button">Log out</button>'
      : '<a href="/login">Log in</a><a class="button-link" href="/signup">Join us</a>';
    account.querySelector("[data-testid='logout-button']")?.addEventListener("click", () => {
      setSession(null);
      if (path === "/") document.querySelector("[data-testid='booking-form']")?.remove();
    });
  }
  function showAuthError(message) {
    let el = document.querySelector("[data-testid='auth-error']");
    if (!el) {
      el = document.createElement("p"); el.className = "status error"; el.dataset.testid = "auth-error";
      (app.querySelector(".page-content") || app).prepend(el);
    }
    el.textContent = message;
  }
  function clearFeedback(testid) { document.querySelector("[data-testid='"+testid+"']")?.remove(); }
  function localDatePlus(days) {
    const date = new Date();
    date.setDate(date.getDate()+days);
    return date.getFullYear()+"-"+String(date.getMonth()+1).padStart(2,"0")+"-"+String(date.getDate()).padStart(2,"0");
  }

  function renderAuth(kind) {
    const signup = kind === "signup";
    app.innerHTML = '<div class="auth-wrap page-content"><section class="surface auth-card"><p class="eyebrow">A seat at the table</p><h1>'+ (signup ? "Come on in." : "Welcome back.") +'</h1><p class="booking-summary">'+ (signup ? "Make a little room for good food and good company." : "Your next evening out is just around the corner.") +'</p><form id="auth-form">'+
      '<div class="field"><label for="auth-email">Email</label><input id="auth-email" data-testid="'+(signup?"signup-email":"login-email")+'" type="email" autocomplete="email" required></div>'+
      (signup ? '<div class="field"><label for="display-name">Name</label><input id="display-name" data-testid="signup-display-name" autocomplete="name" required></div>' : '')+
      '<div class="field"><label for="auth-password">Password</label><input id="auth-password" data-testid="'+(signup?"signup-password":"login-password")+'" type="password" autocomplete="'+(signup?"new-password":"current-password")+'" required></div>'+
      '<button class="primary" data-testid="'+(signup?"signup-submit":"login-submit")+'" type="submit">'+(signup?"Create your account":"Log in")+'</button></form><p class="booking-summary" style="margin:18px 0 0">'+(signup?'Already a regular? <a class="muted-link" href="/login">Log in</a>':'New to the table? <a class="muted-link" href="/signup">Create an account</a>')+'</p></section></div>';
    app.querySelector("#auth-form").addEventListener("submit", async event => {
      event.preventDefault(); clearFeedback("auth-error");
      const button=app.querySelector("[data-testid='"+(signup?"signup-submit":"login-submit")+"']");
      button.disabled=true;
      const body={email:app.querySelector("[data-testid='"+(signup?"signup-email":"login-email")+"']").value,
        password:app.querySelector("[data-testid='"+(signup?"signup-password":"login-password")+"']").value};
      if (signup) body.display_name=app.querySelector("[data-testid='signup-display-name']").value;
      try {
        const value=await request("/auth/"+(signup?"signup":"login"),{method:"POST",body});
        setSession(value);
      } catch(error) {
        showAuthError(error.message || "We couldn't sign you in. Check your details and try again.");
      } finally { button.disabled=false; }
    });
  }

  let restaurants=[];
  let selectedRestaurant=null;
  let activeSearch=null;
  let searchSequence=0;
  let selection=null;
  let retryRecord=null;
  let formRevision=0;
  let successfulResult=null;
  async function loadRestaurants() {
    const data=await request("/restaurants");
    restaurants=data.restaurants || [];
    return restaurants;
  }
  async function getRestaurant(id) {
    if (selectedRestaurant?.id===id) return selectedRestaurant;
    return request("/restaurants/"+encodeURIComponent(id));
  }
  function renderHome() {
    app.innerHTML = '<section class="hero"><div class="hero-copy"><p class="eyebrow">A good night, made easy</p><h1>Find your place<br>at the table.</h1><p>Choose a neighborhood favorite, find a time that feels right, and leave the details to us.</p></div><div class="hero-art" aria-hidden="true"><i class="art-stem"></i><i class="art-leaf"></i><i class="art-leaf two"></i></div></section>'+
      '<section class="page-content"><form class="search-panel" id="search-form">'+
      '<div class="field"><label for="restaurant">Restaurant</label><select id="restaurant" data-testid="restaurant-select"></select></div>'+
      '<div class="field"><label for="visit-date">Date</label><input id="visit-date" data-testid="date-input" type="date" required></div>'+
      '<div class="field"><label for="party-size">Party size</label><input id="party-size" data-testid="party-size-input" type="number" min="1" step="1" value="2" required></div>'+
      '<button class="primary" type="submit" data-testid="search-button">Find a table</button></form>'+
      '<section id="results-region" aria-live="polite"></section><div id="booking-region"></div></section>';
    const restaurantSelect=app.querySelector("[data-testid='restaurant-select']");
    restaurantSelect.innerHTML=restaurants.map(r=>'<option value="'+esc(r.id)+'">'+esc(r.name)+'</option>').join("");
    app.querySelector("[data-testid='date-input']").value=localDatePlus(7);
    if (restaurants.length) restaurantSelect.value=restaurants[0].id;
    app.querySelector("#search-form").addEventListener("submit",event=>{event.preventDefault();runSearch(false);});
    restaurantSelect.addEventListener("change",()=>{selectedRestaurant=null;});
  }
  function labelsFor(ids,restaurant) {
    const tables=restaurant?.tables || [];
    return ids.map(id=>tables.find(t=>t.id===id)?.label || id);
  }
  function timeFromLocal(value) { return value?.slice(11,16) || ""; }
  function drawResults(data,restaurant,params) {
    activeSearch={...params,data,restaurant};
    selectedRestaurant=restaurant;
    const target=app.querySelector("#results-region");
    if (!data.slots.length) {
      target.innerHTML='<div class="section-head"><h2>Availability</h2><p class="results-note">'+esc(restaurant.name)+' · '+esc(params.date)+'</p></div><div class="empty-state" data-testid="no-slots">This restaurant is closed on that day. Try another date.</div>';
      return;
    }
    const tableList=restaurant.tables || [];
    let rows=data.slots.map(slot=>{
      const at=timeFromLocal(slot.starts_at_local);
      const available=new Set(slot.available_table_ids || []);
      const singles=tableList.map(table=>slotCell([table.id],table.label,available.has(table.id),at,params.date));
      const pairs=(slot.available_options || []).filter(option=>option.table_ids.length===2)
        .map(option=>slotCell(option.table_ids,labelsFor(option.table_ids,restaurant).join(" + "),true,at,params.date));
      return '<div class="slot-row"><div class="slot-time">'+esc(at)+'</div>'+singles.join("")+pairs.join("")+'</div>';
    }).join("");
    const headers=tableList.map(t=>'<div>'+esc(t.label)+'</div>').join("")+
      ((restaurant.combinable||[]).map(pair=>'<div>'+esc(labelsFor(pair,restaurant).join(" + "))+'</div>').join(""));
    target.innerHTML='<div class="section-head"><h2>Choose your moment</h2><p class="results-note">'+esc(restaurant.name)+' · '+esc(params.date)+' · '+params.party_size+' guests</p></div><div class="grid-scroll"><div class="slot-row-head"><div>Local time</div>'+headers+'</div><div class="availability-grid" data-testid="availability-grid">'+rows+'</div></div>';
    target.querySelectorAll(".slot-card[data-available='true']").forEach(button=>button.addEventListener("click",()=>{
      if (!session()) {showAuthError("Log in to reserve this table.");return;}
      selection={restaurantId:params.restaurant_id,restaurant,tableIds:JSON.parse(button.dataset.tableIds),startsAtLocal:button.dataset.local,partySize:params.party_size};
      formRevision++;retryRecord=null;successfulResult=null;clearFeedback("booking-error");clearFeedback("booking-uncertain");
      renderBooking();
    }));
  }
  function slotCell(ids,label,isAvailable,at,date) {
    const id=ids.join("+");
    return '<button type="button" class="slot-card" data-testid="slot-'+esc(id)+'-'+esc(at)+'" data-available="'+(isAvailable?"true":"false")+'" data-table-ids="'+esc(JSON.stringify(ids))+'" data-local="'+esc(date)+'T'+esc(at)+'"'+(isAvailable?"":" disabled")+'><span class="slot-name">'+esc(label)+'</span><span class="slot-state">'+(isAvailable?"Available":"Unavailable")+'</span></button>';
  }
  async function runSearch(preserveSelection) {
    const button=app.querySelector("[data-testid='search-button']");
    const params={restaurant_id:app.querySelector("[data-testid='restaurant-select']").value,
      date:app.querySelector("[data-testid='date-input']").value,
      party_size:Number(app.querySelector("[data-testid='party-size-input']").value)};
    if (!params.restaurant_id || !params.date || !Number.isInteger(params.party_size) || params.party_size<1) return;
    const sequence=++searchSequence;
    if (!preserveSelection) { selection=null;retryRecord=null;successfulResult=null;app.querySelector("#booking-region").innerHTML=""; }
    button.disabled=true;button.textContent="Checking…";
    const results=app.querySelector("#results-region");
    if (!preserveSelection) results.innerHTML='<div class="section-head"><h2>Availability</h2><p class="results-note">Checking local tables…</p></div><div class="loading-card">Looking for a time that suits you.</div>';
    try {
      const [data,restaurant]=await Promise.all([
        request("/availability?restaurant_id="+encodeURIComponent(params.restaurant_id)+"&date="+encodeURIComponent(params.date)+"&party_size="+encodeURIComponent(String(params.party_size))),
        getRestaurant(params.restaurant_id)
      ]);
      if (sequence!==searchSequence) return;
      if (selection && !preserveSelection) return;
      drawResults(data,restaurant,params);
      if (preserveSelection && selection) renderBooking();
    } catch(error) {
      if (sequence!==searchSequence) return;
      if (selection && !preserveSelection) return;
      results.innerHTML='<div class="status error">'+esc(error.message || "Availability could not be loaded.")+'</div>';
    } finally {
      if (sequence===searchSequence) {button.disabled=false;button.textContent="Find a table";}
    }
  }
  function renderBooking() {
    if (!selection) return;
    const labels=labelsFor(selection.tableIds,selection.restaurant);
    app.querySelector("#booking-region").innerHTML='<div class="booking-layout"><section class="surface booking-panel" data-testid="booking-form"><p class="eyebrow">Your table</p><h2>Make it yours</h2><p class="booking-summary" data-testid="booking-summary">Reserved for '+esc(labels.join(" + "))+' at '+esc(selection.startsAtLocal.slice(11,16))+' · '+esc(selection.restaurant.name)+'</p><div class="booking-controls"><div class="field"><label for="booking-party">Party size</label><input id="booking-party" data-testid="booking-party-size" type="number" min="1" step="1" value="'+esc(selection.partySize)+'"></div><button class="primary" type="button" data-testid="booking-submit">Confirm reservation</button></div>'+
      '<div id="booking-feedback"></div></section><aside class="booking-aside"><p class="eyebrow">A little anticipation</p><h3>Good food is better shared.</h3><p>We’ll hold the details here so you can find your way back.</p></aside></div>';
    app.querySelector("[data-testid='booking-party-size']").addEventListener("input",()=>{
      formRevision++;retryRecord=null;successfulResult=null;clearFeedback("booking-error");clearFeedback("booking-uncertain");clearConfirmation();
    });
    app.querySelector("[data-testid='booking-submit']").addEventListener("click",submitBooking);
    if (successfulResult && retryRecord?.revision===formRevision) renderConfirmation(successfulResult);
  }
  function clearConfirmation() { app.querySelector("[data-testid='confirmation']")?.remove(); }
  function renderConfirmation(result) {
    clearFeedback("booking-error");clearFeedback("booking-uncertain");
    const region=app.querySelector("#booking-feedback");
    if (!region) return;
    const labels=labelsFor(selection.tableIds,selection.restaurant);
    region.innerHTML='<section class="surface confirmation" data-testid="confirmation"><p class="eyebrow">You’re all set</p><h3>We saved you a seat.</h3><p class="confirmation-reference" data-testid="confirmation-reference">'+esc(result.reference)+'</p><p class="booking-summary" data-testid="confirmation-details">'+esc(selection.restaurant.name)+' · <span data-testid="confirmation-tables">'+esc(labels.join(" + "))+'</span> · '+esc(selection.startsAtLocal.slice(11,16))+'</p><p class="booking-summary">Keep this reference handy for your evening.</p></section>';
  }
  function errorBox(testid,message) {
    clearFeedback(testid);
    const el=document.createElement("div");el.className="status error";el.dataset.testid=testid;el.textContent=message;
    app.querySelector("#booking-feedback").append(el);
  }
  async function submitBooking() {
    if (!selection) return;
    if (!session()) {showAuthError("Log in to reserve this table.");return;}
    const party=Number(app.querySelector("[data-testid='booking-party-size']").value);
    const body={restaurant_id:selection.restaurantId,table_ids:selection.tableIds,starts_at_local:selection.startsAtLocal,party_size:party};
    const bodyKey=JSON.stringify(body);
    if (!retryRecord || retryRecord.bodyKey!==bodyKey || retryRecord.revision!==formRevision)
      retryRecord={body,bodyKey,key:(crypto.randomUUID?.() || (Date.now()+"-"+Math.random().toString(36).slice(2))),revision:formRevision};
    clearFeedback("booking-error");clearFeedback("booking-uncertain");
    const button=app.querySelector("[data-testid='booking-submit']");button.disabled=true;
    try {
      const result=await request("/reservations",{method:"POST",headers:{"Idempotency-Key":retryRecord.key},body:retryRecord.body});
      successfulResult=result;renderConfirmation(result);
      // A same-form submit deliberately retains its exact key/body for server replay.
    } catch(error) {
      if (error.status && error.status<500) {
        successfulResult=null;clearConfirmation();
        if (error.code==="table_unavailable") {
          selection.partySize=party;
          await runSearch(true);
        }
        errorBox("booking-error",error.message || "That table could not be reserved.");
      } else {
        successfulResult=null;clearConfirmation();
        const region=app.querySelector("#booking-feedback");
        const el=document.createElement("div");el.className="status uncertain";el.dataset.testid="booking-uncertain";
        el.textContent="We haven’t received a final answer yet. Your request is saved here; try again without changing the details.";
        region.append(el);
      }
    } finally {
      const current=app.querySelector("[data-testid='booking-submit']");
      if(current)current.disabled=false;
    }
  }

  async function renderLookup() {
    app.innerHTML='<section class="page-content"><p class="eyebrow">Your evening, at a glance</p><h1>Find your reservation.</h1><p class="booking-summary">Enter the reference from your confirmation to see the details or make a change.</p><section class="surface lookup-card"><form class="lookup-form" id="lookup-form"><div class="field"><label for="lookup-reference">Reservation reference</label><input id="lookup-reference" data-testid="lookup-reference-input" autocomplete="off" required></div><button class="primary" type="submit" data-testid="lookup-submit">Look it up</button></form><div id="lookup-feedback"></div></section></section>';
    app.querySelector("#lookup-form").addEventListener("submit",async event=>{
      event.preventDefault();clearFeedback("reservation-error");
      const ref=app.querySelector("[data-testid='lookup-reference-input']").value.trim();
      await loadReservation(ref);
    });
  }
  async function loadReservation(ref) {
    const region=app.querySelector("#lookup-feedback");
    region.innerHTML='<div class="loading-card">Finding your reservation…</div>';
    try {
      const result=await request("/reservations/"+encodeURIComponent(ref));
      const restaurant=await getRestaurant(result.restaurant_id);
      const ids=result.table_ids || (result.table_id?[result.table_id]:[]);
      const labels=labelsFor(ids,restaurant);
      region.innerHTML='<section class="surface reservation-detail" data-testid="reservation-detail"><p class="eyebrow">Reservation details</p><h2>'+esc(restaurant.name)+'</h2><dl><dt>Reference</dt><dd>'+esc(result.reference)+'</dd><dt>When</dt><dd>'+esc(result.starts_at_local.replace("T"," "))+'</dd><dt>Tables</dt><dd data-testid="reservation-tables">'+esc(labels.join(" + "))+'</dd><dt>Guests</dt><dd>'+esc(result.party_size)+'</dd><dt>Status</dt><dd data-testid="reservation-status">'+esc(result.status)+'</dd></dl>'+
        (result.status==="cancelled"?'':'<button type="button" class="secondary" data-testid="reservation-cancel-button">Cancel reservation</button>')+'</section>';
      region.querySelector("[data-testid='reservation-cancel-button']")?.addEventListener("click",async buttonEvent=>{
        const button=buttonEvent.currentTarget;button.disabled=true;clearFeedback("reservation-error");
        try { const updated=await request("/reservations/"+encodeURIComponent(result.reference)+"/cancel",{method:"POST",body:{}}); await loadReservation(updated.reference); }
        catch(error) {button.disabled=false;showReservationError(error.message || "We couldn’t cancel this reservation.");}
      });
    } catch(error) { showReservationError(error.message || "We couldn’t find that reservation."); }
  }
  function showReservationError(message) {
    const region=app.querySelector("#lookup-feedback");
    if (!region) return;
    region.innerHTML='<div class="status error" data-testid="reservation-error">'+esc(message)+'</div>';
  }

  async function start() {
    renderAccount();
    if (path==="/signup" || path==="/login") {renderAuth(path==="/signup"?"signup":"login");return;}
    if (path==="/lookup") {renderLookup();return;}
    if (path==="/") {
      renderHome();
      try { await loadRestaurants(); renderHome(); }
      catch(error) {app.innerHTML='<div class="status error">'+esc(error.message || "Restaurants are unavailable.")+'</div>';return;}
      return;
    }
    app.innerHTML='<section class="empty-state"><h1>That page isn’t here.</h1><a class="muted-link" href="/">Return to search</a></section>';
  }
  start();
})();
