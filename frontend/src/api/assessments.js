// ============================================================================
// Assessments — DPIA (§10), RoPA, vendor reviews, application discovery.
//
// Three capabilities, and it is worth knowing which is which when a control
// disappears: `assessment:read` sees, `assessment:respond` answers,
// `assessment:approve` signs off and manages templates. An auditor has read
// only, which is right — a DPIA is exactly the document an audit inspects, and
// inspecting it changes nothing.
//
// TWO THINGS THE BROWSER DOES NOT DECIDE
//
// Progress. `progress.outstanding` comes from the server, which evaluates the
// conditional questions before deciding what is outstanding. Computing it here
// would mean reimplementing `show_if` in two places and eventually disagreeing
// — and the disagreement shows up as an assessment stuck at 90% that nobody
// can submit.
//
// Whether an answer is valid. A boolean question holding the string "maybe" is
// a record that means nothing, and these records get exported and relied on, so
// the server checks each value against its question type.
// ============================================================================
import { apiFetch, apiUpload } from "./auth";

// --------------------------------------------------------------- templates --

export function templates({ publishedOnly = false } = {}) {
  const q = publishedOnly ? "?published_only=true" : "";
  return apiFetch(`/assessments/templates${q}`);
}

export function template(id) {
  return apiFetch(`/assessments/templates/${id}`);
}

/** Install the questionnaires this product ships with. Idempotent. */
export function seedTemplates() {
  return apiFetch("/assessments/templates/seed", { method: "POST" });
}

export function createTemplate({ slug, kind, name, description }) {
  return apiFetch("/assessments/templates", {
    method: "POST",
    body: { slug, kind, name, description: description || null },
  });
}

export function addQuestion(templateId, q) {
  return apiFetch(`/assessments/templates/${templateId}/questions`, {
    method: "POST",
    body: {
      prompt: q.prompt,
      type: q.type || "long_text",
      section: q.section || null,
      helper_text: q.helperText || null,
      required: Boolean(q.required),
      options: q.options || [],
      reportable: Boolean(q.reportable),
      show_if_question: q.showIfQuestion || null,
      show_if_equals: q.showIfEquals ?? null,
    },
  });
}

export function deleteQuestion(templateId, questionId) {
  return apiFetch(
    `/assessments/templates/${templateId}/questions/${questionId}`,
    { method: "DELETE" },
  );
}

/** Freeze a questionnaire so it can be run. Irreversible for that version. */
export function publishTemplate(id) {
  return apiFetch(`/assessments/templates/${id}/publish`, { method: "POST" });
}

/**
 * Copy a published questionnaire into an editable next version.
 *
 * This is how a published template is "edited". Editing in place would rewrite
 * the question every existing answer was given to, with no way to tell
 * afterwards that it happened.
 */
export function newVersion(id) {
  return apiFetch(`/assessments/templates/${id}/new-version`, {
    method: "POST",
  });
}

// ------------------------------------------------------------- assessments --

export function assessments({ status, mine } = {}) {
  const q = new URLSearchParams();
  if (status) q.set("status", status);
  if (mine) q.set("mine", "true");
  return apiFetch(`/assessments?${q}`);
}

export function assessment(id) {
  return apiFetch(`/assessments/${id}`);
}

export function createAssessment(body) {
  return apiFetch("/assessments", {
    method: "POST",
    body: {
      template_id: body.templateId,
      name: body.name,
      description: body.description || null,
      manager_user_id: body.managerUserId || null,
      subject_type: body.subjectType || null,
      subject_id: body.subjectId || null,
      subject_label: body.subjectLabel || null,
      due_at: body.dueAt || null,
      review_every_days: body.reviewEveryDays || null,
    },
  });
}

/** Record or change one answer. Returns the fresh progress with it. */
export function saveAnswer(assessmentId, questionId, { value, note }) {
  return apiFetch(`/assessments/${assessmentId}/answers/${questionId}`, {
    method: "PUT",
    body: { value: value ?? null, note: note || null },
  });
}

export function assignQuestion(assessmentId, questionId, assigneeUserId) {
  return apiFetch(
    `/assessments/${assessmentId}/answers/${questionId}/assign`,
    { method: "PATCH", body: { assignee_user_id: assigneeUserId || null } },
  );
}

export function uploadEvidence(assessmentId, questionId, file) {
  return apiUpload(
    `/assessments/${assessmentId}/answers/${questionId}/evidence`,
    file,
  );
}

export function submitAssessment(id) {
  return apiFetch(`/assessments/${id}/submit`, { method: "POST" });
}

/** Sign it off. The conclusion is required — for a DPIA it is the output. */
export function approveAssessment(id, conclusion) {
  return apiFetch(`/assessments/${id}/approve`, {
    method: "POST",
    body: { conclusion },
  });
}

export function rejectAssessment(id, reason) {
  return apiFetch(`/assessments/${id}/reject`, {
    method: "POST",
    body: { reason },
  });
}

// ------------------------------------------------------------------ labels --

export const KIND_LABEL = {
  dpia: "DPIA",
  ropa: "RoPA",
  lia: "Legitimate interests",
  tia: "Transfer impact",
  vendor: "Vendor review",
  discovery: "Discovery survey",
  custom: "Custom",
};

export const STATUS_TONE = {
  draft: "neutral",
  in_progress: "info",
  in_review: "warning",
  approved: "success",
  rejected: "danger",
  archived: "neutral",
};

export const STATUS_LABEL = {
  draft: "Draft",
  in_progress: "Being answered",
  in_review: "Awaiting sign-off",
  approved: "Approved",
  rejected: "Sent back",
  archived: "Archived",
};
