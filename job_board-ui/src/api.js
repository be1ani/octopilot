async function fetchJson(path, options) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {})
    }
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.error || res.statusText || "Request failed";
    const err = new Error(msg);
    err.status = res.status;
    err.path = path;
    err.data = data;
    throw err;
  }
  return data;
}

export const jobBoardApi = {
  health: () => fetchJson("/job-board-api/api/health"),
  listJobs: () => fetchJson("/job-board-api/api/jobs"),
  createJob: (job) =>
    fetchJson("/job-board-api/api/jobs", {
      method: "POST",
      body: JSON.stringify(job)
    }),
  deleteJob: (id) => fetchJson(`/job-board-api/api/jobs/${id}`, { method: "DELETE" }),
  listApplications: (jobId) => {
    const qs = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
    return fetchJson(`/job-board-api/api/applications${qs}`);
  },
  listQueue: (status) => {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    return fetchJson(`/job-board-api/api/queue${qs}`);
  },
  createQueueItem: ({ job_id, profile_id, priority }) =>
    fetchJson("/job-board-api/api/queue", {
      method: "POST",
      body: JSON.stringify({ job_id, profile_id, priority })
    }),
  updateQueueItem: (qid, patch) =>
    fetchJson(`/job-board-api/api/queue/${qid}`, {
      method: "PATCH",
      body: JSON.stringify(patch)
    }),
  deleteQueueItem: (qid) =>
    fetchJson(`/job-board-api/api/queue/${qid}`, { method: "DELETE" })
};

export const orchApi = {
  listProfiles: () => fetchJson(`/orch-api/api/profiles`),
  enqueue: ({ url, profile_id, job_title, job_company, job_city }) =>
    fetchJson("/orch-api/api/machines", {
      method: "POST",
      body: JSON.stringify({ url, profile_id, job_title, job_company, job_city })
    }),
  getSettings: () => fetchJson("/orch-api/api/settings"),
  updateSettings: (patch) =>
    fetchJson("/orch-api/api/settings", {
      method: "PATCH",
      body: JSON.stringify(patch)
    }),
  listApplications: (limit = 500) => fetchJson(`/orch-api/api/applications?limit=${limit}`)
};

