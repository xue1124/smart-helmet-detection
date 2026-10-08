<script setup lang="ts">
import { Document } from "@element-plus/icons-vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { computed, onUnmounted, reactive, ref, watch } from "vue";
import http, {
  deleteMonitorSession,
  type MonitorDevice,
  type MonitorSessionItem,
  type TaskInfo,
} from "../api";
import ResultPanel from "./ResultPanel.vue";

const props = withDefaults(
  defineProps<{
    /** 当前是否在「安全记录」标签页 */
    active?: boolean;
  }>(),
  { active: false },
);

const loading = ref(false);
const detailLoading = ref(false);
const devices = ref<MonitorDevice[]>([]);
const sessions = ref<MonitorSessionItem[]>([]);
const total = ref(0);
const selectedId = ref<number | null>(null);
const detailTask = ref<TaskInfo | null>(null);
const detailTaskId = ref("");
const deleting = ref(false);
const stopping = ref(false);

let pollTimer: ReturnType<typeof setInterval> | null = null;
const POLL_MS = 2000;
const filters = reactive({
  device_id: "" as "" | number,
  source_type: "",
  status: "",
  dateRange: [] as string[],
});

const pagination = reactive({
  page: 1,
  pageSize: 15,
});

const statusOptions = [
  { value: "", label: "全部状态" },
  { value: "streaming", label: "监控中" },
  { value: "pending", label: "等待中" },
  { value: "downloading", label: "下载中" },
  { value: "running", label: "分析中" },
  { value: "stopped", label: "已停止" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
];

const sourceOptions = [
  { value: "", label: "全部来源" },
  { value: "rtsp", label: "实时监控" },
  { value: "local", label: "本地上传" },
  { value: "isapi", label: "录像回放" },
];

const activeStatuses = new Set(["streaming", "pending", "downloading", "running"]);

const analysisStopStatuses = new Set(["pending", "downloading", "running"]);
const rtspStopStatuses = new Set(["streaming", "pending"]);

const selectedSession = computed(() => sessions.value.find((s) => s.id === selectedId.value) ?? null);

const canStopSelected = computed(() => {
  const session = selectedSession.value;
  if (!session) return false;
  if (session.source_type === "local" || session.source_type === "isapi") {
    return analysisStopStatuses.has(session.status);
  }
  if (session.source_type === "rtsp") {
    return rtspStopStatuses.has(session.status);
  }
  return false;
});

function formatTime(iso: string | null | undefined) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function durationText(row: MonitorSessionItem) {
  if (!row.started_at) return "—";
  const start = new Date(row.started_at).getTime();
  const end = row.stopped_at ? new Date(row.stopped_at).getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return "—";
  const sec = Math.floor((end - start) / 1000);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}时${m}分`;
  if (m > 0) return `${m}分${s}秒`;
  return `${s}秒`;
}

function statusTagType(status: string) {
  if (status === "failed") return "danger";
  if (status === "streaming") return "primary";
  if (status === "running" || status === "downloading") return "warning";
  if (status === "stopped" || status === "completed") return "success";
  return "info";
}

async function loadDevices() {
  try {
    const { data } = await http.get<{ devices: MonitorDevice[] }>("/devices");
    devices.value = data.devices || [];
  } catch {
    devices.value = [];
  }
}

async function loadSessions(silent = false) {
  if (!silent) loading.value = true;
  try {
    const params: Record<string, string | number> = {
      page: pagination.page,
      page_size: pagination.pageSize,
    };
    if (filters.device_id !== "") params.device_id = filters.device_id;
    if (filters.source_type) params.source_type = filters.source_type;
    if (filters.status) params.status = filters.status;
    if (filters.dateRange?.length === 2) {
      params.started_from = filters.dateRange[0];
      params.started_to = filters.dateRange[1];
    }
    const { data } = await http.get<{
      items: MonitorSessionItem[];
      total: number;
      page: number;
      page_size: number;
    }>("/monitor-sessions", { params });
    sessions.value = data.items || [];
    total.value = data.total || 0;
    if (selectedId.value && !sessions.value.some((s) => s.id === selectedId.value)) {
      selectedId.value = sessions.value[0]?.id ?? null;
    }
    if (!selectedId.value && sessions.value.length) {
      selectedId.value = sessions.value[0].id;
    }
  } catch (err: any) {
    if (!silent) ElMessage.error(err?.response?.data?.detail || "加载安全记录失败");
  } finally {
    if (!silent) loading.value = false;
  }
}

async function loadDetail(sessionId: number, silent = false) {
  if (!silent) detailLoading.value = true;
  if (!silent) {
    detailTask.value = null;
    detailTaskId.value = "";
  }
  try {
    const { data } = await http.get<{ session: MonitorSessionItem; task: TaskInfo }>(
      `/monitor-sessions/${sessionId}`,
    );
    detailTask.value = data.task;
    detailTaskId.value = data.task.id;
  } catch (err: any) {
    if (!silent) ElMessage.error(err?.response?.data?.detail || "加载详情失败");
  } finally {
    if (!silent) detailLoading.value = false;
  }
}

async function refreshAll(silent = false) {
  await loadSessions(silent);
  if (selectedId.value != null) {
    await loadDetail(selectedId.value, silent);
  }
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function startPoll() {
  stopPoll();
  pollTimer = setInterval(() => {
    void refreshAll(true);
  }, POLL_MS);
}

async function onTabActivated() {
  await loadDevices();
  await refreshAll(false);
  startPoll();
}
function onSearch() {
  pagination.page = 1;
  loadSessions();
}

function onReset() {
  filters.device_id = "";
  filters.source_type = "";
  filters.status = "";
  filters.dateRange = [];
  pagination.page = 1;
  loadSessions();
}

function onPageChange(page: number) {
  pagination.page = page;
  loadSessions();
}

function selectRow(row: MonitorSessionItem) {
  selectedId.value = row.id;
}

async function stopSelectedSession() {
  const session = selectedSession.value;
  if (!session || !canStopSelected.value) return;
  const actionLabel =
    session.source_type === "rtsp"
      ? "停止监控"
      : session.status === "downloading"
        ? "停止下载与分析"
        : "停止分析";
  try {
    await ElMessageBox.confirm(
      `确定${actionLabel}「${session.device_name}」？\n已产生的记录会保留，状态将变为「已停止」。`,
      "停止确认",
      { type: "warning", confirmButtonText: "停止", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  stopping.value = true;
  try {
    const fd = new FormData();
    fd.append("task_id", session.task_id);
    if (session.source_type === "rtsp") {
      await http.post("/stream/live/stop", fd);
    } else {
      await http.post("/tasks/stop", fd);
    }
    ElMessage.success("已停止");
    await refreshAll(true);
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "停止失败");
  } finally {
    stopping.value = false;
  }
}

async function deleteSelectedSession() {
  const session = selectedSession.value;
  if (!session) return;
  const extra =
    session.source_type === "local" || session.source_type === "isapi"
      ? "及相关抓拍图片、上传视频"
      : "及相关抓拍图片";
  try {
    await ElMessageBox.confirm(
      `确定删除「${session.device_name}」${formatTime(session.started_at)} 的安全记录？\n将同时删除该记录${extra}，且不可恢复。`,
      "删除确认",
      { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }
  deleting.value = true;
  try {
    await deleteMonitorSession(session.id);
    ElMessage.success("已删除");
    selectedId.value = null;
    detailTask.value = null;
    detailTaskId.value = "";
    await loadSessions();
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "删除失败");
  } finally {
    deleting.value = false;
  }
}

watch(selectedId, (id) => {
  if (id != null) loadDetail(id);
  else {
    detailTask.value = null;
    detailTaskId.value = "";
  }
});

watch(
  () => props.active,
  (isActive) => {
    if (isActive) void onTabActivated();
    else stopPoll();
  },
  { immediate: true },
);

onUnmounted(() => {
  stopPoll();
});</script>

<template>
  <div class="safety-records">
    <header class="panel toolbar">
      <div class="toolbar-left">
        <h2>安全记录</h2>
        <p class="hint">实时监控、本地上传、录像回放产生的未戴帽事件历史</p>
      </div>
      <el-button :loading="loading" @click="refreshAll(false)">刷新</el-button>
    </header>

    <div class="records-body">
      <section class="panel list-panel">
        <el-form inline class="filter-form">
          <el-form-item label="设备">
            <el-select v-model="filters.device_id" clearable placeholder="全部设备" style="width: 180px">
              <el-option v-for="d in devices" :key="d.id" :label="d.name" :value="d.id" />
            </el-select>
          </el-form-item>
          <el-form-item label="来源">
            <el-select v-model="filters.source_type" style="width: 120px">
              <el-option v-for="o in sourceOptions" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
          </el-form-item>
          <el-form-item label="状态">
            <el-select v-model="filters.status" style="width: 120px">
              <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
          </el-form-item>
          <el-form-item label="开始日期">
            <el-date-picker
              v-model="filters.dateRange"
              type="daterange"
              value-format="YYYY-MM-DD"
              start-placeholder="起"
              end-placeholder="止"
              style="width: 240px"
            />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" @click="onSearch">查询</el-button>
            <el-button @click="onReset">重置</el-button>
          </el-form-item>
        </el-form>

        <el-table
          v-loading="loading"
          :data="sessions"
          stripe
          highlight-current-row
          :row-class-name="({ row }) => (row.id === selectedId ? 'is-current' : '')"
          empty-text="暂无监控记录"
          @row-click="selectRow"
        >
          <el-table-column prop="device_name" label="名称" min-width="120" show-overflow-tooltip />
          <el-table-column label="来源" width="90">
            <template #default="{ row }">{{ row.source_type_label }}</template>
          </el-table-column>
          <el-table-column label="开始时间" min-width="150">
            <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
          </el-table-column>
          <el-table-column label="时长" width="90">
            <template #default="{ row }">{{ durationText(row) }}</template>
          </el-table-column>
          <el-table-column label="人数" width="70" align="center">
            <template #default="{ row }">{{ row.saved_count }}</template>
          </el-table-column>
          <el-table-column label="违规" width="70" align="center">
            <template #default="{ row }">{{ row.violation_frames }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTagType(row.status)">{{ row.status_label }}</el-tag>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="total > pagination.pageSize" class="pager">
          <el-pagination
            layout="total, prev, pager, next"
            :total="total"
            :page-size="pagination.pageSize"
            :current-page="pagination.page"
            @current-change="onPageChange"
          />
        </div>
      </section>

      <section v-loading="detailLoading" class="panel detail-panel">
        <template v-if="selectedSession">
          <div class="detail-head">
            <div>
              <h3>{{ selectedSession.device_name }}</h3>
              <p class="detail-meta">
                {{ formatTime(selectedSession.started_at) }}
                <span v-if="selectedSession.stopped_at"> → {{ formatTime(selectedSession.stopped_at) }}</span>
                · 间隔 {{ selectedSession.frame_stride }} 帧
              </p>
              <p
                v-if="selectedSession.error_message && selectedSession.status !== 'failed'"
                class="detail-note"
              >
                {{ selectedSession.error_message }}
              </p>
            </div>
            <div class="detail-actions">
              <el-button
                v-if="canStopSelected"
                type="warning"
                plain
                size="small"
                :loading="stopping"
                @click="stopSelectedSession"
              >
                停止
              </el-button>
              <el-button
                type="danger"
                plain
                size="small"
                :loading="deleting"
                :disabled="activeStatuses.has(selectedSession.status)"
                @click="deleteSelectedSession"
              >
                删除记录
              </el-button>
            </div>
          </div>
          <ResultPanel
            title="安全事件"
            :task="detailTask"
            :task-id="detailTaskId"
            :live="activeStatuses.has(selectedSession.status)"
            :show-status="false"
          />
        </template>
        <div v-else class="detail-empty">
          <el-icon class="empty-icon"><Document /></el-icon>
          <p>请选择左侧一条监控记录查看详情</p>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.safety-records {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: calc(100vh - 40px);
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  padding: 14px 18px;
}

.toolbar-left h2 {
  margin: 0 0 4px;
  font-size: 18px;
}

.hint {
  margin: 0;
  font-size: 13px;
  color: #64748b;
}

.panel {
  background: #fff;
  border-radius: 12px;
  padding: 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
}

.records-body {
  flex: 1;
  display: grid;
  grid-template-columns: minmax(420px, 1fr) minmax(360px, 1fr);
  gap: 12px;
  min-height: 0;
}

.list-panel {
  min-height: 520px;
}

.filter-form {
  margin-bottom: 12px;
}

.filter-form :deep(.el-form-item) {
  margin-bottom: 8px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}

.detail-panel {
  display: flex;
  flex-direction: column;
  min-height: 520px;
  max-height: calc(100vh - 120px);
  overflow: hidden;
}

.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid #e2e8f0;
}

.detail-head h3 {
  margin: 0 0 4px;
  font-size: 16px;
}

.detail-meta {
  margin: 0;
  font-size: 12px;
  color: #64748b;
}

.detail-note {
  margin: 6px 0 0;
  font-size: 12px;
  color: #94a3b8;
}

.detail-actions {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-shrink: 0;
}

.detail-panel :deep(.result-panel) {
  flex: 1;
  min-height: 0;
}

.detail-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: #94a3b8;
}

.empty-icon {
  font-size: 48px;
  margin-bottom: 8px;
}

:deep(.el-table .is-current > td) {
  background: #eff6ff !important;
}

@media (max-width: 1100px) {
  .records-body {
    grid-template-columns: 1fr;
  }

  .detail-panel {
    max-height: none;
  }
}
</style>
