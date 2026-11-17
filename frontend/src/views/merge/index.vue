<template>
  <section class="page" data-module="merge">
    <header class="page-head">
      <div>
        <h2>结果聚合归并管理</h2>
        <p class="page-desc">维护采集结果的聚合策略、进度追踪与输出管理，为结果聚合归并提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="buildPending">按完整覆盖建立待归并</button>
        <button class="btn" type="button" @click="exportRows">导出结果聚合归并清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="section-title">
      <h3>新增分片覆盖准入汇总</h3>
      <span>与分片覆盖清单同源，覆盖不完整的计划不会进入待归并</span>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>覆盖编号</th><th>采集计划</th><th>范围版本</th><th>处理结论</th><th>覆盖目标</th><th>未覆盖区间</th><th>来源版本</th><th>条目状态</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in admission" :key="String(row.id)">
          <td>{{ row.覆盖编号 ?? '—' }}</td>
          <td>{{ row.采集计划 ?? '—' }}</td>
          <td>{{ row.范围版本 ?? '—' }}</td>
          <td>{{ row.处理结论 ?? '—' }}</td>
          <td>{{ row.覆盖目标 ?? '—' }}</td>
          <td>{{ textOf(row.未覆盖区间) }}</td>
          <td>{{ textOf(row.来源版本) }}</td>
          <td>{{ row.条目状态 ?? '—' }}</td>
        </tr>
        <tr v-if="!admission.length">
          <td colspan="8" class="empty-state">暂无覆盖准入批次</td>
        </tr>
      </tbody>
    </table>

    <div class="section-title">
      <h3>分片覆盖清单</h3>
      <span>共 {{ entries.length }} 条明细，逐片核对覆盖区间与节点容量</span>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>覆盖编号</th><th>采集计划</th><th>条目类型</th><th>覆盖区间</th><th>分配节点</th><th>占用容量</th><th>节点容量上限</th><th>来源版本</th><th>条目状态</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in entries" :key="String(row.id)">
          <td>{{ row.覆盖编号 ?? '—' }}</td>
          <td>{{ row.采集计划 ?? '—' }}</td>
          <td>{{ row.条目类型 ?? '—' }}</td>
          <td>{{ row.覆盖区间 ?? '—' }}</td>
          <td>{{ row.分配节点 ?? '—' }}</td>
          <td>{{ row.占用容量 ?? '—' }}</td>
          <td>{{ row.节点容量上限 ?? '—' }}</td>
          <td>{{ row.来源版本 ?? '—' }}</td>
          <td>{{ row.条目状态 ?? '—' }}</td>
        </tr>
        <tr v-if="!entries.length">
          <td colspan="9" class="empty-state">暂无覆盖清单明细</td>
        </tr>
      </tbody>
    </table>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无结果聚合归并数据，可先登记聚合任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条结果聚合归并记录 · 待归并 {{ coverageSummary['待归并条目数'] ?? 0 }} 条</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="successMessage" class="success-text">{{ successMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/merge'
const columns = ["任务编号", "采集计划", "分片总数", "已归并数", "归并策略", "输出路径", "数据总量", "任务状态"]
const actions = ["启动归并", "重试归并", "归档任务"]
const statuses = ["待归并", "归并中", "已完成", "归并失败"]
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const successMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const admission = ref<Record<string, unknown>[]>([])
const entries = ref<Record<string, unknown>[]>([])
const coverageSummary = ref<Record<string, number>>({})

const statCards = computed(() => [
  { label: '聚合任务总数', value: total.value },
  { label: '归并中任务', value: statusCount('归并中') },
  { label: '已完成任务', value: statusCount('已完成') },
  { label: '待归并条目', value: coverageSummary.value['待归并条目数'] ?? 0 },
])

function statusCount(status: string) {
  return rows.value.filter((row) => row['status'] === status).length
}

function textOf(value: unknown) {
  if (Array.isArray(value)) {
    return value.length ? value.join('、') : '—'
  }
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

async function buildPending() {
  errorMessage.value = ''
  successMessage.value = ''
  try {
    const response = await request('/api/merge/pending/build', { method: 'POST', body: '{}' })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message ?? '待归并条目建立失败')
    }
    successMessage.value = payload.message ?? '待归并条目已建立'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '待归并条目建立失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('结果聚合归并动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果聚合归并操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('聚合任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    const coverage = await fetchJson<{
      summary: Record<string, number>
      admission: Record<string, unknown>[]
      entries: Record<string, unknown>[]
    }>('/api/merge/coverage')
    coverageSummary.value = coverage.summary ?? {}
    admission.value = coverage.admission ?? []
    entries.value = coverage.entries ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结果聚合归并列表读取失败'
  }
}

onMounted(reload)
</script>
