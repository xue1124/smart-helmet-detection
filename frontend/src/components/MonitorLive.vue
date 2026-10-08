<script setup lang="ts">
import { Grid, VideoCamera } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import type { MonitorDevice, TaskInfo } from "../api";
import ResultPanel from "./ResultPanel.vue";

export interface RtspSessionView {
  deviceId: number;
  deviceName: string;
  taskId: string;
  task: TaskInfo | null;
}

const MAX_CAMERAS = 9;

const props = withDefaults(
  defineProps<{
  devices: MonitorDevice[];
  selectedDeviceIds: number[];
  sessions: RtspSessionView[];
  frameStride: number;
  enableReid: boolean;
  reidThreshold: number;
  starting: boolean;
  stopping: boolean;
  stoppingDeviceId: number | null;
  canStart: boolean;
  /** 当前是否在「实时监控」标签页 */
  active?: boolean;
}>(),
  { active: true },
);

const emit = defineEmits<{
  "update:selectedDeviceIds": [value: number[]];
  "update:frameStride": [value: number];
  "update:enableReid": [value: boolean];
  "update:reidThreshold": [value: number];
  start: [];
  stop: [];
  stopOne: [session: RtspSessionView];
  refreshDevices: [];
}>();

const focusedDeviceId = ref<number | null>(null);
const expandedDeviceId = ref<number | null>(null);
const previewEpoch = ref(0);
const previewSrc = ref("");
const showPreviewImg = ref(false);
const previewLoading = ref(false);
const previewFailed = ref(false);
const previewImgRef = ref<HTMLImageElement | null>(null);

const PREVIEW_LOAD_TIMEOUT_MS = 12000;
const PREVIEW_RECONNECT_DELAY_MS = 120;
const MIN_RECONNECT_MS = 3000;

let lastReconnectAt = 0;
let previewLoadTimer: ReturnType<typeof setTimeout> | null = null;

const isStreaming = computed(() => props.sessions.length > 0);

const streamingDeviceIds = computed(() => new Set(props.sessions.map((s) => s.deviceId)));

const pendingCount = computed(() =>
  props.selectedDeviceIds.filter((id) => !streamingDeviceIds.value.has(id)).length,
);

const cameraCount = computed(() =>
  isStreaming.value ? props.sessions.length : props.selectedDeviceIds.length,
);

const useNineGrid = computed(() => cameraCount.value > 4);

const gridSize = computed(() => (useNineGrid.value ? 9 : 4));

const gridLabel = computed(() => (useNineGrid.value ? "九宫格" : "四宫格"));

const startButtonLabel = computed(() => (isStreaming.value ? "添加监控" : "开始监控"));

const gridCells = computed(() => {
  const cells: (RtspSessionView | null)[] = [];
  for (let i = 0; i < gridSize.value; i++) {
    cells.push(props.sessions[i] ?? null);
  }
  return cells;
});

const focusedSession = computed(() => {
  if (!props.sessions.length) return null;
  if (focusedDeviceId.value != null) {
    const hit = props.sessions.find((s) => s.deviceId === focusedDeviceId.value);
    if (hit) return hit;
  }
  return props.sessions[0];
});

const expandedSession = computed(() => {
  if (expandedDeviceId.value == null) return null;
  return props.sessions.find((s) => s.deviceId === expandedDeviceId.value) ?? null;
});

watch(
  () => props.sessions.map((s) => s.taskId).join(","),
  () => {
    if (props.sessions.length && focusedDeviceId.value == null) {
      focusedDeviceId.value = props.sessions[0].deviceId;
    }
    if (
      focusedDeviceId.value != null &&
      !props.sessions.some((s) => s.deviceId === focusedDeviceId.value)
    ) {
      focusedDeviceId.value = props.sessions[0]?.deviceId ?? null;
    }
    if (
      expandedDeviceId.value != null &&
      !props.sessions.some((s) => s.deviceId === expandedDeviceId.value)
    ) {
      expandedDeviceId.value = null;
    }
  },
);

watch(
  () => props.sessions.length,
  (len) => {
    if (len === 0) {
      focusedDeviceId.value = null;
      expandedDeviceId.value = null;
    }
  },
);

watch(
  () => props.active,
  (isActive) => {
    if (!isActive) {
      expandedDeviceId.value = null;
      teardownPreview();
    }
  },
);

watch(
  () => props.stopping,
  (isStopping) => {
    if (isStopping) {
      expandedDeviceId.value = null;
      teardownPreview();
    }
  },
);

watch(expandedSession, (session, prev) => {
  if (session) {
    if (!prev || prev.taskId !== session.taskId) {
      void openPreviewStream(session.taskId);
    }
  } else {
    teardownPreview();
  }
});

function previewUrl(taskId: string) {
  return `/api/v1/stream/live/preview/${encodeURIComponent(taskId)}?k=${previewEpoch.value}`;
}

function clearPreviewLoadTimer() {
  if (previewLoadTimer) {
    clearTimeout(previewLoadTimer);
    previewLoadTimer = null;
  }
}

function schedulePreviewLoadTimeout() {
  clearPreviewLoadTimer();
  previewLoadTimer = setTimeout(() => {
    if (!expandedSession.value || !showPreviewImg.value) return;
    previewLoading.value = false;
    previewFailed.value = true;
  }, PREVIEW_LOAD_TIMEOUT_MS);
}

async function detachPreviewImg() {
  previewSrc.value = "";
  showPreviewImg.value = false;
  if (previewImgRef.value) {
    previewImgRef.value.removeAttribute("src");
  }
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, PREVIEW_RECONNECT_DELAY_MS));
}

async function openPreviewStream(taskId: string) {
  clearPreviewLoadTimer();
  previewFailed.value = false;
  previewLoading.value = true;

  await detachPreviewImg();

  previewEpoch.value += 1;
  previewSrc.value = previewUrl(taskId);
  showPreviewImg.value = true;

  await nextTick();
  schedulePreviewLoadTimeout();
}

function teardownPreview() {
  clearPreviewLoadTimer();
  void detachPreviewImg();
  previewLoading.value = false;
  previewFailed.value = false;
}

async function reconnectPreview(force = false) {
  if (!expandedSession.value) return;
  const now = Date.now();
  if (!force && now - lastReconnectAt < MIN_RECONNECT_MS) {
    ElMessage.info("请稍后再试");
    return;
  }
  lastReconnectAt = now;
  await openPreviewStream(expandedSession.value.taskId);
}

function onPreviewLoad() {
  previewLoading.value = false;
  previewFailed.value = false;
  clearPreviewLoadTimer();
}

function onPreviewError() {
  if (!expandedSession.value) return;
  void reconnectPreview(true);
}

function deviceStatusType(status: number) {
  if (status === 1) return "success";
  if (status === 2) return "danger";
  return "info";
}

function isDeviceStreaming(deviceId: number) {
  return streamingDeviceIds.value.has(deviceId);
}

function onDevicesChange(val: number[]) {
  const streaming = [...streamingDeviceIds.value];
  const merged = [...new Set([...streaming, ...val])];
  if (merged.length > MAX_CAMERAS) {
    ElMessage.warning(`最多同时监控 ${MAX_CAMERAS} 路`);
    emit("update:selectedDeviceIds", merged.slice(0, MAX_CAMERAS));
    return;
  }
  emit("update:selectedDeviceIds", merged);
}

function toggleDeviceSelect(deviceId: number) {
  if (isDeviceStreaming(deviceId)) {
    focusedDeviceId.value = deviceId;
    return;
  }
  const set = new Set(props.selectedDeviceIds);
  if (set.has(deviceId)) set.delete(deviceId);
  else {
    if (set.size >= MAX_CAMERAS) {
      ElMessage.warning(`最多选择 ${MAX_CAMERAS} 路`);
      return;
    }
    set.add(deviceId);
  }
  emit("update:selectedDeviceIds", [...set]);
}

function onGridCellClick(cell: RtspSessionView | null) {
  if (!cell) return;
  focusedDeviceId.value = cell.deviceId;
}

function onGridCellDblClick(cell: RtspSessionView | null) {
  if (!cell) return;
  focusedDeviceId.value = cell.deviceId;
  expandedDeviceId.value = cell.deviceId;
}

function collapseExpanded() {
  expandedDeviceId.value = null;
}

function cellStats(cell: RtspSessionView) {
  const r = cell.task?.result;
  if (!r) return "分析中…";
  const persons = r.saved_count ?? 0;
  const frames = r.processed_frames ?? 0;
  return `${persons} 人 · ${frames} 帧`;
}

onBeforeUnmount(() => {
  expandedDeviceId.value = null;
  teardownPreview();
});

function stopSession(session: RtspSessionView, ev?: Event) {
  ev?.stopPropagation();
  emit("stopOne", session);
}
</script>

<template>
  <div class="monitor-live">
    <header class="toolbar panel">
      <div class="toolbar-left">
        <h2>实时监控</h2>
        <el-tag v-if="isStreaming" type="success" size="small">
          {{ sessions.length }} 路监控中
        </el-tag>
        <el-tag v-if="cameraCount > 0" type="info" size="small">
          <el-icon style="vertical-align: -2px"><Grid /></el-icon>
          {{ gridLabel }}
        </el-tag>
      </div>
      <div class="toolbar-controls">
        <el-select
          :model-value="selectedDeviceIds"
          multiple
          collapse-tags
          collapse-tags-tooltip
          :placeholder="isStreaming ? '选择要添加的设备' : '选择设备（最多 9 路）'"
          style="width: 280px"
          @update:model-value="onDevicesChange"
        >
          <el-option
            v-for="d in devices"
            :key="d.id"
            :label="`${d.name}${d.location ? ' · ' + d.location : ''}${isDeviceStreaming(d.id) ? '（监控中）' : ''}`"
            :value="d.id"
            :disabled="isDeviceStreaming(d.id)"
          />
        </el-select>
        <el-button type="primary" :loading="starting" :disabled="!canStart" @click="emit('start')">
          {{ startButtonLabel }}
          <span v-if="isStreaming && pendingCount">({{ pendingCount }})</span>
        </el-button>
        <el-button type="danger" plain :loading="stopping" :disabled="!isStreaming" @click="emit('stop')">
          全部停止
        </el-button>
        <el-button @click="emit('refreshDevices')">刷新设备</el-button>
      </div>
    </header>

    <div class="monitor-body">
      <section class="video-panel panel">
        <div v-if="isStreaming" class="video-area">
          <div v-if="expandedSession" class="video-expanded">
            <div class="video-stream-wrap">
              <img
                v-if="showPreviewImg && previewSrc"
                ref="previewImgRef"
                class="video-stream"
                :src="previewSrc"
                :alt="expandedSession.deviceName"
                @load="onPreviewLoad"
                @error="onPreviewError"
              />
              <div v-if="previewLoading || previewFailed" class="preview-stale-overlay">
                {{
                  previewFailed
                    ? "加载失败，请点「重新加载」或返回宫格后重试"
                    : "正在加载画面…"
                }}
              </div>
            </div>
            <div class="expanded-bar">
              <span class="expanded-title">{{ expandedSession.deviceName }}</span>
              <div class="expanded-actions">
                <el-button size="small" plain :loading="previewLoading" @click="reconnectPreview(true)">
                  重新加载
                </el-button>
                <el-button size="small" type="primary" plain @click="collapseExpanded">返回宫格</el-button>
              </div>
            </div>
          </div>
          <div v-else class="video-grid" :class="useNineGrid ? 'grid-nine' : 'grid-four'">
            <div
              v-for="(cell, idx) in gridCells"
              :key="cell?.taskId ?? `empty-${idx}`"
              class="grid-cell"
              :class="{ focused: cell && cell.deviceId === focusedDeviceId, empty: !cell }"
              @click="onGridCellClick(cell)"
              @dblclick="onGridCellDblClick(cell)"
            >
              <template v-if="cell">
                <div class="cell-idle">
                  <el-icon class="cell-idle-icon"><VideoCamera /></el-icon>
                  <div class="cell-name">{{ cell.deviceName }}</div>
                  <div class="cell-stats">{{ cellStats(cell) }}</div>
                  <div class="cell-hint">单击看事件 · 双击看画面</div>
                </div>
              </template>
              <div v-else class="cell-placeholder">
                <el-icon><VideoCamera /></el-icon>
              </div>
            </div>
          </div>
        </div>

        <div v-else class="video-placeholder">
          <el-icon class="placeholder-icon"><VideoCamera /></el-icon>
          <p v-if="selectedDeviceIds.length">已选 {{ selectedDeviceIds.length }} 路，将使用{{ gridLabel }}</p>
          <p v-else>请从右侧选择设备并开始监控</p>
        </div>

        <div class="video-settings">
          <div class="setting-row">
            <span class="setting-label">分析间隔（每 {{ frameStride }} 帧）</span>
            <el-slider
              :model-value="frameStride"
              :min="1"
              :max="120"
              show-input
              :disabled="isStreaming"
              @update:model-value="emit('update:frameStride', $event)"
            />
          </div>
          <div class="setting-row inline">
            <el-switch
              :model-value="enableReid"
              active-text="同人合并"
              :disabled="isStreaming"
              @update:model-value="emit('update:enableReid', $event)"
            />
            <template v-if="enableReid">
              <span class="setting-label">阈值 {{ reidThreshold }}</span>
              <el-slider
                :model-value="reidThreshold"
                :min="0.5"
                :max="0.99"
                :step="0.01"
                style="width: 200px"
                :disabled="isStreaming"
                @update:model-value="emit('update:reidThreshold', $event)"
              />
            </template>
          </div>
          <p v-if="isStreaming" class="settings-hint">分析在后台运行；单击宫格切换事件，双击打开监控画面。分析参数仅对新添加的摄像头生效</p>
        </div>
      </section>

      <aside class="side-panel">
        <section class="panel events-panel">
          <ResultPanel
            title="安全事件记录"
            :task="focusedSession?.task ?? null"
            :task-id="focusedSession?.taskId ?? ''"
            live
          />
          <p v-if="isStreaming && focusedSession" class="events-hint">
            当前查看：{{ focusedSession.deviceName }}（单击宫格切换事件，双击打开画面）
          </p>
        </section>

        <section class="panel devices-panel">
          <h3>监控设备</h3>
          <el-empty v-if="!devices.length" description="暂无设备" :image-size="64" />
          <ul v-else class="device-list">
            <li
              v-for="d in devices"
              :key="d.id"
              class="device-item"
              :class="{
                active: !isDeviceStreaming(d.id) && selectedDeviceIds.includes(d.id),
                streaming: isDeviceStreaming(d.id),
                focused: focusedDeviceId === d.id,
              }"
              @click="toggleDeviceSelect(d.id)"
            >
              <div class="device-row">
                <div class="device-info">
                  <div class="device-name">{{ d.name }}</div>
                  <div class="device-meta">{{ d.location || "未填写位置" }}</div>
                </div>
                <el-button
                  v-if="isDeviceStreaming(d.id)"
                  size="small"
                  type="danger"
                  plain
                  :loading="stoppingDeviceId === d.id"
                  :disabled="stopping && stoppingDeviceId !== d.id"
                  @click.stop="stopSession(sessions.find((s) => s.deviceId === d.id)!)"
                >
                  停止
                </el-button>
              </div>
              <el-tag size="small" :type="deviceStatusType(d.status)">{{ d.status_label }}</el-tag>
            </li>
          </ul>
        </section>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.monitor-live {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: calc(100vh - 40px);
}

.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  padding: 14px 18px;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.toolbar-left h2 {
  margin: 0;
  font-size: 18px;
}

.toolbar-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.monitor-body {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 340px;
  gap: 12px;
  min-height: 0;
}

.panel {
  background: #fff;
  border-radius: 12px;
  padding: 16px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
}

.video-panel {
  display: flex;
  flex-direction: column;
  min-height: 520px;
}

.video-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 420px;
}

.video-expanded {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 420px;
  background: #0f172a;
  border-radius: 10px;
  padding: 8px;
}

.video-stream-wrap {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.video-expanded .video-stream {
  flex: 1;
  min-height: 0;
  width: 100%;
  object-fit: contain;
}

.preview-stale-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(15, 23, 42, 0.72);
  color: #e2e8f0;
  font-size: 14px;
  pointer-events: none;
}

.expanded-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 4px 2px;
}

.expanded-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.expanded-title {
  font-size: 14px;
  font-weight: 600;
  color: #e2e8f0;
}

.video-grid {
  flex: 1;
  display: grid;
  gap: 8px;
  min-height: 420px;
  background: #0f172a;
  border-radius: 10px;
  padding: 8px;
}

.video-grid.grid-four {
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr;
}

.video-grid.grid-nine {
  grid-template-columns: repeat(3, 1fr);
  grid-template-rows: repeat(3, 1fr);
}

.grid-cell {
  position: relative;
  background: #1e293b;
  border-radius: 8px;
  overflow: hidden;
  min-height: 0;
  cursor: pointer;
  border: 2px solid transparent;
  display: flex;
  align-items: center;
  justify-content: center;
}

.grid-cell.focused {
  border-color: #3b82f6;
}

.cell-label {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  padding: 4px 8px;
  font-size: 12px;
  color: #f8fafc;
  background: linear-gradient(transparent, rgba(0, 0, 0, 0.65));
  pointer-events: none;
}

.cell-idle {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 12px;
  text-align: center;
  width: 100%;
  height: 100%;
}

.cell-idle-icon {
  font-size: 32px;
  color: #64748b;
}

.cell-name {
  font-size: 13px;
  font-weight: 600;
  color: #e2e8f0;
}

.cell-stats {
  font-size: 12px;
  color: #94a3b8;
}

.cell-hint {
  font-size: 11px;
  color: #64748b;
  margin-top: 4px;
}

.grid-cell.empty {
  cursor: default;
  background: #0f172a;
}

.video-stream {
  width: 100%;
  height: 100%;
  object-fit: contain;
  display: block;
}

.cell-placeholder {
  color: #475569;
  font-size: 28px;
}

.video-placeholder {
  flex: 1;
  min-height: 420px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: #f8fafc;
  border: 1px dashed #cbd5e1;
  border-radius: 10px;
  color: #64748b;
}

.placeholder-icon {
  font-size: 48px;
  margin-bottom: 8px;
  color: #94a3b8;
}

.video-settings {
  margin-top: 14px;
  border-top: 1px solid #e2e8f0;
  padding-top: 12px;
}

.setting-row {
  margin-bottom: 10px;
}

.setting-row.inline {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.setting-label {
  font-size: 13px;
  color: #64748b;
}

.settings-hint {
  margin: 4px 0 0;
  font-size: 12px;
  color: #94a3b8;
}

.side-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
}

.events-panel {
  flex: 1;
  min-height: 320px;
  max-height: calc(100vh - 280px);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.events-hint {
  margin: 8px 0 0;
  font-size: 12px;
  color: #64748b;
}

.devices-panel h3 {
  margin: 0 0 10px;
  font-size: 15px;
}

.device-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 220px;
  overflow: auto;
}

.device-item {
  padding: 10px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  margin-bottom: 8px;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}

.device-item:hover {
  background: #f8fafc;
}

.device-item.active {
  border-color: #3b82f6;
  background: #eff6ff;
}

.device-item.streaming {
  border-color: #86efac;
  background: #f0fdf4;
}

.device-item.focused {
  border-color: #3b82f6;
  box-shadow: 0 0 0 1px #3b82f6;
}

.device-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}

.device-info {
  min-width: 0;
  flex: 1;
}

.device-name {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 4px;
}

.device-meta {
  font-size: 12px;
  color: #64748b;
}

@media (max-width: 1100px) {
  .monitor-body {
    grid-template-columns: 1fr;
  }

  .events-panel {
    max-height: none;
  }
}
</style>
