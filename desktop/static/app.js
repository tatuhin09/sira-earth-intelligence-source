let sessionToken = "";
let overview = null;
let chatStatus = null;
let researchStatus = null;
let researchPollTimer = null;
let watchedResearchJobId = null;

const $ = (id) => document.getElementById(id);
const views = [...document.querySelectorAll(".view")];
const navItems = [...document.querySelectorAll(".nav-item")];

const subtitles = {
  dashboard: "Overview of SIRA's autonomous runtime, system health and recent activity.",
  chat: "Talk to SIRA about its local operational state.",
  activity: "Recent audit, cycle, recovery and diagnostic artifacts.",
  research: "Research capability, evidence and last research linkage.",
  memory: "Learning memory health and evidence counts.",
  providers: "Free-first provider policy and current authority.",
  promotions: "Protected promotion, rollback and recovery visibility.",
  health: "Release-readiness, tests and integrity snapshot.",
  notifications: "Owner access, permission and runtime notices.",
  settings: "Local desktop connection and control policy.",
};

function escapeText(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function humanTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeText(value);
  return new Intl.DateTimeFormat(undefined, {
    day: "2-digit", month: "short", year: "numeric",
    hour: "numeric", minute: "2-digit"
  }).format(date);
}

function updateClock() {
  const now = new Date();
  $("local-date").textContent = new Intl.DateTimeFormat(undefined, {
    weekday: "short", day: "2-digit", month: "short", year: "numeric"
  }).format(now);
  $("local-time").textContent = new Intl.DateTimeFormat(undefined, {
    hour: "numeric", minute: "2-digit", second: "2-digit"
  }).format(now);
}

function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2400);
}

async function api(path, options = {}) {
  const headers = {"Content-Type": "application/json"};
  if (options.method === "POST") headers["X-SIRA-Session"] = sessionToken;
  const response = await fetch(path, {...options, headers: {...headers, ...(options.headers || {})}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || data.error || "Request failed");
  return data;
}

function setPill(el, text, kind = "") {
  el.textContent = text;
  el.className = "pill" + (kind ? " " + kind : "");
}

function runtimeIsOn() {
  return overview?.runtime?.effective_state === "running";
}

function renderTimeline(container, items) {
  if (!items.length) {
    container.innerHTML = `<p class="muted">No activity artifacts yet.</p>`;
    return;
  }
  container.innerHTML = items.map((item) => `
    <div class="timeline-item">
      <span class="timeline-marker"></span>
      <div class="timeline-copy">
        <strong>${escapeText(item.title || item.kind)}</strong>
        <p>${escapeText(item.status || item.decision_code || item.outcome || "recorded")} • ${humanTime(item.created_at)}</p>
      </div>
    </div>
  `).join("");
}

function detailRows(rows) {
  return rows.map(([k, v]) =>
    `<div><span>${escapeText(k)}</span><strong>${escapeText(v)}</strong></div>`
  ).join("");
}

function renderOverview(data) {
  overview = data;
  const runtime = data.runtime || {};
  const release = data.release || {};
  const memory = data.memory || {};
  const notifications = data.notifications || {};
  const git = data.git || {};
  const last = runtime.last_cycle || {};
  const core = data.core || {};

  $("runtime-state").textContent = runtime.effective_state || "unknown";
  $("runtime-copy").textContent = runtime.effective_state === "running"
    ? "Persistent autonomous improvement loop is active."
    : "Autonomous runtime is stopped and remains owner-controlled.";
  setPill($("runtime-pill"), runtime.effective_state === "running" ? "LIVE" : "STOPPED", runtime.effective_state === "running" ? "live" : "");
  $("generation").textContent = runtime.generation ?? "—";
  $("side-generation").textContent = runtime.generation ?? "—";
  $("worker").textContent = runtime.worker_alive ? "Alive" : "Stopped";
  $("state-health").textContent = runtime.state_health || "—";

  $("release-ready").textContent = release.release_ready ? "Ready" : "Not ready";
  setPill($("release-pill"), release.release_ready ? `${release.checks_passed ?? "—"}/${release.checks_required ?? "—"}` : "CHECK", release.release_ready ? "good" : "bad");
  $("release-copy").textContent = release.release_ready
    ? "Final protected acceptance gate is green."
    : "Run the release check to refresh readiness.";

  const counts = memory.counts || {};
  $("core-knowledge-count").textContent = core.knowledge?.verified_count ?? "—";
  $("core-knowledge-state").textContent = core.knowledge?.integrity || "unavailable";
  $("core-goal-count").textContent = core.learning?.active_goal_count ?? "—";
  $("notification-count").textContent = notifications.total ?? 0;
  renderNotificationPreview(notifications);
  $("git-state").textContent = git.clean ? "Clean" : "Dirty";
  $("git-revision").textContent = git.revision || "—";

  $("sidebar-runtime").textContent = runtime.effective_state === "running" ? "Running" : "Stopped";
  $("sidebar-health").textContent = runtime.state_health === "ok" ? "Runtime healthy" : `Health: ${runtime.state_health || "unknown"}`;
  $("sidebar-revision").textContent = git.revision ? `rev ${git.revision}` : "revision unavailable";
  $("chat-current-status").textContent = `Current SIRA status: ${core.runtime?.effective_state || runtime.effective_state || "unknown"} • generation ${core.runtime?.generation ?? runtime.generation ?? "—"} • health ${core.runtime?.state_health || runtime.state_health || "unknown"}. Saved replies below describe their own time.`;
  $("runtime-btn").textContent = runtimeIsOn() ? "Stop SIRA" : "Start SIRA";
  $("runtime-btn").className = "button " + (runtimeIsOn() ? "danger" : "primary");

  $("last-cycle-title").textContent = last.outcome || last.status || "No completed cycle";
  setPill($("last-cycle-pill"), last.status || "—", last.status === "completed" ? "good" : "");
  $("last-cycle-body").innerHTML = detailRows([
    ["Status", last.status || "—"],
    ["Target", last.target_kind || "—"],
    ["Outcome", last.outcome || "—"],
    ["Completed", humanTime(last.completed_at)],
  ]);

  renderTimeline($("recent-activity"), data.activity || []);
  renderCore(core);
  renderMemory(data);
  renderHealth(data);
  if ($("research-detail")) renderResearch(data);
  renderProviders(data);
  renderPromotions(data);
  $("memory-strip").textContent = memory.healthy ? "Healthy" : "Attention";
}

function renderCore(core) {
  const task = (core.pending_work || [])[0] || core.last_task || {};
  const work = core.workers || {};
  const learning = core.learning || {};
  const knowledge = core.knowledge || {};
  const providers = core.providers || {};
  const goals = learning.goals || [];
  const gaps = core.gaps || [];
  $("core-task-title").textContent = task.intent ? `${task.intent} • ${task.status}` : "No Core task recorded";
  $("core-current-task").innerHTML = detailRows([
    ["Route", task.route || "—"], ["Why", task.reason || "—"],
    ["Priority", core.current_direct_user_task ? "Direct user task first" : "No pending user task"],
    ["Background", (core.pending_work || []).filter(row => row.origin === "background").length],
    ["Result ref", (task.artifact_refs || task.evidence_refs || [])[0] || "—"],
    ["Verification", task.verification_status || "not verified"],
  ]);
  $("core-goals-title").textContent = `${learning.active_goal_count ?? 0} active goal(s)`;
  $("core-goals").innerHTML = goals.length ? goals.map(goal => `
    <div class="core-list-item"><strong>${escapeText(goal.topic)}</strong>
    <span>${escapeText(goal.verified_steps ?? 0)}/${escapeText(goal.total_steps ?? 0)} steps with some evidence • ${escapeText(goal.last_outcome || "Not attempted")} • Next: ${escapeText(goal.next_question || "No eligible step yet")}</span></div>`).join("")
    : '<p class="muted">No active learning goals.</p>';
  const demonstrated = (core.capabilities || []).filter(row => row.state === "demonstrated");
  const researchCapability = (core.capabilities || []).find(row => row.name === "research");
  const researchReason = researchCapability?.reason === "verified_claim_evidence_not_general_research_mastery"
    ? "Bounded verified claim evidence; broader research capability is not demonstrated"
    : researchCapability?.reason === "no_current_two_host_verified_research_claim"
      ? "No current two-host verified claim evidence for the broader capability"
      : (researchCapability?.reason || "No capability evidence recorded");
  $("core-research-capability").textContent = `Overall Research capability: ${researchCapability?.state || "unverified"} • ${researchReason}`;
  $("core-capabilities").innerHTML = demonstrated.slice(0, 3).map(row => `
    <div class="core-list-item"><strong>${escapeText(row.name.replaceAll("_", " "))}</strong>
    <span>Demonstrated • ${escapeText((row.evidence_refs || [])[0] || "evidence unavailable")}</span></div>`).join("") +
    gaps.slice(0, 3).map(row => `<div class="core-list-item"><strong>${escapeText(row.capability)}</strong>
    <span>${escapeText(row.capability === "research" && row.reason === "missing_evidence" ? "Unverified: no current two-host verified research claim" : row.capability === "research" && row.state === "partially_demonstrated" ? `Partial: bounded two-host verified claim • ${(row.evidence_refs || [])[0] || "evidence unavailable"}` : `${row.state} • ${row.reason}`)}</span></div>`).join("") ||
    '<p class="muted">No current capability evidence recorded.</p>';
  $("core-worker-title").textContent = work.active_cycle
    ? `Cycle ${work.active_cycle.phase || "active"}` : core.runtime?.worker_alive
      ? "Runtime worker alive / no active cycle" : "No active cycle recorded";
  $("core-workers").innerHTML = detailRows([
    ["Worker", work.runtime_worker || "unknown"],
    ["Current cycle", work.active_cycle?.cycle_id || "None"],
    ["Recent worker tasks", (work.recent_tasks || []).length],
    ["Recent outcome", core.recent_outcome || "—"],
    ["Recent failure", (core.recent_failures || [])[0]?.outcome || "None recorded"],
    ["Promotion", (core.recent_improvements || [])[0]?.promotion_id || "None recorded"],
  ]);
  $("core-provider-title").textContent = `${providers.available_count ?? 0} of ${providers.known_count ?? 0} available`;
  $("core-providers").innerHTML = detailRows([
    ["Cooling", providers.cooling_count ?? 0],
    ["Owner runtime", core.runtime?.desired_state || "unknown"],
    ["Authority", core.authority_granted ? "Granted" : "Not granted"],
    ["Promotion", core.promotion_authorized ? "Authorized" : "Not authorized"],
    ["Paid spending", core.paid_spending_authorized ? "Authorized" : "Not authorized"],
    ["Bootstrap sources", knowledge.bootstrap?.source_count ?? 0],
    ["Stale / conflicted", `${knowledge.stale_sample_count ?? 0} sampled / ${knowledge.conflicted_count ?? 0}`],
  ]);
  $("core-authority").textContent = core.authority_granted ? "Granted" : "Not granted";
}

function renderMemory(data) {
  const memory = data.memory || {};
  const counts = memory.counts || {};
  $("memory-health").textContent = memory.healthy ? "Healthy" : "Needs attention";
  $("memory-health-copy").textContent = `SQLite quick-check: ${memory.quick_check || "unknown"} • schema v${memory.schema_version ?? "—"}`;
  $("memory-detail").innerHTML = detailRows([
    ["Persistent Memory (experience/context)", counts.memories ?? 0],
    ["Verified Knowledge (independently checked claims)", data.core?.knowledge?.verified_count ?? 0],
    ["Occurrences", counts.occurrences ?? 0],
    ["Transitions", counts.transitions ?? 0],
  ]);
}

function renderHealth(data) {
  const runtime = data.runtime || {};
  const release = data.release || {};
  const memory = data.memory || {};
  const git = data.git || {};
  const checks = [
    ["Runtime state", runtime.state_health === "ok" ? "Healthy" : runtime.state_health || "Unknown", runtime.state_health === "ok"],
    ["Release acceptance", release.release_ready ? "Ready" : "Not ready", !!release.release_ready],
    ["Memory DB", memory.healthy ? "Healthy" : "Unhealthy", !!memory.healthy],
    ["Git working tree", git.clean ? "Clean" : "Dirty", !!git.clean],
    ["Owner control", "Explicit Start / Stop", true],
    ["Desktop binding", "Loopback only", true],
    ["Protected shell", "Active", true],
    ["Paid spending", "Disabled by default", true],
    ["Release checks", release.release_ready
      ? `${release.checks_passed ?? "—"} / ${release.checks_required ?? "—"}` : "No current pass", !!release.release_ready],
  ];
  $("tests-summary").textContent = release.release_ready ? "Ready" : "Not ready";
  setPill($("health-release-pill"), release.release_ready ? "Ready" : "Check", release.release_ready ? "good" : "bad");
  $("health-grid").innerHTML = checks.map(([k,v,good]) => `
    <div class="health-item ${good ? "good" : ""}">
      <span class="label">${escapeText(k)}</span><strong>${escapeText(v)}</strong>
    </div>`).join("");
}

function renderResearch(data) {
  const last = data.runtime?.last_cycle || {};
  $("research-detail").innerHTML = detailRows([
    ["Last target", last.target_kind || "—"],
    ["Opportunity ID", last.opportunity_id || "—"],
    ["Evidence ID", last.evidence_id || "—"],
    ["Research ID", last.research_id || "Not created"],
  ]);
}

function renderProviders(data) {
  const release = data.release || {};
  const core = data.core || {};
  const providers = core.providers || {};
  $("provider-authority").innerHTML = detailRows([
    ["Paid spending", core.paid_spending_authorized ? "Authorized" : "Not authorized"],
    ["External UI API", "None"],
    ["Release state", release.release_ready ? "Ready" : "Check required"],
  ]);
  $("provider-list").innerHTML = (providers.entries || []).map(row =>
    `<div class="provider-item"><strong>${escapeText(row.provider_id)}</strong><span>${escapeText(row.state)} • ${escapeText(row.cost_class)}</span></div>`
  ).join("") || '<p class="muted">No provider health snapshot available.</p>';
}

function renderPromotions(data) {
  const last = data.runtime?.last_cycle || {};
  $("promotion-detail").innerHTML = detailRows([
    ["Promotion ID", last.promotion_id || "None in last cycle"],
    ["Last outcome", last.outcome || "—"],
    ["Runtime state", data.runtime?.effective_state || "—"],
  ]);
  const promotionRows = (data.activity || []).filter(
    (item) => ["promotion","recovery"].includes(item.kind)
  );
  renderTimeline($("promotion-activity"), promotionRows);
}

function renderChat(messages) {
  const el = $("chat-messages");
  el.innerHTML = (messages || []).map((row) => `
    <div class="message ${row.role === "user" ? "user" : "assistant"}">
      ${escapeText(row.text)}
      <span class="message-meta">${row.role === "assistant" ? "Saved reply • " : "Sent • "}${humanTime(row.created_at)}${row.role === "assistant" ? " • Historical snapshot; check current status above" : ""}</span>
    </div>`).join("");
  el.scrollTop = el.scrollHeight;

  const rail = $("dashboard-chat");
  const last = (messages || []).slice(-4);
  rail.innerHTML = last.length ? last.map((row) => `
    <div class="message ${row.role === "user" ? "user" : "assistant"}">${escapeText(row.text)}
    <span class="message-meta">${row.role === "assistant" ? "Saved reply • " : "Sent • "}${humanTime(row.created_at)}</span></div>
  `).join("") : `<div class="empty-state">Ask SIRA about status, memory or its last cycle.</div>`;
  rail.scrollTop = rail.scrollHeight;
}

async function refreshOverview() {
  renderOverview(await api("/api/overview"));
}

async function refreshActivity() {
  const data = await api("/api/activity");
  renderTimeline($("activity-list"), data.items || []);
}

async function refreshNotifications() {
  const data = await api("/api/notifications");
  const rows = data.events || [];
  $("notification-count").textContent = data.total ?? rows.length;
  renderNotificationPreview(data);
  $("notification-list").innerHTML = rows.length ? rows.slice().reverse().map((row) => `
    <div class="notification">
      <h3>${escapeText(row.title || "SIRA notification")}</h3>
      <p>${escapeText(row.body || "")}</p>
      <span class="muted tiny">${escapeText(row.category || "owner_notice")} • ${escapeText(row.urgency || "importance unspecified")} • Delivery: ${escapeText(row.status || "unknown")} • ${humanTime(row.created_at)}${row.source_kind ? ` • ${escapeText(row.source_kind)}` : ""}${row.source_request_id ? ` • ${escapeText(row.source_request_id)}` : ""}</span>
    </div>`).join("") : `<div class="empty-state">No owner notifications recorded.</div>`;
}

function renderNotificationPreview(notifications) {
  const latest = notifications.latest;
  $("dashboard-notification").innerHTML = latest
    ? `<strong>${escapeText(latest.title || "Owner notice")}</strong><p>${escapeText(latest.body || "")}</p><span class="muted tiny">${escapeText(latest.category || "owner_notice")} • ${humanTime(latest.created_at)} • Delivery: ${escapeText(latest.status || "unknown")}</span>`
    : "No owner notifications recorded.";
}

function renderChatStatus(data) {
  chatStatus = data;
  const guard = data.cost_guard || {};
  const broker = data.capability_broker || {};
  const ready = !!data.ready;

  setPill(
    $("chat-mode-pill"),
    ready ? "Gemini ready" : "Guarded / local",
    ready ? "good" : "neutral"
  );

  $("chat-model-title").textContent = ready
    ? "Gemini chat is ready"
    : "Model chat is guarded";

  if (!guard.free_tier_confirmed) {
    $("chat-model-copy").textContent =
      "Confirm zero-cost/no-charge use before SIRA may call Gemini.";
  } else if (broker.status !== "ready") {
    $("chat-model-copy").textContent =
      "Gemini is currently blocked by SIRA capability or credential policy.";
  } else if (!guard.allowed) {
    $("chat-model-copy").textContent =
      "The local desktop-chat request cap is currently blocking model calls.";
  } else {
    $("chat-model-copy").textContent =
      "General messages can use bounded Gemini conversation with local SIRA memory/runtime context.";
  }

  $("free-tier-confirm").checked = !!guard.free_tier_confirmed;
  $("chat-usage").innerHTML = detailRows([
    ["Provider", data.provider || "gemini"],
    ["Model", data.model_id || "—"],
    ["Broker", broker.status || "—"],
    ["Used this month", guard.local_requests_this_month ?? 0],
    ["Local cap", guard.monthly_cap ?? "—"],
    ["Paid authority", data.paid_spending_authority ? "Enabled" : "None"],
  ]);
}

async function refreshChatStatus() {
  renderChatStatus(await api("/api/chat/status"));
}

async function refreshChat() {
  const data = await api("/api/chat");
  renderChat(data.messages || []);
  await refreshChatStatus();
}

function researchStatusKind(status) {
  if (status === "completed") return "good";
  if (status === "failed") return "bad";
  if (status === "blocked" || status === "limited") return "neutral";
  if (status === "running" || status === "queued") return "live";
  return "neutral";
}

function renderResearchJobCard(job) {
  const verification = job.verification || {};
  const sources = job.sources || [];
  const metrics = job.metrics || {};
  const sourceHtml = sources.length
    ? `<div class="source-grid">${sources.map((source) => `
        <div class="source-card">
          <strong>${escapeText(source.source_id || "S")} • ${escapeText(source.title || "Source")}</strong>
          <span>${escapeText(source.doi || source.url || source.source_kind || "")}</span>
        </div>
      `).join("")}</div>`
    : `<div class="empty-state">No source cards recorded for this job.</div>`;

  return `
    <div class="research-job-card">
      <div class="research-job-head">
        <div>
          <h3>${escapeText(job.query || "Research job")}</h3>
          <div class="research-meta">
            <span>${escapeText(job.domain || "—")}</span>
            <span>${escapeText(job.route || "planning")}</span>
            <span>${humanTime(job.updated_at || job.created_at)}</span>
          </div>
        </div>
        <span class="pill ${researchStatusKind(job.status)}">${escapeText(job.status || "unknown")}</span>
      </div>
      <div class="research-meta">
        <span>Verification: ${escapeText(verification.status || "pending")}</span>
        <span>Evidence: ${escapeText(verification.independent_evidence_count ?? 0)}</span>
        <span>API: ${escapeText(metrics.api_requests ?? 0)}</span>
        <span>Model: ${escapeText(metrics.metered_model_requests ?? 0)}</span>
        <span>Paid: ${escapeText(metrics.paid_requests ?? 0)}</span>
      </div>
      ${job.answer ? `<div class="research-answer">${escapeText(job.answer)}</div>` : ""}
      ${sourceHtml}
    </div>
  `;
}

function renderResearchStatus(data) {
  researchStatus = data;
  const jobs = data.jobs || [];
  const active = jobs.find((job) =>
    ["queued", "running"].includes(job.status)
  );
  const latest = jobs[0] || null;

  $("search-free-confirm").checked =
    !!data.gemini_search_zero_cost_confirmed;

  $("research-policy-detail").innerHTML = detailRows([
    ["Explicit-only", data.explicit_only ? "Yes" : "No"],
    ["Free-first", data.free_first ? "Yes" : "No"],
    ["Scholarly", data.scholarly_route || "—"],
    ["Biomedical", data.biomedical_route || "—"],
    ["General web", data.general_web_route || "—"],
    ["Paid authority", data.paid_spending_authority ? "Enabled" : "None"],
  ]);

  const current = active || latest;
  $("research-current").innerHTML = current
    ? `
      <div class="research-progress-row">
        <strong>${escapeText(current.progress || current.status || "—")}</strong>
        <span>${escapeText(current.progress_percent ?? 0)}%</span>
      </div>
      <div class="progress-track">
        <div class="progress-bar" style="width:${Math.max(0, Math.min(100, Number(current.progress_percent || 0)))}%"></div>
      </div>
      <div class="research-meta">
        <span>${escapeText(current.domain || "—")}</span>
        <span>${escapeText(current.route || "planning")}</span>
        <span>${escapeText(current.status || "—")}</span>
      </div>
    `
    : "No research jobs yet.";

  $("research-history").innerHTML = jobs.length
    ? jobs.slice(0, 12).map(renderResearchJobCard).join("")
    : `<div class="empty-state">No explicit research history yet.</div>`;

  const progress = $("chat-research-progress");
  if (active) {
    progress.classList.remove("hidden");
    progress.innerHTML = `
      <div class="research-progress-row">
        <strong>Research: ${escapeText(active.progress || "running")}</strong>
        <span>${escapeText(active.progress_percent ?? 0)}%</span>
      </div>
      <div class="progress-track">
        <div class="progress-bar" style="width:${Math.max(0, Math.min(100, Number(active.progress_percent || 0)))}%"></div>
      </div>
    `;
  } else {
    progress.classList.add("hidden");
    progress.innerHTML = "";
  }
}

async function refreshResearchStatus() {
  const data = await api("/api/research/status");
  renderResearchStatus(data);
  const job = (data.jobs || [])[0];
  const capability = (overview?.core?.capabilities || []).find(row => row.name === "research");
  if (capability?.state === "unverified" && job?.verification?.status === "verified_multi_evidence") {
    $("core-research-capability").textContent = "Overall Research capability: unverified • Verified research-job evidence exists; broader capability threshold is not satisfied";
  }
  $("core-research-title").textContent = job ? `${job.status} • ${job.domain || "research"}` : "No recent research";
  $("core-research").innerHTML = detailRows([
    ["Job", job?.job_id || "—"], ["Progress", job?.progress || "—"],
    ["Evidence", job?.verification?.status || "not acquired"],
    ["Route", job?.route || "—"],
  ]);
  $("core-research-state").textContent = job?.verification?.status || "No result";
  return data;
}

function stopResearchPolling() {
  if (researchPollTimer !== null) {
    clearInterval(researchPollTimer);
    researchPollTimer = null;
  }
}

function startResearchPolling(jobId) {
  watchedResearchJobId = jobId;
  stopResearchPolling();
  researchPollTimer = setInterval(async () => {
    try {
      const data = await refreshResearchStatus();
      const job = (data.jobs || []).find(
        (row) => row.job_id === watchedResearchJobId
      );
      if (
        job &&
        !["queued", "running"].includes(job.status)
      ) {
        stopResearchPolling();
        await refreshChat();
        await refreshOverview();
        toast(
          job.status === "completed"
            ? "Research job completed — check verification"
            : `Research finished: ${job.status}`
        );
      }
    } catch (error) {
      stopResearchPolling();
      toast(error.message);
    }
  }, 1400);
}

async function switchView(name) {
  navItems.forEach((item) => item.classList.toggle("active", item.dataset.view === name));
  views.forEach((view) => view.classList.toggle("active", view.id === "view-" + name));
  $("page-title").textContent = name === "health" ? "Tests & Health" : name.charAt(0).toUpperCase() + name.slice(1);
  $("page-subtitle").textContent = subtitles[name] || "";
  if (name === "activity") await refreshActivity();
  if (name === "notifications") await refreshNotifications();
  if (name === "chat") {
    await refreshChat();
    await refreshResearchStatus();
  }
  if (name === "research") await refreshResearchStatus();
}

async function sendChat(message, research = false) {
  const result = await api("/api/chat", {
    method:"POST",
    body:JSON.stringify({message, research})
  });

  if (
    result.schema === "sira.desktop_research_start.v1"
  ) {
    await refreshChat();
    await refreshResearchStatus();
    const job = result.job || {};
    if (result.status === "started" && job.job_id) {
      $("chat-response-meta").textContent =
        "Explicit research started • free-first routing";
      startResearchPolling(job.job_id);
      toast("Research job started");
    } else if (result.status === "blocked") {
      $("chat-response-meta").textContent =
        `SIRA Core: ${result.core_task?.reason || "research blocked"}`;
      toast("Research blocked by current Core policy");
    } else {
      $("chat-response-meta").textContent =
        "Research already running";
      if (job.job_id) startResearchPolling(job.job_id);
    }
    return result;
  }

  await refreshChat();
  const model = result.model || {};
  const core = result.core_task || {};
  if (result.mode === "model") {
    $("chat-response-meta").textContent =
      `SIRA Core: ${core.route || "guarded model"} • Model reply${model.needs_research ? " • fresh research recommended" : ""}`;
  } else if (result.mode === "local_operational") {
    $("chat-response-meta").textContent =
      `SIRA Core: ${core.route || "status"} • local, zero model requests`;
  } else if (result.mode === "local_verified_knowledge") {
    $("chat-response-meta").textContent =
      `SIRA Core: ${core.route || "verified memory"} • ${(result.provenance || []).length} cited claim(s) • zero model requests`;
  } else if (result.mode?.startsWith("local_")) {
    $("chat-response-meta").textContent =
      `SIRA Core: ${core.route || "local"} • ${core.status || "completed"}`;
  } else {
    $("chat-response-meta").textContent =
      `Model guarded/failed • ${model.status || "blocked"}`;
  }
  await refreshOverview();
  return result;
}


navItems.forEach((item) => item.addEventListener("click", () => switchView(item.dataset.view)));
document.querySelectorAll("[data-view-jump]").forEach((item) =>
  item.addEventListener("click", () => switchView(item.dataset.viewJump))
);

$("refresh-btn").addEventListener("click", async () => {
  try { await refreshOverview(); toast("Dashboard refreshed"); } catch (error) { toast(error.message); }
});

$("runtime-btn").addEventListener("click", async () => {
  const button = $("runtime-btn");
  button.disabled = true;
  try {
    await api(runtimeIsOn() ? "/api/runtime/stop" : "/api/runtime/start", {method:"POST", body:"{}"});
    await new Promise((resolve) => setTimeout(resolve, 550));
    await refreshOverview();
    toast(runtimeIsOn() ? "SIRA is running" : "SIRA is stopped");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});

async function runRelease() {
  try {
    const result = await api("/api/release/refresh", {method:"POST", body:"{}"});
    await refreshOverview();
    toast(result.release_ready ? "Release check passed" : "Release check needs attention");
  } catch (error) { toast(error.message); }
}
$("release-refresh").addEventListener("click", runRelease);

document.querySelectorAll("[data-quick]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (button.dataset.quick === "release") await runRelease();
    else { await refreshOverview(); toast("Status refreshed"); }
  });
});

$("deliver-btn").addEventListener("click", async () => {
  try {
    await api("/api/notifications/deliver", {method:"POST", body:"{}"});
    await refreshNotifications(); await refreshOverview(); toast("Notification delivery cycle completed");
  } catch (error) { toast(error.message); }
});

$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  try { await sendChat(message); } catch (error) { toast(error.message); }
});

$("research-send-btn").addEventListener("click", async () => {
  const input = $("chat-input");
  const message = input.value.trim();
  if (!message) {
    toast("Enter a research question first");
    return;
  }
  input.value = "";
  const button = $("research-send-btn");
  button.disabled = true;
  try {
    await sendChat(message, true);
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("dashboard-chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("dashboard-chat-input");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";
  try { await sendChat(message); } catch (error) { toast(error.message); }
});

$("save-chat-settings").addEventListener("click", async () => {
  const button = $("save-chat-settings");
  button.disabled = true;
  try {
    const confirmed = $("free-tier-confirm").checked;
    const result = await api("/api/chat/settings", {
      method: "POST",
      body: JSON.stringify({
        free_tier_confirmed: confirmed
      }),
    });
    renderChatStatus(result.status);
    toast(
      confirmed
        ? "Zero-cost model-use confirmation saved"
        : "Model-backed chat confirmation disabled"
    );
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("save-research-settings").addEventListener("click", async () => {
  const button = $("save-research-settings");
  button.disabled = true;
  try {
    const confirmed = $("search-free-confirm").checked;
    const result = await api("/api/research/settings", {
      method: "POST",
      body: JSON.stringify({
        search_zero_cost_confirmed: confirmed
      }),
    });
    renderResearchStatus(result.status);
    toast(
      confirmed
        ? "Zero-cost Search confirmation saved"
        : "General web Search confirmation disabled"
    );
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

async function boot() {
  updateClock();
  setInterval(updateClock, 1000);
  // Local-only snapshot. Skip hidden tabs and never poll external providers here.
  setInterval(() => {
    if (document.visibilityState === "visible") refreshOverview().catch(() => {});
  }, 20000);
  try {
    const session = await api("/api/session");
    sessionToken = session.token;
    await refreshOverview();
    await refreshChat();
    await refreshNotifications();
    const research = await refreshResearchStatus();
    const active = (research.jobs || []).find(
      (job) => ["queued", "running"].includes(job.status)
    );
    if (active?.job_id) startResearchPolling(active.job_id);
  } catch (error) {
    toast("Desktop connection failed: " + error.message);
  }
}
boot();
