<script setup lang="ts">
import { computed, ref } from "vue";
import type { ResultImage, TaskInfo } from "../api";

const props = withDefaults(
  defineProps<{
  title: string;
  task: TaskInfo | null;
  taskId: string;
  live?: boolean;
  /** 是否在标题旁显示任务状态标签 */
  showStatus?: boolean;
}>(),
  { showStatus: true },
);

const detailVisible = ref(false);
const detailImage = ref<ResultImage | null>(null);

function cropUrl(taskId: string, filename: string, url?: string) {
  if (url) return url;
  const path = filename.split("/").map(encodeURIComponent).join("/");
  return `/api/v1/tasks/${encodeURIComponent(taskId)}/files/crops/${path}`;
}

function previewUrls(taskId: string, img: ResultImage) {
  const list = img.merged_images?.length ? img.merged_images : [img];
  return list.map((m) => cropUrl(taskId, m.filename, m.url));
}

function formatConf(conf: number) {
  return `${(conf * 100).toFixed(0)}%`;
}

const sortedImages = computed(() => {
  const list = props.task?.result?.images ?? [];
  return [...list].sort((a, b) => {
    const ta = a.time_sec ?? a.frame_index ?? 0;
    const tb = b.time_sec ?? b.frame_index ?? 0;
    return ta - tb;
  });
});

function openDetail(img: ResultImage) {
  if ((img.merged_count ?? 0) <= 1) return;
  detailImage.value = img;
  detailVisible.value = true;
}

function closeDetail() {
  detailVisible.value = false;
  detailImage.value = null;
}

const detailPreviewList = computed(() => {
  if (!detailImage.value) return [];
  return previewUrls(props.taskId, detailImage.value);
});

function statusLabel(status: string) {
  const map: Record<string, string> = {
    pending: "等待中",
    downloading: "获取录像中",
    running: "分析中",
    streaming: "监控中",
    stopped: "已停止",
    completed: "已完成",
    failed: "失败",
  };
  return map[status] || status;
}

function statusType(status: string) {
  if (status === "failed") return "danger";
  if (status === "streaming" || status === "running") return "primary";
  if (status === "downloading") return "warning";
  if (status === "completed" || status === "stopped") return "success";
  return "info";
}

const showTaskProgress = computed(() => {
  if (!props.task) return false;
  return (
    props.task.status === "downloading" ||
    props.task.status === "running" ||
    props.task.status === "pending"
  );
});

const taskProgressPercent = computed(() => {
  const task = props.task;
  if (!task) return 0;
  const total = task.total || 0;
  if (total <= 0) return 0;
  return Math.min(100, Math.round(((task.progress || 0) / total) * 100));
});
</script>

<template>
  <div class="result-panel">
    <div class="result-head">
      <h3>{{ title }}</h3>
      <el-tag v-if="showStatus && task" size="small" :type="statusType(task.status)">
        {{ statusLabel(task.status) }}
      </el-tag>
    </div>

    <div v-if="!taskId" class="empty">开始分析后，结果将显示在这里</div>
    <div v-else-if="!task" class="empty">加载结果中...</div>
    <div v-else class="result-body">
      <div v-if="task.error && task.status === 'failed'" class="error-banner">
        任务异常结束：{{ task.error }}
      </div>
      <div v-if="task.result" class="stats">
        <div class="stat">
          <span class="stat-num">{{ task.result.saved_count ?? 0 }}</span>
          <span class="stat-label">人数</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ task.result.violation_frames ?? 0 }}</span>
          <span class="stat-label">违规帧</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ live ? (task.result.processed_frames ?? 0) : (task.result.sampled_frames ?? 0) }}</span>
          <span class="stat-label">{{ live ? "已分析帧" : "采样帧" }}</span>
        </div>
      </div>

      <div v-if="showTaskProgress" class="task-progress">
        <el-progress :percentage="taskProgressPercent" :stroke-width="10" />
        <p class="progress-note">{{ task.message || "处理中..." }}</p>
      </div>

      <div v-if="sortedImages.length" class="gallery">
        <article
          v-for="img in sortedImages"
          :key="`${img.person_id}-${img.filename}`"
          class="card"
          :class="{ merged: (img.merged_count ?? 0) > 1 }"
          @click="openDetail(img)"
        >
          <div class="photo">
            <el-image
              :src="cropUrl(taskId, img.filename, img.url)"
              fit="contain"
              class="photo-img"
              :preview-src-list="(img.merged_count ?? 0) > 1 ? [] : previewUrls(taskId, img)"
              @click.stop="(img.merged_count ?? 0) > 1 && openDetail(img)"
            />
            <span v-if="(img.merged_count ?? 0) > 1" class="count-pill">{{ img.merged_count }}</span>
          </div>
          <footer class="card-foot">
            <span class="time">{{ img.time_str }}</span>
            <span class="conf">未戴帽 {{ formatConf(img.confidence) }}</span>
          </footer>
        </article>
      </div>

      <el-empty
        v-else-if="task.status === 'streaming'"
        :description="task.message || '分析中，请稍候...'"
      />
      <el-empty v-else description="暂无未戴帽检测" />
    </div>

    <el-dialog
      v-model="detailVisible"
      :title="detailImage ? `人员 #${detailImage.person_id + 1} · 共 ${detailImage.merged_count} 张截图` : ''"
      width="720px"
      class="merge-dialog"
      destroy-on-close
      @close="closeDetail"
    >
      <div v-if="detailImage" class="detail-grid">
        <figure
          v-for="(m, idx) in detailImage.merged_images"
          :key="m.filename"
          class="detail-item"
          :class="{ best: m.is_best }"
        >
          <el-image
            :src="cropUrl(taskId, m.filename, m.url)"
            fit="contain"
            class="detail-img"
            :preview-src-list="detailPreviewList"
            :initial-index="idx"
          />
          <figcaption>
            <span>{{ m.time_str }}</span>
            <span v-if="m.confidence != null">未戴帽 {{ formatConf(m.confidence) }}</span>
          </figcaption>
        </figure>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.result-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.result-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}

h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: #0f172a;
}

.result-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.empty {
  color: #94a3b8;
  font-size: 13px;
}

.error {
  color: #ef4444;
  font-size: 13px;
}

.error-banner {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  color: #b91c1c;
  font-size: 13px;
  line-height: 1.5;
}

.stats {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}

.task-progress {
  margin-bottom: 16px;
}

.progress-note {
  margin: 8px 0 0;
  font-size: 13px;
  color: #64748b;
}

.stat {
  flex: 1;
  padding: 10px 12px;
  border-radius: 10px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  text-align: center;
}

.stat-num {
  display: block;
  font-size: 20px;
  font-weight: 700;
  color: #0f172a;
  line-height: 1.2;
}

.stat-label {
  font-size: 12px;
  color: #64748b;
}

.gallery {
  flex: 1;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px;
  overflow: auto;
  padding-right: 2px;
  align-content: start;
}

.card {
  border-radius: 10px;
  overflow: hidden;
  background: #fff;
  border: 1px solid #e2e8f0;
  transition: box-shadow 0.15s ease, transform 0.15s ease;
}

.card.merged {
  cursor: pointer;
}

.card.merged:hover {
  box-shadow: 0 6px 20px rgba(15, 23, 42, 0.08);
  transform: translateY(-1px);
}

.photo {
  position: relative;
  height: 180px;
  background: #eef2f7;
  display: flex;
  align-items: center;
  justify-content: center;
}

.photo-img {
  width: 100%;
  height: 100%;
}

.count-pill {
  position: absolute;
  top: 8px;
  right: 8px;
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.75);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  line-height: 22px;
  text-align: center;
}

.card-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  font-size: 12px;
  color: #64748b;
}

.time {
  flex-shrink: 0;
}

.conf {
  font-weight: 600;
  color: #ea580c;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: 12px;
  max-height: 60vh;
  overflow: auto;
}

.detail-item {
  margin: 0;
  border-radius: 8px;
  overflow: hidden;
  border: 2px solid transparent;
  background: #eef2f7;
}

.detail-item.best {
  border-color: #3b82f6;
}

.detail-img {
  width: 100%;
  height: 160px;
  display: block;
}

.detail-item figcaption {
  display: flex;
  justify-content: space-between;
  gap: 4px;
  padding: 6px 8px;
  font-size: 11px;
  color: #64748b;
}

:deep(.merge-dialog .el-dialog__body) {
  padding-top: 8px;
}

:deep(.photo-img .el-image__inner),
:deep(.detail-img .el-image__inner) {
  object-fit: contain;
}
</style>
