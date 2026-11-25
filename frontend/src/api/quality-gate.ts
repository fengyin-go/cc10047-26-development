import { fetchJson, request } from './client'

export type PartitionSample = {
  partition: string
  valid_rows: number
  total_rows: number
  threshold: number
  rule?: string
  window_id?: string
  source_refs?: string[]
  check_id?: number | string
}

export type Verdict = {
  id: number
  check_id: number | string
  partition: string
  window_id: string
  threshold: number
  rule: string
  valid_rows: number
  total_rows: number
  valid_rate: number
  passed: boolean
  decision_reason: string
  source_refs: string[]
  sample_snapshot: Record<string, unknown>
  window_fingerprint: string
  version: number
  status: string
  history: Array<Record<string, unknown>>
  conflict_with_published: boolean
  reason: string
  created_at: string
}

export type PublishRecord = {
  id: number
  commit_id: number
  scope_id: number
  partition: string
  verdict_id: number
  published: boolean
  block_reason: string | null
  threshold: number
  rule: string
  valid_rows: number
  total_rows: number
  valid_rate: number
  sample_window_id: string
  sample_snapshot: Record<string, unknown>
  source_refs: string[]
  write_count: number
  created_at: string
}

export type ReviewTodo = {
  id: number
  todo_type: 'published' | 'blocked' | 'new_evidence'
  status: 'open' | 'resolved'
  partition: string
  commit_id: number | null
  scope_id: number | null
  verdict_id: number | null
  sample_window_id: string | null
  threshold: number | null
  valid_rows: number
  total_rows: number
  valid_rate: number
  passed: boolean
  source_refs: string[]
  conflict: Record<string, unknown> | null
  message: string
  resolution: string | null
  created_at: string
  resolved_at: string | null
}

type SubmitResult = {
  ok: boolean
  message: string
  status?: string
  scope_id?: number
  commit_id?: number
  requested_partitions?: string[]
  pending_partitions?: string[]
  published_partitions?: string[]
  blocked_partitions?: string[]
  actual_publish_range?: string[]
  sample_window_ids?: Record<string, string>
  write_count?: number
  idempotent_replay?: boolean
  skipped_already_published?: string[]
  missing_partitions?: string[]
}

export type CommitDetail = {
  ok: boolean
  message?: string
  commit_id: number
  requested_partitions: string[]
  actual_publish_range: string[]
  blocked_partitions: string[]
  scopes: Array<Record<string, unknown>>
  partitions: PublishRecord[]
}

export type ReviewTodoList = {
  ok: boolean
  items: ReviewTodo[]
  summary: Record<'published' | 'blocked' | 'new_evidence', { open: number; resolved: number; total: number }>
  labels: Record<string, string>
}

export async function evaluateVerdicts(checkId: number | string, samples: PartitionSample[]) {
  const response = await request('/api/quality-gate/verdicts', {
    method: 'POST',
    body: JSON.stringify({ check_id: checkId, samples }),
  })
  return (await response.json()) as {
    ok: boolean
    message?: string
    publishable_partitions: string[]
    verdicts: Verdict[]
  }
}

export async function listVerdicts(partition?: string) {
  const query = partition ? `?partition=${encodeURIComponent(partition)}` : ''
  return fetchJson<{ ok: boolean; items: Verdict[] }>(`/api/quality-gate/verdicts${query}`)
}

export async function correctVerdict(payload: {
  partition: string
  valid_rows: number
  total_rows: number
  note?: string
  source_refs?: string[]
}) {
  const response = await request('/api/quality-gate/verdicts/correct', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return (await response.json()) as {
    ok: boolean
    message: string
    verdict: Verdict
    previous_verdict?: Verdict
    conflict: { conflict: boolean; changed_fields: string[] } | null
  }
}

export async function submitCommit(
  commitId: number,
  partitions: string[],
  options: { idempotencyKey?: string; failAfterScope?: boolean } = {},
) {
  const response = await request(`/api/quality-gate/commits/${commitId}/submit`, {
    method: 'POST',
    body: JSON.stringify({
      partitions,
      idempotency_key: options.idempotencyKey,
      fail_after_scope: options.failAfterScope ?? false,
    }),
  })
  return (await response.json()) as SubmitResult
}

export async function retryCommit(commitId: number, partitions: string[], idempotencyKey?: string) {
  const response = await request(`/api/quality-gate/commits/${commitId}/retry`, {
    method: 'POST',
    body: JSON.stringify({ partitions, idempotency_key: idempotencyKey }),
  })
  return (await response.json()) as SubmitResult
}

export async function resumeScope(scopeId: number) {
  const response = await request(`/api/quality-gate/scopes/${scopeId}/resume`, { method: 'POST' })
  return (await response.json()) as SubmitResult
}

export async function getCommitDetail(commitId: number) {
  return fetchJson<CommitDetail>(`/api/quality-gate/commits/${commitId}/detail`)
}

export async function listReviewTodos() {
  return fetchJson<ReviewTodoList>('/api/quality-gate/review-todos')
}

export async function resolveReviewTodo(todoId: number, resolution: string) {
  const response = await request(`/api/quality-gate/review-todos/${todoId}/resolve`, {
    method: 'POST',
    body: JSON.stringify({ resolution }),
  })
  return (await response.json()) as { ok: boolean; message?: string }
}
