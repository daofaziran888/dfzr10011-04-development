<template>
  <section class="page" data-module="shard">
    <header class="page-head">
      <div>
        <h2>任务分片调度管理</h2>
        <p class="page-desc">维护任务分片的范围划分、节点分配与重试控制，为任务分片调度提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <RouterLink class="btn primary" to="/coverage">登记分片任务</RouterLink>
        <button class="btn" type="button" @click="exportRows">导出任务分片调度清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <div class="section-title">
      <h3>分片覆盖准入与容量核对</h3>
      <span>与聚合侧同一份清单：每个分片只占用一个容量版本，逐片核对区间与节点容量上限</span>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>覆盖编号</th><th>采集计划</th><th>条目类型</th><th>覆盖区间</th><th>分配节点</th><th>占用容量</th><th>节点容量上限</th><th>来源版本</th><th>条目状态</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in coverageEntries" :key="String(row.id)">
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
        <tr v-if="!coverageEntries.length">
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
          <td :colspan="columns.length + 1" class="empty-state">暂无任务分片调度数据，可先登记分片任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条任务分片调度记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/shard'
const columns = ["分片编号", "采集计划", "分片范围", "分配节点", "开始时间", "结束时间", "重试次数", "分片状态"]
const actions = ["分配节点", "重试分片", "标记完成"]
const statuses = ["待分配", "执行中", "已完成", "执行失败"]
const rows = ref<Row[]>([])
const total = ref(0)
const coverageEntries = ref<Record<string, unknown>[]>([])
const coverageSummary = ref<Record<string, number>>({})

const statCards = computed(() => [
  { label: '分片任务总数', value: total.value },
  { label: '执行中分片', value: rows.value.filter((row) => row['status'] === '执行中').length },
  { label: '已完成分片', value: rows.value.filter((row) => row['status'] === '已完成').length },
  { label: '覆盖清单条目', value: coverageSummary.value['分片条目数'] ?? 0 },
  { label: '未覆盖区间', value: coverageSummary.value['未覆盖区间数'] ?? 0 },
])
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

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
      throw new Error('任务分片调度动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '任务分片调度操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('分片任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    const coverage = await fetchJson<{
      summary: Record<string, number>
      entries: Record<string, unknown>[]
    }>('/api/shard/coverage')
    coverageSummary.value = coverage.summary ?? {}
    coverageEntries.value = coverage.entries ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '任务分片调度列表读取失败'
  }
}

onMounted(reload)
</script>
