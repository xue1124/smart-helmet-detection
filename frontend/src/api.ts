import axios from "axios";

const http = axios.create({
  baseURL: "/api/v1",
  timeout: 30000,
  withCredentials: true,
});

http.interceptors.response.use(
  (res) => res,
  (err) => {
    const url = err?.config?.url || "";
    if (err?.response?.status === 401 && !url.includes("/auth/login")) {
      window.dispatchEvent(new CustomEvent("sc-unauthorized"));
    }
    return Promise.reject(err);
  },
);

export default http;

export interface AuthUser {
  id: number;
  username: string;
  role: string;
  last_login_at?: string | null;
}

export async function fetchCurrentUser(): Promise<AuthUser | null> {
  try {
    const { data } = await http.get<{ user: AuthUser }>("/auth/me");
    return data.user;
  } catch {
    return null;
  }
}

export async function login(username: string, password: string): Promise<AuthUser> {
  const { data } = await http.post<{ user: AuthUser }>("/auth/login", { username, password });
  return data.user;
}

export async function logout(): Promise<void> {
  await http.post("/auth/logout");
}

export interface IsapiChannel {
  id: number;
  name: string;
  raw_id: string;
}

export interface ResultImage {
  filename: string;
  person_id: number;
  time_str: string;
  confidence: number;
  merged_count: number;
  url: string;
  merged_images?: Array<{
    filename: string;
    url: string;
    is_best?: boolean;
    time_str?: string;
    confidence?: number;
  }>;
}

export interface TaskResult {
  saved_count: number;
  violation_frames: number;
  sampled_frames: number;
  processed_frames?: number;
  elapsed_sec?: number;
  images: ResultImage[];
}

export interface TaskInfo {
  id: string;
  status: string;
  progress: number;
  total: number;
  message: string;
  video_name: string;
  source_type?: string;
  device_id?: number;
  error?: string;
  result?: TaskResult | null;
  created_at?: string;
  finished_at?: string;
}

export interface MonitorDevice {
  id: number;
  name: string;
  location: string;
  rtsp_url: string;
  status: number;
  status_label: string;
  enabled: boolean;
  created_by: string;
  remark: string;
  last_probe_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface MonitorSessionItem {
  id: number;
  device_id: number | null;
  device_name: string;
  source_type: string;
  source_type_label: string;
  task_id: string;
  status: string;
  status_label: string;
  frame_stride: number;
  enable_reid: boolean;
  reid_threshold: number;
  output_dir: string;
  error_message?: string | null;
  started_at: string | null;
  stopped_at: string | null;
  saved_count: number;
  violation_frames: number;
}

export interface ActiveRtspSession {
  task_id: string;
  device_id: number | null;
  device_name: string;
}

export async function fetchActiveRtspSessions(): Promise<ActiveRtspSession[]> {
  const { data } = await http.get<{ sessions: ActiveRtspSession[]; count: number }>(
    "/stream/live/active",
  );
  return data.sessions || [];
}

export async function deleteMonitorSession(sessionId: number): Promise<void> {
  await http.delete(`/monitor-sessions/${sessionId}`);
}
