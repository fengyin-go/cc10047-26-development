<template>
  <section class="page quality-gate" data-testid="quality-gate">
    <header class="page-head">
      <div>
        <h2>摄入质量门禁</h2>
        <p class="page-desc">
          来源分区样本按采样窗口阈值独立判定，提交版本只发布合格分区；迟到更正产生新证据，
          已发布分区不会二次写入。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-all" type="button" @click="refreshAll">刷新</button>
        <button class="btn ghost" data-testid="reset-demo" type="button" @click="resetDemo">重置演练</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">可发布分区</span>
        <strong class="stat-value">{{ publishableCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">实际发布范围</span>
        <strong class="stat-value">{{ detail.actual_publish_range.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">被阻断范围</span>
        <strong class="stat-value stat-warn">{{ detail.blocked_partitions.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">新证据待办</span>
        <strong class="stat-value stat-warn">{{ todos.summary?.new_evidence?.open ?? 0 }}</strong>
      </article>
    </div>

    <p v-if="message" class="gate-message" :class="messageKind" data-testid="gate-message">
      {{ message }}
    </p>

    <div class="gate-grid">
      <section class="gate-panel">
        <h3>① 分区采样判定（阈值 {{ (threshold * 100).toFixed(0) }}%）</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>分区</th>
              <th>合格数/输入行</th>
              <th>有效率</th>
              <th>实际来源</th>
              <th>采样窗口</th>
              <th>判定</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="verdict in activeVerdicts" :key="verdict.partition" :data-status="verdict.passed ? 'pass' : 'fail'">
              <td>{{ verdict.partition }}</td>
              <td>{{ verdict.valid_rows }}/{{ verdict.total_rows }}</td>
              <td>{{ (verdict.valid_rate * 100).toFixed(1) }}%</td>
              <td>{{ verdict.source_refs.join('、') || '—' }}</td>
              <td>{{ verdict.window_id }}</td>
              <td>
                <span class="badge" :class="verdict.passed ? 'badge-ok' : 'badge-bad'">
                  {{ verdict.passed ? '合格' : '未达标' }}
                </span>
                <span v-if="verdict.version > 1" class="badge badge-history">v{{ verdict.version }} 含历史</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="panel-actions">
          <button class="btn primary" data-testid="evaluate" type="button" @click="evaluate">
            计算分区判定
          </button>
        </div>
      </section>

      <section class="gate-panel">
        <h3>② 提交版本 {{ commitId }} 发布范围</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>分区</th>
              <th>采用样本</th>
              <th>阈值/规则</th>
              <th>发布结果</th>
              <th>写入次数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in detail.partitions" :key="`${record.scope_id}-${record.id}`">
              <td>{{ record.partition }}</td>
              <td>
                {{ record.valid_rows }}/{{ record.total_rows }}
                <small>（{{ record.sample_window_id }}）</small>
              </td>
              <td>{{ (record.threshold * 100).toFixed(0) }}% / {{ record.rule }}</td>
              <td>
                <span class="badge" :class="record.published ? 'badge-ok' : 'badge-bad'">
                  {{ record.published ? '已发布' : '被阻断' }}
                </span>
                <small v-if="!record.published">{{ record.block_reason }}</small>
              </td>
              <td>{{ record.write_count }}</td>
            </tr>
            <tr v-if="!detail.partitions.length">
              <td colspan="5" class="empty-state">尚未提交，先在左侧完成分区判定</td>
            </tr>
          </tbody>
        </table>
        <div class="panel-actions">
          <button class="btn primary" data-testid="submit" type="button" @click="submit">
            提交一批（{{ partitions.join('、') }}）
          </button>
          <button class="btn" data-testid="simulate-interrupt" type="button" @click="simulateInterrupt">
            模拟中断提交
          </button>
          <button class="btn" data-testid="resume" type="button" :disabled="!interruptScopeId" @click="resume">
            中断恢复
          </button>
        </div>
      </section>
    </div>

    <div class="gate-grid">
      <section class="gate-panel">
        <h3>③ 迟到更正与重试</h3>
        <p class="panel-hint">
          p2 留在待处理时，一条失败行迟到更正：原判定与当前样本冲突会登记「新证据」，
          重试只补写 p2，p1 不会再次写入。
        </p>
        <div class="panel-actions wrap">
          <button class="btn" data-testid="correct-p2" type="button" @click="correctP2">
            更正 p2 失败行（1/2 → 2/2）
          </button>
          <button class="btn primary" data-testid="retry" type="button" @click="retry">
            重试提交（含 p1、p2）
          </button>
        </div>
        <div v-if="lastConflict" class="conflict-box" data-testid="conflict-box">
          <strong>样本冲突证据（{{ lastConflict.partition || 'p2' }}）</strong>
          <ul>
            <li>原判定：{{ lastConflict.frozen_valid_rate * 100 }}%（{{ formatSample(lastConflict.frozen_sample) }}）</li>
            <li>当前样本：{{ lastConflict.current_valid_rate * 100 }}%（{{ formatSample(lastConflict.current_sample) }}）</li>
            <li>冲突字段：{{ lastConflict.changed_fields.join('、') }}</li>
          </ul>
        </div>
      </section>

      <section class="gate-panel">
        <h3>④ 质量复核待办</h3>
        <div class="todo-summary">
          <span v-for="(label, key) in todos.labels" :key="key" class="todo-chip" :data-kind="key">
            {{ label }}：{{ todos.summary?.[key as TodoKind]?.total ?? 0 }}
            （待处理 {{ todos.summary?.[key as TodoKind]?.open ?? 0 }}）
          </span>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>类型</th>
              <th>分区</th>
              <th>状态</th>
              <th>说明</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="todo in todos.items" :key="todo.id" :data-todo-type="todo.todo_type">
              <td>
                <span class="badge" :class="todoBadgeClass(todo.todo_type)">{{ todoTypeLabel(todo.todo_type) }}</span>
              </td>
              <td>{{ todo.partition }}</td>
              <td>{{ todo.status === 'open' ? '待处理' : '已复核' }}</td>
              <td class="todo-message">{{ todo.message }}</td>
              <td>
                <button
                  v-if="todo.status === 'open'"
                  class="link"
                  type="button"
                  :data-testid="`resolve-todo-${todo.id}`"
                  @click="resolveTodo(todo.id)"
                >
                  复核确认
                </button>
              </td>
            </tr>
            <tr v-if="!todos.items.length">
              <td colspan="5" class="empty-state">暂无质量复核待办</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  correctVerdict,
  evaluateVerdicts,
  getCommitDetail,
  listReviewTodos,
  listVerdicts,
  resolveReviewTodo,
  resumeScope,
  retryCommit,
  submitCommit,
} from '@/api/quality-gate'
import type { CommitDetail, ReviewTodoList, Verdict } from '@/api/quality-gate'

type TodoKind = 'published' | 'blocked' | 'new_evidence'

const commitId = 1
const threshold = 0.8
const partitions = ['p1', 'p2']
const round = ref(0)

const verdicts = ref<Verdict[]>([])
const detail = ref<CommitDetail>({
  ok: true,
  commit_id: commitId,
  requested_partitions: [],
  actual_publish_range: [],
  blocked_partitions: [],
  scopes: [],
  partitions: [],
})
const todos = ref<ReviewTodoList>({
  ok: true,
  items: [],
  summary: {
    published: { open: 0, resolved: 0, total: 0 },
    blocked: { open: 0, resolved: 0, total: 0 },
    new_evidence: { open: 0, resolved: 0, total: 0 },
  },
  labels: { published: '已发布分区', blocked: '被阻断范围', new_evidence: '新证据' },
})
const message = ref('')
const messageKind = ref('info')
const lastConflict = ref<Record<string, any> | null>(null)
const interruptScopeId = ref<number | null>(null)
const idempotencyKey = computed(() => `frontend-demo-${round.value}`)

const activeVerdicts = computed(() =>
  verdicts.value.filter((verdict) => verdict.status === 'active'),
)
const publishableCount = computed(() =>
  activeVerdicts.value.filter((verdict) => verdict.passed).length,
)

function notify(text: string, kind: 'info' | 'ok' | 'warn' | 'error' = 'info') {
  message.value = text
  messageKind.value = kind
}

function demoSamples() {
  const suffix = round.value ? `-${round.value}` : ''
  return [
    {
      partition: `p1${suffix}`,
      valid_rows: 9,
      total_rows: 10,
      threshold,
      rule: 'ratio',
      window_id: `win-p1${suffix}`,
      source_refs: ['src-p1-a', 'src-p1-b'],
    },
    {
      partition: `p2${suffix}`,
      valid_rows: 1,
      total_rows: 2,
      threshold,
      rule: 'ratio',
      window_id: `win-p2${suffix}`,
      source_refs: ['src-p2-a'],
    },
  ]
}

async function evaluate() {
  try {
    const result = await evaluateVerdicts(7, demoSamples())
    if (!result.ok) throw new Error(result.message ?? '判定失败')
    await refreshAll()
    notify(
      `分区判定完成：p1 有效率 90% 合格，p2 有效率 50% 未达 80% 门槛，互不抵消`,
      'ok',
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '分区判定失败', 'error')
  }
}

async function submit() {
  try {
    const result = await submitCommit(
      commitId,
      demoSamples().map((sample) => sample.partition),
      { idempotencyKey: idempotencyKey.value },
    )
    if (!result.ok) throw new Error(result.message)
    interruptScopeId.value = null
    await refreshAll()
    notify(
      result.idempotent_replay
        ? '同一范围并发/重复提交命中幂等，未重复写入'
        : `实际发布范围：${(result.actual_publish_range ?? []).join('、') || '空'}；`
          + `被阻断：${(result.blocked_partitions ?? []).join('、') || '无'}`,
      result.blocked_partitions?.length ? 'warn' : 'ok',
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '提交失败', 'error')
  }
}

async function simulateInterrupt() {
  try {
    const result = await submitCommit(
      commitId,
      demoSamples().map((sample) => sample.partition),
      { idempotencyKey: `${idempotencyKey.value}-interrupt`, failAfterScope: true },
    )
    if (!result.ok) throw new Error(result.message)
    interruptScopeId.value = result.scope_id ?? null
    notify('提交在写入前中断，原样本窗口与待处理分区已保留，可执行中断恢复', 'warn')
  } catch (error) {
    notify(error instanceof Error ? error.message : '中断模拟失败', 'error')
  }
}

async function resume() {
  if (interruptScopeId.value === null) return
  try {
    const result = await resumeScope(interruptScopeId.value)
    if (!result.ok) throw new Error(result.message)
    interruptScopeId.value = null
    await refreshAll()
    notify(`中断恢复完成，实际发布范围：${(result.actual_publish_range ?? []).join('、') || '空'}`, 'ok')
  } catch (error) {
    notify(error instanceof Error ? error.message : '恢复失败', 'error')
  }
}

async function correctP2() {
  try {
    const p2 = demoSamples()[1].partition
    const result = await correctVerdict({
      partition: p2,
      valid_rows: 2,
      total_rows: 2,
      note: '迟到行更正',
      source_refs: ['src-p2-a', 'src-p2-late'],
    })
    if (!result.ok) throw new Error(result.message)
    lastConflict.value = result.conflict
    await refreshAll()
    notify('p2 迟到更正完成：原判定历史保留，冲突已登记为新证据待办', 'warn')
  } catch (error) {
    notify(error instanceof Error ? error.message : '更正失败', 'error')
  }
}

async function retry() {
  try {
    const result = await retryCommit(
      commitId,
      demoSamples().map((sample) => sample.partition),
      `${idempotencyKey.value}-retry`,
    )
    if (!result.ok) throw new Error(result.message)
    await refreshAll()
    notify(
      `重试完成：本次写入 ${result.write_count ?? 0} 个分区，`
      + `实际发布范围 ${(result.actual_publish_range ?? []).join('、') || '空'}；`
      + `已发布跳过：${(result.skipped_already_published ?? []).join('、') || '无'}`,
      'ok',
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '重试失败', 'error')
  }
}

async function resolveTodo(todoId: number) {
  try {
    const result = await resolveReviewTodo(todoId, '人工复核确认')
    if (!result.ok) throw new Error(result.message ?? '复核失败')
    await refreshAll()
  } catch (error) {
    notify(error instanceof Error ? error.message : '复核失败', 'error')
  }
}

async function refreshAll() {
  const [verdictPayload, detailPayload, todoPayload] = await Promise.all([
    listVerdicts(),
    getCommitDetail(commitId),
    listReviewTodos(),
  ])
  verdicts.value = verdictPayload.items
  detail.value = detailPayload
  todos.value = todoPayload
}

function resetDemo() {
  round.value += 1
  lastConflict.value = null
  interruptScopeId.value = null
  message.value = '已开启新一轮演练：使用新分区名重新走判定 → 提交 → 更正 → 重试'
  messageKind.value = 'info'
}

function formatSample(sample: Record<string, unknown> | undefined | null) {
  if (!sample) return '—'
  return `${sample.valid_rows}/${sample.total_rows}`
}

function todoTypeLabel(type: TodoKind) {
  return todos.value.labels[type] ?? type
}

function todoBadgeClass(type: TodoKind) {
  if (type === 'published') return 'badge-ok'
  if (type === 'blocked') return 'badge-bad'
  return 'badge-warn'
}

onMounted(refreshAll)
</script>
