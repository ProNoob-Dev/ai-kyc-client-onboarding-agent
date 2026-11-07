/* KYC & Client Onboarding dashboard — vanilla JS, talks to the FastAPI backend.
   No build step, no external dependencies. */

const API = "";

/* ---------- helpers ---------- */
const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function statusBadge(status) {
  const labels = {
    ready: "✓ Ready",
    pending_review: "Pending review",
    approved: "✓ Approved",
    rejected: "✕ Rejected",
    info_requested: "Info requested",
    submitted: "Submitted",
  };
  const label = labels[status] || status;
  return `<span class="badge badge-${esc(status)}">${esc(label)}</span>`;
}

function decisionLabel(decision) {
  const labels = {
    pass_through: "Pass through",
    needs_review: "Needs review",
    propose_reject: "Propose reject",
  };
  return labels[decision] || decision || "—";
}

function docTypeLabel(docType) {
  const labels = {
    id_proof: "ID proof",
    incorporation: "Incorporation",
    address_proof: "Address proof",
    unknown: "Unknown",
  };
  return labels[docType] || docType || "—";
}

async function api(path, options) {
  const res = await fetch(API + path, options);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
  }
  return res.json();
}

/* ---------- state ---------- */
let selectedCaseId = null;

/* ---------- metrics ---------- */
async function loadMetrics() {
  try {
    const m = await api("/metrics");
    $("#m-total").textContent = m.total_cases;
    $("#m-total-sub").textContent =
      `${m.ready} ready · ${m.approved_by_reviewer} approved by reviewer`;
    $("#m-pass").textContent = `${Math.round(m.pass_through_rate * 100)}%`;
    $("#m-avg-ms").textContent = `${m.avg_processing_ms} ms`;
    $("#m-pending").textContent = m.pending_review;
  } catch (err) {
    console.error("metrics failed", err);
  }
}

async function loadServiceInfo() {
  try {
    const info = await api("/health");
    $("#llm-badge").textContent = `LLM: ${info.llm_provider || "?"}`;
  } catch (err) {
    $("#llm-badge").textContent = "LLM: ?";
  }
}

/* ---------- cases list + queue ---------- */
async function loadCases() {
  const status = $("#filter-status").value;
  const type = $("#filter-type").value;
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (type) params.set("client_type", type);
  const query = params.toString();
  let cases = [];
  try {
    cases = await api(`/onboarding${query ? "?" + query : ""}`);
  } catch (err) {
    $("#cases-body").innerHTML =
      `<tr><td colspan="8" class="muted">Failed to load cases: ${esc(err.message)}</td></tr>`;
    return;
  }
  const body = $("#cases-body");
  if (!cases.length) {
    body.innerHTML = `<tr><td colspan="8" class="muted">No cases yet — create one above.</td></tr>`;
    return;
  }
  body.innerHTML = cases
    .map(
      (c) => `<tr data-id="${esc(c.id)}" class="${c.id === selectedCaseId ? "selected" : ""}">
        <td><span class="mono">${esc(c.id)}</span></td>
        <td>${esc(c.client_name)}</td>
        <td>${esc(c.client_type)}</td>
        <td>${c.documents_count}</td>
        <td>${c.mismatches_count}${c.requires_hitl ? " ⚠️" : ""}</td>
        <td>${esc(decisionLabel(c.decision))}</td>
        <td>${statusBadge(c.status)}</td>
        <td>${esc(new Date(c.created_at).toLocaleString())}</td>
      </tr>`
    )
    .join("");
  body.querySelectorAll("tr[data-id]").forEach((row) => {
    row.addEventListener("click", () => selectCase(row.dataset.id));
  });

  // Keep the upload form's case selector in sync.
  const select = $("#document-case-select");
  const current = select.value;
  select.innerHTML = cases
    .map((c) => `<option value="${esc(c.id)}">${esc(c.client_name)} (${esc(c.id)})</option>`)
    .join("");
  if (current && cases.some((c) => c.id === current)) select.value = current;
}

async function loadQueue() {
  let pending = [];
  try {
    pending = await api("/onboarding/review-queue");
  } catch (err) {
    console.error("queue failed", err);
  }
  $("#queue-count").textContent = pending.length;
  const list = $("#queue-list");
  if (!pending.length) {
    list.innerHTML = `<li class="muted">Queue is clear — nothing needs a human right now.</li>`;
    return;
  }
  list.innerHTML = pending
    .map(
      (c) => `<li data-id="${esc(c.id)}">
        <span><span class="q-id">${esc(c.id)}</span> · ${esc(c.client_name)}<br>
        <span class="q-meta">${esc(c.client_type)} · ${c.mismatches_count} mismatch(es) ·
        ${esc(decisionLabel(c.decision))}</span></span>
        <span><span class="q-sev">${c.mismatches_count} ⚠️</span> ${statusBadge(c.status)}</span>
      </li>`
    )
    .join("");
  list.querySelectorAll("li[data-id]").forEach((li) => {
    li.addEventListener("click", () => selectCase(li.dataset.id));
  });
}

/* ---------- case detail ---------- */
async function selectCase(id) {
  selectedCaseId = id;
  const card = $("#detail-card");
  card.hidden = false;
  $("#detail-title").textContent = `Case ${id}`;
  $("#detail-body").innerHTML = `<p class="muted">Loading…</p>`;
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const [detail, audit] = await Promise.all([
      api(`/onboarding/${id}/status`),
      api(`/audit?case_id=${id}`),
    ]);
    const full = await api(`/onboarding/${id}`);
    renderDetail(detail, full, audit);
    loadCases(); // refresh row highlight
  } catch (err) {
    $("#detail-body").innerHTML =
      `<p class="form-status err">Failed to load case: ${esc(err.message)}</p>`;
  }
}

function renderDetail(detail, full, audit) {
  const docs = detail.documents || [];
  const mismatches = detail.mismatches || [];
  const citations = detail.citations || [];
  const trace = full.trace || [];
  const ef = detail.extracted_fields || {};

  // Verification checklist: required docs, present or missing.
  const presentTypes = new Set(docs.map((d) => d.doc_type));
  const checklist = (detail.required_documents || [])
    .map((req) => {
      const doc = docs.find((d) => d.doc_type === req);
      const done = Boolean(doc);
      return `<li>
        <span class="check-icon ${done ? "done" : "missing"}">${done ? "✓" : "○"}</span>
        <span><span class="doc-type">${esc(docTypeLabel(req))}</span>
        ${doc ? `<span class="doc-name"> — ${esc(doc.name)}</span>` : `<span class="doc-name"> — missing</span>`}</span>
      </li>`;
    })
    .join("");

  const docCards = docs.length
    ? docs.map((d) => {
        const fields = d.extracted_fields || {};
        const fieldRows = Object.entries(fields)
          .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`)
          .join("");
        return `<div class="doc-card">
          <h4>${esc(d.name)}</h4>
          <div class="doc-meta">
            ${esc(docTypeLabel(d.doc_type))} · confidence ${Number(d.doc_type_confidence || 0).toFixed(2)} ·
            ${esc(d.status)}
          </div>
          ${fieldRows ? `<dl class="kv">${fieldRows}</dl>` : `<p class="muted">No fields extracted.</p>`}
        </div>`;
      }).join("")
    : "<p class='muted'>No documents uploaded yet.</p>";

  const mismatchCards = mismatches.length
    ? mismatches.map((m) => `<div class="mismatch-card ${m.field === "document_expiry" ? "critical" : ""}">
        <h4>${esc(m.field)} <span class="badge badge-${esc(m.severity)}">${esc(m.severity)}</span></h4>
        <p class="muted">${esc(m.detail)}</p>
        <div class="expected-found">
          <div><div class="col-label">Expected</div><div class="value">${esc(m.expected || "—")}</div></div>
          <div><div class="col-label">Found — discrepancy</div><div class="value found">${esc(m.found || "—")}</div></div>
        </div>
      </div>`).join("")
    : "<p class='muted'>No mismatches — all fields consistent.</p>";

  const reviewBox =
    detail.status === "pending_review"
      ? `<div class="review-box">
          <strong>Human review required</strong> — ${esc(decisionLabel(detail.decision))} was proposed by the supervisor.
          <div class="row">
            <input id="reviewer-name" type="text" placeholder="Your name (reviewer)" value="kyc-officer-1">
            <input id="review-note" type="text" placeholder="Note (optional)">
            <button class="btn btn-approve" data-review="approve" type="button">✓ Approve</button>
            <button class="btn btn-danger" data-review="reject" type="button">✕ Reject</button>
            <button class="btn btn-warn" data-review="request_info" type="button">? Request info</button>
          </div>
          <p id="review-status" class="form-status" role="status"></p>
        </div>`
      : full.reviewer
        ? `<p><strong>Reviewed by ${esc(full.reviewer)}</strong>${full.review_note ? ` — ${esc(full.review_note)}` : ""}</p>`
        : "";

  $("#detail-body").innerHTML = `
    <p>
      ${statusBadge(detail.status)}
      <span class="badge badge-grey">${esc(detail.client_type)}</span>
      ${detail.requires_hitl ? `<span class="badge badge-amber">Needs human</span>` : ""}
      <span class="badge badge-grey">${esc(decisionLabel(detail.decision))}</span>
    </p>
    <div class="detail-grid">
      <div>
        <h3>Verification checklist</h3>
        <ul class="checklist">${checklist}</ul>
        <h3 style="margin-top:1.25rem">Extracted fields (merged)</h3>
        ${Object.keys(ef).length
          ? `<dl class="kv">${Object.entries(ef).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join("")}</dl>`
          : "<p class='muted'>Nothing extracted yet.</p>"}
        <h3 style="margin-top:1.25rem">Documents</h3>
        ${docCards}
      </div>
      <div>
        <h3>Mismatches</h3>
        ${mismatchCards}
        <h3 style="margin-top:1.25rem">Requirement citations</h3>
        ${citations.length
          ? citations.map((c) => `<div class="citation"><strong>${esc(c.rule_id)}</strong> — ${esc(c.rule_title)} <span class="muted">(score ${c.score})</span><br>${esc(c.excerpt)}</div>`).join("")
          : "<p class='muted'>No citations.</p>"}
        <h3 style="margin-top:1.25rem">Agent trace</h3>
        <ul class="trace">
          ${trace.map((t) => `<li><span class="agent">${esc(t.agent)}</span> · ${esc(t.action)}<br>${esc(t.detail)}</li>`).join("")}
        </ul>
      </div>
    </div>
    <h3 style="margin-top:1rem">Audit trail</h3>
    <ul class="audit">
      ${audit.length
        ? audit.map((a) => `<li><span class="actor">${esc(a.actor)}</span> — ${esc(a.action)}<br><span class="muted">${esc(a.detail)}</span></li>`).join("")
        : "<li class='muted'>No audit entries.</li>"}
    </ul>
    ${reviewBox}`;

  document.querySelectorAll("#detail-body [data-review]").forEach((btn) => {
    btn.addEventListener("click", () => submitReview(detail.id, btn.dataset.review));
  });
}

async function submitReview(caseId, action) {
  const reviewer = ($("#reviewer-name") || {}).value || "reviewer";
  const note = ($("#review-note") || {}).value || "";
  const statusEl = $("#review-status");
  statusEl.textContent = "Submitting review…";
  statusEl.className = "form-status";
  try {
    await api(`/onboarding/${caseId}/review/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reviewer, note }),
    });
    await refreshAll();
    await selectCase(caseId);
  } catch (err) {
    statusEl.textContent = `Review failed: ${err.message}`;
    statusEl.className = "form-status err";
  }
}

/* ---------- forms ---------- */
function readFilesAsText(files) {
  const readers = [...files].map(
    (file) =>
      new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve({ name: file.name, content: String(reader.result || "") });
        reader.onerror = () => resolve({ name: file.name, content: "" });
        reader.readAsText(file);
      })
  );
  return Promise.all(readers);
}

async function submitCaseForm(event) {
  event.preventDefault();
  const form = event.target;
  const statusEl = $("#case-form-status");
  statusEl.textContent = "Creating case…";
  statusEl.className = "form-status";
  const data = new FormData(form);
  const payload = {
    client_name: data.get("client_name"),
    client_type: data.get("client_type"),
    country: data.get("country") || "India",
    declared_registration_number: (data.get("declared_registration_number") || "").trim() || null,
    declared_address: (data.get("declared_address") || "").trim() || null,
  };
  try {
    const created = await api("/onboarding", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    statusEl.textContent = `Case created: ${created.id} — now upload its documents.`;
    statusEl.className = "form-status ok";
    form.reset();
    await refreshAll();
    const select = $("#document-case-select");
    select.value = created.id;
    await selectCase(created.id);
  } catch (err) {
    statusEl.textContent = `Failed: ${err.message}`;
    statusEl.className = "form-status err";
  }
}

async function submitDocumentForm(event) {
  event.preventDefault();
  const form = event.target;
  const statusEl = $("#document-form-status");
  const data = new FormData(form);
  const caseId = data.get("case_id");
  if (!caseId) {
    statusEl.textContent = "Pick a case first.";
    statusEl.className = "form-status err";
    return;
  }
  statusEl.textContent = "Verifying document…";
  statusEl.className = "form-status";

  const docName = (data.get("name") || "").trim();
  const docText = (data.get("content") || "").trim();
  const hint = data.get("doc_type_hint") || null;
  const uploads = [];
  if (docText) uploads.push({ name: docName || "document.txt", content: docText, doc_type_hint: hint });
  const files = $("#document-files").files;
  if (files && files.length) {
    const read = await readFilesAsText(files);
    read.filter((d) => d.content.trim()).forEach((d) => uploads.push({ ...d, doc_type_hint: hint }));
  }
  if (!uploads.length) {
    statusEl.textContent = "Paste document text or upload a file.";
    statusEl.className = "form-status err";
    return;
  }
  try {
    let result = null;
    for (const doc of uploads) {
      result = await api(`/onboarding/${caseId}/documents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(doc),
      });
    }
    statusEl.textContent =
      `Verified: ${result.id} → ${decisionLabel(result.decision)} (${result.status}).`;
    statusEl.className = "form-status ok";
    form.reset();
    await refreshAll();
    await selectCase(caseId);
  } catch (err) {
    statusEl.textContent = `Failed: ${err.message}`;
    statusEl.className = "form-status err";
  }
}

/* ---------- boot ---------- */
async function refreshAll() {
  await Promise.all([loadMetrics(), loadCases(), loadQueue()]);
}

document.addEventListener("DOMContentLoaded", () => {
  loadServiceInfo();
  refreshAll();
  $("#refresh-btn").addEventListener("click", refreshAll);
  $("#case-form").addEventListener("submit", submitCaseForm);
  $("#document-form").addEventListener("submit", submitDocumentForm);
  $("#filter-status").addEventListener("change", loadCases);
  $("#filter-type").addEventListener("change", loadCases);
  $("#detail-close").addEventListener("click", () => {
    $("#detail-card").hidden = true;
    selectedCaseId = null;
    loadCases();
  });
  setInterval(refreshAll, 30000); // refresh every 30s
});
