# 代码地图

这份文档用于从业务流程理解项目，不建议一开始逐行阅读所有文件。

## 一、建议阅读顺序

1. `api/main.py`：找到前端请求对应的接口。
2. `services/tasks/task_manager.py`：理解任务如何创建、启动和停止。
3. `services/analysis/video_analyzer.py`：理解离线视频如何抽帧分析。
4. `services/analysis/helmet_detector.py`：理解YOLO检测与违规判断。
5. `services/analysis/violation_store.py`：理解OSNet人物合并与结果保存。
6. `services/tasks/rtsp_stream.py`：理解实时流、预览、重连和停止。
7. 最后阅读数据库、设备、ISAPI、短信和认证等辅助模块。

不要先钻进 `models/` 和 `utils/`。它们主要是YOLO底层兼容代码，不是本项目业务主线。

`sql/` 用于首次创建数据库，`tools/` 用于独立测试模型或模拟ISAPI设备，都不在正式请求调用链中。

## 二、目录职责

### 接口层

| 文件 | 职责 |
| --- | --- |
| `api/main.py` | 接收HTTP请求、Pydantic参数校验、登录保护、调用业务服务、返回结果 |

### AI分析层

| 文件 | 职责 |
| --- | --- |
| `services/analysis/helmet_detector.py` | 加载YOLO；检测人、头、安全帽；判断违规；定位人物框 |
| `services/analysis/person_reid.py` | 加载OSNet ONNX；将人物图片转换为归一化特征向量 |
| `services/analysis/violation_store.py` | 实时增量匹配人物ID、保存crop、生成任务结果、触发新人物通知 |
| `services/analysis/video_analyzer.py` | 离线视频抽帧并调用检测和人物合并 |

### 任务层

| 文件 | 职责 |
| --- | --- |
| `services/tasks/task_manager.py` | 本地、ISAPI和RTSP任务的创建、状态、线程、停止与清理 |
| `services/tasks/rtsp_stream.py` | RTSP持续拉流、实时预览、断线重连、录制和停止信号 |

### 数据层

| 文件 | 职责 |
| --- | --- |
| `services/persistence/database.py` | SQLAlchemy引擎和数据库会话 |
| `services/persistence/models/` | 用户、设备、监控会话的数据表模型 |
| `services/persistence/device_service.py` | 设备增删改查和在线探测 |
| `services/persistence/monitor_sessions.py` | 分析会话的创建、更新、查询和删除 |
| `services/persistence/result_store.py` | 将分析结果写入JSON，或根据crop恢复结果 |

### 外部集成与基础能力

| 文件 | 职责 |
| --- | --- |
| `services/integrations/isapi_client.py` | 查询海康通道、构造地址、下载和合并历史录像 |
| `services/integrations/sms_client.py` | 调用短信HTTP接口 |
| `services/integrations/sms_notifier.py` | 异步短信队列和告警冷却 |
| `services/auth/auth_service.py` | 登录、令牌签发与解析 |
| `services/auth/passwords.py` | 密码哈希与校验 |
| `services/common/` | 图片写入和时间格式化 |
| `services/config.py` | 项目根目录、上传目录、结果目录和权重目录 |

## 三、三条核心调用链

### 本地视频

```text
前端上传视频
→ api/main.py
→ tasks/task_manager.py 创建任务和后台线程
→ analysis/video_analyzer.py 抽帧
→ analysis/helmet_detector.py 判断违规
→ analysis/violation_store.py 合并人物并保存crop
→ persistence/monitor_sessions.py 保存会话状态
→ 前端轮询任务结果
```

### ISAPI历史录像

```text
前端提交设备、通道和时间
→ api/main.py
→ integrations/isapi_client.py 查询并下载录像
→ tasks/task_manager.py
→ 后续与本地视频共用同一条AI分析链路
```

### RTSP实时流

```text
前端启动监控
→ api/main.py
→ tasks/task_manager.py 创建RTSP任务
→ tasks/rtsp_stream.py 创建长期运行线程并持续读帧
→ analysis/helmet_detector.py 判断违规
→ analysis/violation_store.py 增量合并人物
→ persistence/monitor_sessions.py 更新会话
→ integrations/sms_notifier.py 对新违规人员告警
→ 前端读取MJPEG预览和任务结果
```

## 四、一句话理解项目

三种视频来源负责“取得画面”，YOLO负责“发现违规”，OSNet负责“判断是不是同一个人”，任务和数据服务负责“让结果能够运行、保存和展示”。
